"""Round-driven task solver. Only the platform executes commands / calls its LLM."""
import json
import logging
import re
import shlex
import copy
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from .model import command, distance, neighbours, pos
from .navigation import paths
from .task_bootstrap import bootstrap_command
from .sandbox_http import api_failed
from .task_sop import learn_sop, validate_pages
from .task_prompt import TASK_PROMPT
from .build_info import BUILD

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
        self.m.setdefault("discoveries", {})

    @staticmethod
    def _nonempty_answer(value):
        if not isinstance(value, dict):
            return bool(str(value).strip())
        stats = {"total_count", "world_heritage_count", "types", "oldest_era"}
        return bool(value) and not (stats.issubset(value) and not value.get("total_count")
                                    and not value.get("world_heritage_count")
                                    and not value.get("types") and not value.get("oldest_era"))

    def answer_evidence(self, value):
        """Hard gate: submit only data supported by this task's sandbox output."""
        s = self.m["active"]
        evidence = s.get("evidence", {})
        if not evidence.get("successfulCommand") or evidence.get("invalid"):
            return False, "没有成功的当前任务命令结果，禁止提交猜测或默认答案。"
        if not self._nonempty_answer(value):
            return False, "答案为空或是无证据的全零默认值，禁止提交。"
        expected, observed = evidence.get("expectedCount"), evidence.get("observedCount")
        if expected is not None and observed is not None and observed != expected:
            return False, f"数据不完整：已观察{observed}条，接口声明{expected}条；继续分页后再提交。"
        if isinstance(value, dict) and expected is not None and "total_count" in value:
            try:
                if int(value["total_count"]) != int(expected):
                    return False, "答案total_count与已验证接口总数不一致。"
            except (TypeError, ValueError):
                return False, "答案total_count不是有效数字。"
        if 'verifiedAnswer' not in s or json.dumps(value, sort_keys=True) != json.dumps(s['verifiedAnswer'], sort_keys=True):
            return False, "答案必须与当前任务沙盒已验证输出一致，禁止模型修改或猜测。"
        return True, ""

    @staticmethod
    def _record_payload_evidence(payload, evidence):
        """Find common nested pagination shapes without retaining records."""
        pending = [payload]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key in ("pagination", "pageInfo"):
                    page = item.get(key)
                    if isinstance(page, dict):
                        for total_key in ("total_count", "totalCount", "total"):
                            if isinstance(page.get(total_key), int):
                                evidence["expectedCount"] = page[total_key]
                        for rows_key in ("records", "items", "data"):
                            if isinstance(page.get(rows_key), list):
                                evidence["observedCount"] = len(page[rows_key])
                for rows_key in ("records", "items"):
                    if isinstance(item.get(rows_key), list):
                        evidence["observedCount"] = len(item[rows_key])
                pending.extend(v for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                pending.extend(v for v in item if isinstance(v, (dict, list)))

    def record_evidence(self, output, body, normal):
        s = self.m["active"]
        s.pop('verifiedAnswer', None)
        evidence = s['evidence'] = {'successfulCommand': False, 'invalid': True}
        if not normal or not body:
            return
        if body.get("kind") == "task_result":
            try:
                if body.get('complete') is not True:
                    raise ValueError('Incomplete result')
                if s.get('sopRun'):
                    config, city = s['sopRun']['config'], s['sopRun']['city']
                    answer, verified = validate_pages(config, city, body.get('pages'))
                    if json.dumps(answer, sort_keys=True) != json.dumps(body.get('answer'), sort_keys=True):
                        raise ValueError('Answer disagrees with records')
                    evidence.update(verified)
                    self.m['discoveries'].setdefault(s['type'], {})['sop'] = copy.deepcopy(config)
                else:
                    answer = body.get('answer')
                    tokens = re.findall(r'(?m)^TOKEN\s*[:=]\s*([A-Za-z0-9_-]+)\s*$',
                                        str(body.get('checkerOutput', '')))
                    if not tokens or len(set(tokens)) != 1 or answer != {'token': tokens[0]}:
                        raise ValueError('Result requires original pages or checker TOKEN output')
                    evidence.update(successfulCommand=True, invalid=False)
                s['verifiedAnswer'] = answer
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                evidence['invalid'] = True
                self.trace('evidence_rejected', reason=str(exc))
            return
        if body.get("kind") == "task_http":
            if body.get("ok") is True:
                self._record_payload_evidence(body.get("data"), evidence)
                try:
                    sop = learn_sop(body)
                except (KeyError, TypeError, ValueError):
                    sop = None
                if sop:
                    self.m['discoveries'].setdefault(s['type'], {})['sop'] = sop
            return
        # A raw final JSON token is also usable, but arbitrary statistics aren't proof.
        if set(body) == {'token'} and isinstance(body['token'], str) and body['token']:
            s['verifiedAnswer'] = body
            evidence.update(successfulCommand=True, invalid=False)

    def budget(self):
        s = self.m["active"] or {}
        remaining = max(0, s.get("deadline", self.w.round) - self.w.round)
        # SOP: command +1, verified result and submit +2, feedback +3.
        return {"remainingRounds": remaining,
                "remainingCalls": max(0, remaining - 2),
                "remainingCommands": max(0, (remaining - 2) // 2),
                "remainingScriptCommands": max(0, (remaining - 3) // 2),
                "remainingSubmissions": max(0, 3 - s.get("submits", 0))}

    def trace(self, event, **details):
        """Diagnostic only: preserve full payloads, including terminal-round feedback."""
        if not LOG.isEnabledFor(logging.INFO):
            return
        s = self.m["active"] or {}
        record = {"event": event, "buildId": BUILD['buildId'], "round": self.w.round, "team": self.w.team,
                  "teamId": self.w.raw.get("teamOur", {}).get("teamId"),
                  "matchId": self.w.raw.get("matchId"),
                  "taskKey": s.get("key"), "started": s.get("started"),
                  "accepted": s.get("accepted"), "stage": s.get("stage"),
                  "sent": s.get("sent"), "timeoutRounds": s.get("timeout"),
                  "deadline": s.get("deadline"),
                  "remainingRounds": s["deadline"] - self.w.round if "deadline" in s else None,
                  "calls": s.get("calls", 0), "commands": s.get("cmds", 0),
                  "submissions": s.get("submits", 0),
                  **self.budget(), **details}
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
            if s.get("experience"):
                self.m["experience"][s["type"]] = s["experience"][:2000]
            if self.m["discoveries"].get(s["type"]):
                self.m["discoveries"][s["type"]] = copy.deepcopy(self.m["discoveries"][s["type"]])
            LOG.info("task_result=%s", json.dumps(record, ensure_ascii=False))
            self.trace("finish", reason=reason, errors=self.errors,
                       serverTaskTimeout=1 in self.codes,
                       localDeadlineExceeded=s.get("accepted") is not None and self.w.round > s["deadline"])
        self.m["active"] = None
        # An interrupted phase cannot be adopted again before the server clears it.
        self.m["await_clear"] = bool(self.phase)

    def ask(self, feedback=""):
        s = self.m["active"]
        if s["submits"] >= 3:
            self.finish("retry_exhausted")
            return False
        if s["deadline"] - self.w.round < 3:
            self.finish("insufficient_rounds")
            return False
        s["calls"] += 1
        self.prompt = (TASK_PROMPT.replace('\n', ' ') + '\n' + json.dumps({"phaseTask": self.phase,
                               **self.budget(), "bootstrap": {"httpContract": s.get("bootstrap", {}).get("httpContract"),
                                             "httpHelper": s.get("bootstrap", {}).get("httpHelper"),
                                             "taskFile": s.get("bootstrap", {}).get("taskFile"),
                                             "files": s.get("bootstrap", {}).get("files", [])},
                               "discoveries": self.m["discoveries"].get(s["type"], {}),
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
        retained = value if len(value) <= 12000 else value[:12000] + "\n[CONTEXT_TRUNCATED]"
        s["transcript"] = (s["transcript"] + [{kind: retained}])[-8:]

    def send_command(self, cmd, bootstrap=False, direct=False):
        s = self.m["active"]
        if not isinstance(cmd, str) or len(cmd) > 16000:
            return self.ask("命令格式错误或超过16000字符，请缩短。")
        if s["deadline"] - self.w.round < (3 if direct else 4):
            self.trace("command_rejected", reason="insufficient_rounds")
            return self.ask("剩余回合不足以执行命令并提交；只能使用已有实际数据生成答案。")
        self.execute = cmd
        if not bootstrap:
            self.remember("executeCmd", cmd)
        s.update(stage="cmd", sent=self.w.round, cmds=s["cmds"] + 1,
                 bootstrapPending=bootstrap)
        self.trace("command_request", executeCmd=cmd, bootstrap=bootstrap)
        return True

    def run_sop(self, params):
        s = self.m['active']
        config = self.m['discoveries'].get(s['type'], {}).get('sop')
        bootstrap = s.get('bootstrap', {})
        helper = bootstrap.get('httpHelper')
        if (not isinstance(params, dict) or not config or not helper
                or not isinstance(params.get('city'), str) or not params['city']
                or not isinstance(params.get('apiKey'), str)):
            return self.ask('runSop需要已发现的SOP、当前city和apiKey；先使用httpRequest探索真实结构。')
        task_text = next((f['text'] for f in bootstrap.get('files', [])
                          if f.get('path') == bootstrap.get('taskFile')), '')
        city_match = re.search(r'查询\s*([\u4e00-\u9fff]{2,12}?)(?:市)?(?:的)?(?:全部)?文化遗产', task_text)
        if (not city_match or params['city'].removesuffix('市') != city_match.group(1)
                or 'world_heritage_count' not in task_text):
            return self.ask('runSop城市或统计类型未在本题任务文件中确认，请核对任务。')
        source = ("import runpy,json,signal\n"
                  "if hasattr(signal,'alarm'): signal.alarm(12)\n"
                  "h=runpy.run_path(%r)\n"
                  "result=h['solve_sop'](%r,%r,%r,h['request_json'])\n"
                  "print(json.dumps(result,ensure_ascii=False))" %
                  (helper, config, params['city'], params['apiKey']))
        cmd = shlex.join(['python3', '-c', source])
        accepted = self.send_command(cmd, direct=True)
        if self.execute == cmd:
            s['sopRun'] = {'config': copy.deepcopy(config), 'city': params['city']}
        return accepted

    def submit_verified(self, answer, experience=''):
        s = self.m['active']
        if isinstance(answer, str):
            try:
                answer = json.loads(answer)
            except ValueError:
                pass
        valid, reason = self.answer_evidence(answer)
        if not valid:
            self.trace('answer_rejected', reason=reason, evidence=s.get('evidence'))
            return self.ask(reason)
        wire = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, separators=(',', ':'))
        if not wire.strip() or len(wire) > 64000 or wire in s['answers']:
            return self.ask('答案为空、过长或与已提交失败答案重复；请重新验证。')
        if s['submits'] >= 3 or s['deadline'] - self.w.round < 2:
            self.finish('insufficient_rounds' if s['submits'] < 3 else 'retry_exhausted')
            return False
        s['answers'].append(wire)
        s['experience'] = str(experience)[:2000]
        self.remember('taskAnswer', wire)
        self.p.emit(self.actor, command('submitAnswer', taskAnswer=wire))
        s.update(stage='submit', sent=self.w.round, submits=s['submits'] + 1)
        self.trace('submit_request', taskAnswer=wire, evidence=s.get('evidence'))
        return True

    def observe_output(self, output):
        """Keep verified environment facts even when the task later fails."""
        s = self.m["active"]
        normal = output.startswith("[exitCode:0]\n") and "[TRUNCATED]" not in output
        body = envelope(output.partition("\n")[2])
        facts = self.m["discoveries"].setdefault(s["type"], {})
        if s.pop("bootstrapPending", False) and normal and body.get("kind") == "task_bootstrap":
            s["bootstrap"] = body
            if body.get("taskFile") and body.get("files"):
                facts["directory"] = str(PurePosixPath(body["taskFile"]).parent)
            if body.get("httpContract"):
                facts["httpContract"] = body["httpContract"]
            return normal and not body.get("errors") and not any(f.get("truncated") for f in body.get("files", []))
        # Store only endpoint/header metadata, never response records, keys or tokens.
        if body.get("kind") == "task_http" and normal and body.get("ok") is True:
            parsed = urlsplit(body.get("url", ""))
            facts["http"] = {"endpoint": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                             "authHeader": body.get("authHeader")}
        message = str(body.get("message", "")).lower()
        if api_failed(body) and "authorization" in message and "bearer" in message:
            facts["authGuidance"] = "服务端曾要求Authorization: Bearer <任务API key>；当前任务需验证。"
        self.record_evidence(output, body, normal and not api_failed(body))
        return normal and bool(body) and not api_failed(body) and (
            body.get('kind') == 'task_http' or not s.get('evidence', {}).get('invalid'))

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
                    directory = self.m["discoveries"].get(s["type"], {}).get("directory")
                    cmd = bootstrap_command(self.phase, directory)
                    if cmd:
                        return self.send_command(cmd, bootstrap=True)
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
                http = value.get("httpRequest")
                sop = value.get('runSop')
                task_result = value.get("task_result")
                if isinstance(task_result, dict):
                    answer = task_result.get("answer")
                    # Compatibility only: a model's evidence is never authoritative.
                if sum((bool(cmd), answer is not None and not isinstance(task_result, dict),
                        http is not None, sop is not None, isinstance(task_result, dict))) != 1:
                    return self.ask("响应格式错误，只能提供executeCmd、httpRequest、runSop或taskAnswer中的一种。")
                if sop is not None:
                    return self.run_sop(sop)
                if http is not None:
                    helper = s.get("bootstrap", {}).get("httpHelper")
                    if not helper or not isinstance(http, dict) or not isinstance(http.get("url"), str):
                        return self.ask("httpRequest格式错误或httpHelper不可用，请用executeCmd进行有界查询。")
                    args = ["python3", helper, http["url"]]
                    for field, flag in (("apiKey", "--key"), ("header", "--header")):
                        if field in http:
                            if not isinstance(http[field], str):
                                return self.ask("httpRequest的apiKey和header必须为字符串。")
                            args.extend([flag, http[field]])
                    cmd = shlex.join(args)
                if cmd:
                    s.pop('sopRun', None)
                    return self.send_command(cmd)
                return self.submit_verified(answer, value.get('experience', ''))
            if s["stage"] == "cmd":
                output = self.w.raw.get("lastCmdResult") if consecutive else ""
                if output:
                    bootstrap = s.get("bootstrapPending", False)
                    ok = self.observe_output(output)
                    if not bootstrap or not s.get("bootstrap"):
                        self.remember("lastCmdResult", output)
                    body = envelope(output.partition('\n')[2])
                    if ok and body.get('kind') == 'task_result' and 'verifiedAnswer' in s:
                        return self.submit_verified(s['verifiedAnswer'], '使用已验证的SOP读取当前任务数据并校验结果。')
                    return self.ask("命令正常完成，请根据实际输出继续。" if ok else
                                    "命令或API业务失败/超时/判题异常/截断；检查状态码、认证和类型，不能将部分输出当完整答案。")
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
