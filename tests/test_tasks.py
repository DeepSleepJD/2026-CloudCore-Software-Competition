"""Synthetic platform feedback, not an official server or a real LLM evaluation."""
import copy
import json
import time
import threading
import unittest
from urllib.request import Request, urlopen

from agent.model import World, Layout
from agent.strategy import Agent
from agent.server import AgentServer
from tests.support import EconomyRollout, check, d, point, unit


class TaskRollout:
    def __init__(self, left=False):
        self.sim = EconomyRollout(left)
        self.p = self.sim.request
        self.actor = next(r for r in self.p["teamOur"]["roles"] if r["roleType"] == "pioneer")
        self.rid = str(self.actor["id"])
        self.agent = Agent()
        self.number = 0
        self.events = []
        self.times = []
        self.accepted = None
        self.expected = ""
        self.partial_once = False
        self.p["teamOur"]["playerTasks"] = [
            {"taskType": "自进化类" + str(i), "taskPosition": point(x, y),
             "coldDownRounds": 0, "isValid": True, "timeoutRounds": 25,
             "goldReward": 80, "scoreReward": 80}
            for i, (x, y) in enumerate(((14, 14), (17, 17)) if left else ((23, 14), (27, 17)), 1)]

    def step(self, automatic=True, **feedback):
        self.p.update(feedback)
        before = time.perf_counter()
        response = self.agent.decide(self.p)
        self.times.append(time.perf_counter() - before)
        self.assert_wire(response)
        commands = response["roleCommandMap"]
        own = commands.get(self.rid, {})
        event = own.get("action", "wait")
        if response["prompt"]:
            event = "prompt"
        if response["executeCmd"]:
            event = "executeCmd"
        self.events.append((self.p["roundNo"], event))
        # Reuse the unchanged survival simulator for all non-task effects.
        stripped = copy.deepcopy(response)
        stripped["prompt"] = stripped["executeCmd"] = ""
        if own.get("action") in ("acceptTask", "submitAnswer"):
            stripped["roleCommandMap"].pop(self.rid)
        self.sim.step(stripped)
        self.p.update(llmResp="", lastCmdResult="", errors=[])
        if own:
            self.p["lastRoundRoleActionResults"][self.rid] = True
        for task in self.p["teamOur"]["playerTasks"]:
            task["coldDownRounds"] = max(0, task["coldDownRounds"] - 1)
            task["isValid"] = task["coldDownRounds"] == 0
        if automatic:
            if own.get("action") == "acceptTask":
                self.accepted = next(t for t in self.p["teamOur"]["playerTasks"] if self.near(t))
                self.number += 1
                self.expected = json.dumps({"token": "dynamic-%d" % self.number}, separators=(",", ":"))
                self.p["phaseTask"] = "Read job_%d.md and obtain its token via the documented sandbox API." % self.number
            if response["prompt"]:
                if self.p.get("phaseTask"):
                    # First inspect via command; answer only after command output reaches prompt.
                    queried = "lastCmdResult" in response["prompt"]
                    self.p["llmResp"] = json.dumps(
                        {"taskAnswer": self.expected, "experience": "Read job file, query its API, extract token."}
                        if queried else {"executeCmd": "cat job_%d.md" % self.number})
            if response["executeCmd"]:
                self.p["lastCmdResult"] = "[exitCode:0]\n" + self.expected
            if own.get("action") == "submitAnswer":
                if self.partial_once:
                    self.partial_once = False
                    self.p["errors"] = [{"errorCode": 2, "description": "部分字段错误"}]
                elif own["taskAnswer"] == self.expected:
                    self.p["phaseTask"] = ""
                    self.accepted.update(coldDownRounds=30, isValid=False)
                    self.p["teamOur"]["goldNum"] += self.accepted["goldReward"]
                else:
                    self.p["errors"] = [{"errorCode": 2, "description": "wrong"}]
        return response

    def near(self, task):
        target = task["taskPosition"]
        kind = next(z["neutralType"] for z in self.p["mapInfo"]["zones"] if z["pos"] == target)
        return any(d(self.actor["pos"], z["pos"]) == 1 for z in self.p["mapInfo"]["zones"] if z["neutralType"] == kind)

    def assert_wire(self, response):
        check(self.p, response)
        assert isinstance(response["prompt"], str) and isinstance(response["executeCmd"], str)
        assert not (response["prompt"] and response["executeCmd"])
        if response["executeCmd"]:
            assert self.p.get("phaseTask")
        own = response["roleCommandMap"].get(self.rid, {})
        if own.get("action") in ("acceptTask", "submitAnswer"):
            assert self.actor["health"] > 0
            assert any(self.near(t) for t in self.p["teamOur"]["playerTasks"])
        if own.get("action") == "acceptTask":
            assert not self.p.get("phaseTask")
            assert any(self.near(t) and t["isValid"] and t["coldDownRounds"] == 0 for t in self.p["teamOur"]["playerTasks"])
        if own.get("action") == "submitAnswer":
            assert self.p.get("phaseTask") and isinstance(own["taskAnswer"], str)
        for cmd in response["roleCommandMap"].values():
            if cmd.get("action") == "attack":
                assert cmd["controllerId"] not in response["roleCommandMap"]

    def reach(self, stage):
        for _ in range(35):
            s = self.agent.memory.get("tasks", {}).get("active")
            if s and s["stage"] == stage:
                return
            self.step()
        raise AssertionError("stage not reached: " + stage)


