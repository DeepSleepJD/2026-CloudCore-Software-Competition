"""Round-driven task solver. Only the platform executes commands / calls its LLM."""
import json
import logging
import re

from .model import command, distance, neighbours, pos
from .navigation import paths

LOG = logging.getLogger(__name__)
INF = 10**6


def envelope(text):
    """Accept one explicit JSON operation, never guess an answer from prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


class TaskScheduler:
    def __init__(self, planner):
        self.p, self.w = planner, planner.w
        self.m = planner.memory.setdefault("tasks", {"active": None, "history": [],
                                                     "backoff": {}, "experience": {}})
        self.prompt = self.execute = ""
        self.actor = next((r for r in self.w.actors if r.kind == "pioneer"), None)
        self.phase = self.w.raw.get("phaseTask") or ""
        self.errors = self.w.raw.get("errors") or []
        self.codes = {e.get("errorCode") for e in self.errors}

    def trace(self, event, **details):
        """Diagnostic only: preserve full payloads, including terminal-round feedback."""
        if not LOG.isEnabledFor(logging.INFO):
            return
        s = self.m["active"] or {}
        record = {"event": event, "round": self.w.round, "team": self.w.team,
                  "teamId": self.w.raw.get("teamOur", {}).get("teamId"),
                  "matchId": self.w.raw.get("matchId"),
                  "taskKey": s.get("key"), "started": s.get("started"),
                  "accepted": s.get("accepted"), "stage": s.get("stage"),
                  "sent": s.get("sent"), "timeoutRounds": s.get("timeout"),
                  "deadline": s.get("deadline"),
                  "remainingRounds": s["deadline"] - self.w.round if "deadline" in s else None,
                  "calls": s.get("calls", 0), "commands": s.get("cmds", 0),
                  "submissions": s.get("submits", 0),
                  "remainingCalls": 8 - s.get("calls", 0),
                  "remainingCommands": 6 - s.get("cmds", 0),
                  "remainingSubmissions": 3 - s.get("submits", 0), **details}
        payload = json.dumps(record, ensure_ascii=False)
        # Keep individual lines manageable without losing long prompts or outputs.
        if len(payload) <= 4000:
            LOG.info("task_trace=%s", payload)
        else:
            parts = [payload[i:i + 4000] for i in range(0, len(payload), 4000)]
            for index, part in enumerate(parts, 1):
                LOG.info("task_trace_chunk=%s", json.dumps(
                    {"event": event, "round": self.w.round, "teamId": record["teamId"],
                     "taskKey": s.get("key"), "started": s.get("started"),
                     "part": index, "parts": len(parts), "data": part}, ensure_ascii=False))

    def cells(self, task):
        target = pos(task["taskPosition"])
        # Task point 2 spans two map cells. Metadata may name either one.
        kind = self.w.zones.get(target)
        if kind and kind.startswith(self.w.team + "TaskPoint"):
            return [p for p, k in self.w.zones.items() if k == kind]
        return [target]

    def adjacent(self, state):
        return any(distance(self.actor.p, p) == 1 for p in state["cells"])

    def home_distance(self):
        # Long-term estimates include walls, but allow teammates to yield later.
        distances, _ = paths(self.w, self.actor, forbidden=self.p.layout.towers, ignore_actors=True)
        return distances.get(self.p.layout.operator, INF)

    def work_window(self):
        """Rounds until the next defence deadline, based on visible own threats."""
        if self.w.day:
            return 70 - self.w.day_tick
        # Do not trust an empty spawn-frame snapshot. Once the wave has arrived,
        # a cleared own wave leaves the rest of this night and next day available.
        threatened = any(not r.target_team or r.target_team == self.w.team
                         or self.w.base_distance(r.p) <= 10 for r in self.w.robots)
        if self.w.day_tick >= 80 and not threatened:
            return 200 - self.w.day_tick
        return 0

    def solve_estimate(self, task_type, timeout):
        durations = [h["round"] - h["accepted"] for h in self.m["history"]
                     if h["reason"] == "completed_inferred" and h["taskType"] == task_type
                     and h.get("accepted") is not None]
        # Learn scheduling latency only; never reuse a previous task's answer.
        return min(timeout, max(8, max(durations[-4:]) + 3) if durations else 12)

    def return_home(self):
        if not self.w.day and self.actor != self.p.operator:
            # The incumbent still owns the gun. Merely cooling down is no handoff.
            return self.p.approach(self.actor, self.p.layout.operator, exact=True, safe=False)
        return self.p.operate(self.actor)

    def finish(self, reason):
        s = self.m["active"]
        if s:
            record = {"round": self.w.round, "taskType": s["type"], "reason": reason,
                      "accepted": s.get("accepted"), "submissions": s.get("submits", 0),
                      "errors": self.errors, "passRate": None}
            self.m["history"] = (self.m["history"] + [record])[-40:]
            self.m["backoff"][s["key"]] = self.w.round + 30
            if reason == "completed_inferred" and s.get("experience"):
                self.m["experience"][s["type"]] = s["experience"][:2000]
            LOG.info("task_result=%s", json.dumps(record, ensure_ascii=False))
            self.trace("finish", reason=reason, errors=self.errors,
                       serverTaskTimeout=1 in self.codes,
                       localDeadlineExceeded=s.get("accepted") is not None and self.w.round > s["deadline"])
        self.m["active"] = None
        # An interrupted phase cannot be adopted again before the server clears it.
        self.m["await_clear"] = bool(self.phase)

    def ask(self, feedback=""):
        s = self.m["active"]
        if s["calls"] >= 8 or s["submits"] >= 3:
            self.finish("retry_exhausted")
            return False
        s["calls"] += 1
        self.prompt = (
            '你是比赛沙盒任务求解器。仅返回一个JSON对象：'
            '{"executeCmd":"shell命令"} 或 {"taskAnswer":"严格按任务要求的最终答案",'
            '"experience":"可复用方法，不含本次答案"}。两种操作只能选一种。'
            '先读取任务指定文件和API文档，基于实际输出求解，禁止猜测文件内容、token或复用旧答案。'
            '命令仅在隔离的比赛沙盒运行，无外网，支持shell/python，15秒上限；'
            '命令应有界、可重复执行，优先只读；不要访问个人凭据。'
            '错误/截断输出不能当成功；截断时缩小查询或分页。答案若为JSON可将taskAnswer设为对象。'
            '\n' + json.dumps({"phaseTask": self.phase,
                               "remainingRounds": s["deadline"] - self.w.round,
                               "experience": self.m["experience"].get(s["type"], ""),
                               "transcript": s["transcript"], "feedback": feedback}, ensure_ascii=False))
        s["stage"], s["sent"] = "llm", self.w.round
        self.trace("llm_request", prompt=self.prompt, feedback=feedback)
        return True

    def remember(self, kind, value):
        s = self.m["active"]
        if len(value) > 12000 or len(s["transcript"]) >= 8:
            self.trace("context_trim", kind=kind, originalChars=len(value),
                       retainedChars=min(len(value), 12000),
                       droppedEntries=max(0, len(s["transcript"]) + 1 - 8))
        s["transcript"] = (s["transcript"] + [{kind: value[:12000]}])[-8:]

    def run(self):
        """Return True to reserve the pioneer, even when its action is to wait."""
        s = self.m["active"]
        if s or self.phase or self.w.raw.get("lastCmdResult"):
            output = self.w.raw.get("lastCmdResult") or ""
            self.trace("round_input", phaseTask=self.phase,
                       llmResp=self.w.raw.get("llmResp"), lastCmdResult=output,
                       commandTimedOut=output.startswith("[TIMEOUT]"),
                       commandJudgerError=output.startswith("[JUDGER_ERROR]"),
                       commandOutputTruncated="[TRUNCATED]" in output,
                       errors=self.errors,
                       consecutive=self.w.round == s.get("sent", -2) + 1 if s else False,
                       actorPosition=self.actor.p if self.actor else None,
                       actorHealth=self.actor.health if self.actor else None,
                       lastRoundRoleActionResults=self.w.raw.get("lastRoundRoleActionResults", {}))
        if not self.actor:
            if s:
                self.finish("death")
            return False
        if s:
            # Terminal feedback must be consumed before any survival action.
            valid = self.w.raw.get("lastRoundRoleActionResults", {}).get(str(self.actor.id))
            consecutive = self.w.round == s.get("sent", -2) + 1
            if 1 in self.codes or (s.get("accepted") is not None and self.w.round > s["deadline"]):
                self.finish("timeout")
                return False
            if s["stage"] not in ("travel", "accept") and not self.phase:
                completed = (s["stage"] == "submit" and consecutive and valid is True
                             and not self.codes and self.adjacent(s) and self.w.round < s["deadline"])
                self.finish("completed_inferred" if completed else
                            "partial_or_wrong_ended" if 2 in self.codes else "ended_unconfirmed")
                return False
            if s["stage"] not in ("travel", "accept") and not self.adjacent(s):
                self.finish("position_lost")
                return False
            home = self.home_distance()
            if self.work_window() <= home + 5:
                self.finish("defense_return_deadline")
                self.return_home()
                return True
            if self.actor.p in self.p.danger or self.actor.health < 60:
                self.finish("emergency_retreat")
                if not self.p.retreat(self.actor):
                    self.return_home()
                return True
            if s["stage"] == "travel":
                task = next((t for t in self.w.raw["teamOur"].get("playerTasks", [])
                             if self.task_key(t) == s["key"]), None)
                if not task or not task.get("isValid") or task.get("coldDownRounds", 0) > 0:
                    self.finish("unavailable")
                    return False
                if self.w.round - s["started"] > s["travel_limit"]:
                    self.finish("travel_timeout")
                    return False
                if self.adjacent(s):
                    s.update(stage="accept", sent=self.w.round, accepted=self.w.round,
                             deadline=self.w.round + s["timeout"])
                    self.p.emit(self.actor, command("acceptTask"))
                    self.trace("accept_request")
                else:
                    self.p.approach(self.actor, s["goal"], exact=True)
                return True
            if s["stage"] == "accept":
                if self.phase:
                    s["description"] = self.phase
                    return self.ask()
                if consecutive and valid is False:
                    self.finish("accept_rejected")
                    return False
                if self.w.round - s["sent"] >= 2:
                    self.finish("accept_unconfirmed")
                    return False
                return True
            if self.phase != s["description"]:
                self.finish("phase_replaced")
                return False
            if 5 in self.codes:
                # Active tasks should be exempt. Avoid hammering a disagreeing server.
                self.finish("llm_quota_error")
                return False
            if s["stage"] == "llm":
                text = self.w.raw.get("llmResp") if consecutive else ""
                if not text:
                    return self.ask("LLM未返回，重试；不要编造答案。") if self.w.round - s["sent"] >= 2 else True
                value = envelope(text)
                cmd, answer = value.get("executeCmd"), value.get("taskAnswer")
                if bool(cmd) == (answer is not None):
                    return self.ask("响应格式错误，必须且只能提供executeCmd或taskAnswer。")
                if cmd:
                    if not isinstance(cmd, str) or len(cmd) > 12000 or s["cmds"] >= 6:
                        self.trace("command_rejected", invalidType=not isinstance(cmd, str),
                                   tooLong=isinstance(cmd, str) and len(cmd) > 12000,
                                   budgetExhausted=s["cmds"] >= 6)
                        self.finish("command_budget_exhausted")
                        return False
                    self.execute = cmd
                    self.remember("executeCmd", cmd)
                    s.update(stage="cmd", sent=self.w.round, cmds=s["cmds"] + 1)
                    self.trace("command_request", executeCmd=cmd)
                    return True
                if not isinstance(answer, str):
                    answer = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
                if not answer.strip() or len(answer) > 64000 or answer in s["answers"]:
                    return self.ask("答案为空、过长或与已提交失败的答案重复，请修正。")
                s["answers"].append(answer)
                s["experience"] = str(value.get("experience", ""))[:2000]
                self.remember("taskAnswer", answer)
                self.p.emit(self.actor, command("submitAnswer", taskAnswer=answer))
                s.update(stage="submit", sent=self.w.round, submits=s["submits"] + 1)
                self.trace("submit_request", taskAnswer=answer, experience=s["experience"])
                return True
            if s["stage"] == "cmd":
                output = self.w.raw.get("lastCmdResult") if consecutive else ""
                if output:
                    self.remember("lastCmdResult", output)
                    ok = output.startswith("[exitCode:0]\n") and "[TRUNCATED]" not in output
                    return self.ask("命令正常完成，请根据实际输出继续。" if ok else
                                    "命令失败/超时/判题异常/截断；修正命令，不能将部分输出当完整答案。")
                if self.w.round - s["sent"] >= 2:
                    return self.ask("命令结果丢失，检查状态后使用幂等查询，不能假定成功。")
                return True
            if s["stage"] == "submit":
                if consecutive and (2 in self.codes or valid is False):
                    self.remember("submissionFeedback", json.dumps(self.errors, ensure_ascii=False))
                    return self.ask("答案错误或部分正确；当前接口未提供通过率，保留已有正确字段并修正。")
                if self.w.round - s["sent"] >= 2:
                    return self.ask("提交后任务仍在，结果未确认；不要重复同一答案。")
                return True
        if not self.phase:
            self.m["await_clear"] = False
        # Unknown active phases are held in place; never execute uncorrelated output.
        if self.phase:
            if self.work_window() > self.home_distance() + 5:
                return True
            return False
        if not self.work_window() or self.actor.health < 100 or self.actor.p in self.p.danger:
            return False
        # A carried strategic voucher is delivered before starting a new job.
        if any("UpgradeVoucher" in item for item in self.actor.bag):
            return False
        distances, _ = self.p.route(self.actor)
        home_paths, _ = paths(self.w, self.actor, start=self.p.layout.operator, ignore_actors=True)
        options = []
        for task in self.w.raw["teamOur"].get("playerTasks", []):
            if task.get("taskType") not in ("自进化类1", "自进化类2"):
                continue
            timeout = task.get("timeoutRounds", 0)
            key = self.task_key(task)
            if not task.get("isValid") or task.get("coldDownRounds", 0) > 0 or timeout < 6:
                continue
            if self.m["backoff"].get(key, 0) > self.w.round:
                continue
            cells = self.cells(task)
            if any(self.w.zones.get(p, "").startswith(("challenger" if self.w.team == "defender" else "defender") + "TaskPoint") for p in cells):
                continue
            goals = {q for p in cells for q in neighbours(p) if q in distances and q not in self.p.danger}
            estimate = self.solve_estimate(task["taskType"], timeout)
            goals = [q for q in goals if distances[q] + estimate + home_paths.get(q, INF) + 6 < self.work_window()]
            if not goals:
                continue
            goal = min(goals, key=lambda q: (distances[q], home_paths.get(q, INF), q))
            reward = task.get("goldReward", 0) + task.get("scoreReward", 0) * 0.25
            options.append((-(reward / (distances[goal] + estimate + 1)), key, task, cells, goal))
        if not options:
            return False
        _, key, task, cells, goal = min(options, key=lambda x: x[:2])
        self.m["active"] = {"key": key, "type": task["taskType"], "cells": cells, "goal": goal,
                            "stage": "travel", "started": self.w.round, "timeout": task["timeoutRounds"],
                            "travel_limit": distances[goal] + 6, "calls": 0, "cmds": 0, "submits": 0,
                            "transcript": [], "answers": []}
        self.trace("task_selected", task=task, goal=goal)
        return self.run()

    @staticmethod
    def task_key(task):
        return (task["taskType"], pos(task["taskPosition"]))