class TaskTests(unittest.TestCase):
    def test_http_multiround_task_protocol(self):
        server = AgentServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            class HttpAgent:
                @property
                def memory(self):
                    return server.agent.memory

                def decide(self, request):
                    body = json.dumps(request).encode("utf-8")
                    req = Request("http://127.0.0.1:%d/turn" % server.server_port, body,
                                  {"Content-Type": "application/json"})
                    with urlopen(req, timeout=5) as response:
                        return json.load(response)
            sim = TaskRollout(); sim.agent = HttpAgent()
            for _ in range(26):
                sim.step()
            self.assertEqual(2, sum(h["reason"] == "completed_inferred" for h in sim.agent.memory["tasks"]["history"]))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_two_sides_complete_multiple_tasks_with_real_movement(self):
        for left in (False, True):
            sim = TaskRollout(left)
            for _ in range(50):
                sim.step()
            history = sim.agent.memory["tasks"]["history"]
            self.assertGreaterEqual(sum(h["reason"] == "completed_inferred" for h in history), 2)
            actions = [e for _, e in sim.events]
            for action in ("move", "acceptTask", "prompt", "executeCmd", "submitAnswer"):
                self.assertIn(action, actions)
            self.assertLess(max(sim.times), 5)
            self.assertTrue(sim.agent.memory["tasks"]["experience"])

    def test_duplicate_round_returns_defensive_copy_without_advancing(self):
        sim = TaskRollout(); sim.reach("llm")
        response = sim.agent.decide(sim.p)
        state = copy.deepcopy(sim.agent.memory)
        self.assertEqual(response, sim.agent.decide(copy.deepcopy(sim.p)))
        response["roleCommandMap"].clear()
        self.assertEqual(state, sim.agent.memory)
        self.assertTrue(sim.agent.decide(sim.p)["roleCommandMap"])

    def test_wrong_or_partial_answer_is_revised_then_confirmed(self):
        sim = TaskRollout(); sim.reach("submit")
        sim.p["phaseTask"] = sim.agent.memory["tasks"]["active"]["description"]
        response = sim.step(False, errors=[{"errorCode": 2, "description": "partial"}])
        self.assertIn("部分正确", response["prompt"])
        response = sim.step(False, llmResp=json.dumps({"taskAnswer": {"token": "corrected"}}))
        self.assertEqual("submitAnswer", response["roleCommandMap"][sim.rid]["action"])
        sim.step(False, phaseTask="", errors=[])
        self.assertEqual("completed_inferred", sim.agent.memory["tasks"]["history"][-1]["reason"])

    def test_legal_submit_alone_is_not_success(self):
        sim = TaskRollout(); sim.reach("submit")
        description = sim.agent.memory["tasks"]["active"]["description"]
        sim.step(False, phaseTask=description)
        self.assertFalse(sim.agent.memory["tasks"]["history"])

    def test_terminal_partial_is_not_claimed_success(self):
        sim = TaskRollout(); sim.reach("submit")
        sim.step(False, phaseTask="", errors=[{"errorCode": 2, "description": "partial"}])
        h = sim.agent.memory["tasks"]["history"][-1]
        self.assertEqual("partial_or_wrong_ended", h["reason"])
        self.assertIsNone(h["passRate"])

    def test_sandbox_errors_and_truncation_are_given_to_solver(self):
        for output in ("[TIMEOUT]\npartial", "[JUDGER_ERROR]\nfailed", "[exitCode:1]\nerror", "[exitCode:0]\npartial\n[TRUNCATED]"):
            sim = TaskRollout(); sim.reach("cmd")
            response = sim.step(False, lastCmdResult=output)
            self.assertIn(output.split("\n")[0], response["prompt"])
            self.assertIn("不能将部分输出", response["prompt"])
            self.assertNotEqual("submitAnswer", response["roleCommandMap"].get(sim.rid, {}).get("action"))

    def test_quota_error_stops_even_during_exemption(self):
        sim = TaskRollout(); sim.reach("llm")
        response = sim.step(False, errors=[{"errorCode": 5}])
        self.assertEqual("", response["prompt"])
        self.assertEqual("llm_quota_error", sim.agent.memory["tasks"]["history"][-1]["reason"])

    def test_timeout_and_cancel(self):
        for feedback, reason in (({"errors": [{"errorCode": 1}]}, "timeout"), ({"phaseTask": ""}, "ended_unconfirmed")):
            sim = TaskRollout(); sim.reach("llm")
            sim.step(False, **feedback)
            self.assertEqual(reason, sim.agent.memory["tasks"]["history"][-1]["reason"])

    def test_death_revive_and_rollback_isolation(self):
        sim = TaskRollout(); sim.reach("llm")
        sim.actor["health"] = 0
        sim.step(False, phaseTask="")
        self.assertEqual("death", sim.agent.memory["tasks"]["history"][-1]["reason"])
        sim.actor["health"] = 200
        sim.p["roundNo"] = 151
        sim.step()
        self.assertIsNotNone(sim.agent.memory["tasks"]["active"])
        sim.p["roundNo"] = 1
        sim.step(False, phaseTask="")
        self.assertEqual([], sim.agent.memory["tasks"]["history"])
        sim.p["teamOur"]["teamId"] = "new-match"
        sim.step(False, phaseTask="")
        self.assertEqual({}, sim.agent.memory["tasks"]["experience"])

    def test_cooldown_exhausted_and_missing_timeout_are_not_accepted(self):
        for updates in ({"isValid": False}, {"coldDownRounds": 10}, {"timeoutRounds": 0}):
            sim = TaskRollout()
            for task in sim.p["teamOur"]["playerTasks"]:
                task.update(updates)
            response = sim.agent.decide(sim.p)
            self.assertFalse(sim.agent.memory["tasks"]["active"])
            self.assertFalse(response["prompt"])

    def test_holds_position_despite_voucher_and_budget(self):
        sim = TaskRollout(); sim.reach("llm")
        sim.actor["backpack"] = ["StationUpgradeVoucher1"]
        sim.p["teamOur"]["goldNum"] = 500
        response = sim.step(False, llmResp="")
        self.assertNotIn(sim.rid, response["roleCommandMap"])
        self.assertTrue(any(c["action"] != "acceptTask" for c in response["roleCommandMap"].values()))

    def test_dusk_explicitly_interrupts_and_returns(self):
        sim = TaskRollout()
        for task in sim.p["teamOur"]["playerTasks"]:
            task["timeoutRounds"] = 60
        sim.reach("llm")
        sim.p["roundNo"] = 67
        response = sim.step(False)
        self.assertEqual("defense_return_deadline", sim.agent.memory["tasks"]["history"][-1]["reason"])
        self.assertEqual("move", response["roleCommandMap"][sim.rid]["action"])
        self.assertFalse(response["prompt"] or response["executeCmd"])

    def test_night_cooldown_never_releases_gunner(self):
        from tests.test_strategy import StrategyTests
        for cooldown in (0, 3):
            p = StrategyTests().armed()
            p["teamOur"]["playerTasks"] = TaskRollout().p["teamOur"]["playerTasks"]
            for r in p["teamOur"]["roles"]:
                if r["roleType"] == "rocket":
                    r["cooldown"] = cooldown
            response = Agent().decide(p)
            check(p, response)
            self.assertNotIn("20011", response["roleCommandMap"])

    def test_bad_llm_output_has_finite_retries(self):
        sim = TaskRollout(); sim.reach("llm")
        for _ in range(30):
            sim.step(False, llmResp="not json")
        self.assertEqual("insufficient_rounds", sim.agent.memory["tasks"]["history"][-1]["reason"])

    def test_missing_results_and_skipped_round_do_not_use_stale_answer(self):
        sim = TaskRollout(); sim.reach("llm")
        sim.p["roundNo"] += 2
        response = sim.step(False, llmResp=json.dumps({"taskAnswer": "stale"}))
        self.assertTrue(response["prompt"])
        self.assertNotIn(sim.rid, response["roleCommandMap"])
        response = sim.step(False, llmResp="")
        self.assertFalse(response["prompt"])
        response = sim.step(False, llmResp="")
        self.assertTrue(response["prompt"])

    def test_failed_accept_is_backed_off_and_no_command_is_run(self):
        sim = TaskRollout(); sim.reach("accept")
        response = sim.step(False, phaseTask="", lastRoundRoleActionResults={sim.rid: False})
        self.assertFalse(response["prompt"] or response["executeCmd"])
        self.assertEqual("accept_rejected", sim.agent.memory["tasks"]["history"][-1]["reason"])

    def test_all_actors_die_state_still_records_death(self):
        sim = TaskRollout(); sim.reach("llm")
        for r in sim.p["teamOur"]["roles"]:
            if r["roleType"] in ("worker", "pioneer"):
                r["health"] = 0
        response = sim.step(False, phaseTask="")
        self.assertFalse(response["roleCommandMap"])
        self.assertEqual("death", sim.agent.memory["tasks"]["history"][-1]["reason"])

    def test_side_switch_clears_task_and_learned_method(self):
        sim = TaskRollout(); sim.reach("submit")
        other = TaskRollout(True)
        sim.agent.decide(other.p)
        self.assertFalse(sim.agent.memory["tasks"]["history"])
        self.assertFalse(sim.agent.memory["tasks"]["experience"])

    def test_point_two_uses_both_cells(self):
        sim = TaskRollout()
        sim.p["teamOur"]["playerTasks"] = [sim.p["teamOur"]["playerTasks"][1]]
        sim.actor["pos"] = point(25, 16)  # Adjacent to 26,17; two away from metadata 27,17.
        response = sim.step()
        self.assertEqual("acceptTask", response["roleCommandMap"][sim.rid]["action"])


if __name__ == "__main__":
    unittest.main()
