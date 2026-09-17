from collections import Counter
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from zk_agent.__main__ import make_server
from zk_agent.config import Config
from zk_agent.strategy import Agent
from zk_agent.tasks import TaskRunner, MARKER, skill_command
from zk_agent.world import World, command, distance, pos


def unit(uid, kind, point, hp=1000, level=1, **extra):
    return {"id": uid, "roleType": kind, "pos": {"x": point[0], "y": point[1]},
            "health": hp, "level": level, "backpack": [], "backPackCapability": 100, **extra}


def payload(round_no=1, towers=True):
    ours = [unit(10013, "station", (5, 6), hp=1500),
            unit(10010, "worker", (4, 6), hp=220),
            unit(10012, "worker", (7, 6), hp=220),
            unit(10011, "pioneer", (10, 9), hp=200)]
    if towers:
        ours += [unit(10040, "rocket", (4, 5), level=3, cooldown=0),
                 unit(10020, "gatling", (7, 5), level=1, cooldown=0)]
    return {"roundNo": round_no,
            "mapInfo": {"width": 41, "height": 32, "zones": [
                {"neutralType": "challengerTaskPoint1", "pos": {"x": 11, "y": 9}},
                {"neutralType": "vendor", "pos": {"x": 12, "y": 12}},
                {"neutralType": "weaponShop", "pos": {"x": 13, "y": 13}},
                {"neutralType": "copper", "pos": {"x": 9, "y": 6}},
                {"neutralType": "stone", "pos": {"x": 3, "y": 4}}]},
            "teamOur": {"type": "challenger", "teamId": "test", "goldNum": 75,
                        "roles": ours, "playerTasks": [{"taskType": "自进化类1", "isValid": True,
                            "coldDownRounds": 0, "timeoutRounds": 100, "scoreReward": 50}]},
            "teamEnemy": {"roles": [unit(20013, "station", (30, 25), hp=1500)]},
            "robot": {"roles": []}, "phaseTask": "", "llmResp": "", "lastCmdResult": "",
            "lastRoundRoleActionResults": {}, "errors": [],
            "vendorShopList": [{"name": "stone", "price": 1}, {"name": "copper", "price": 5}],
            "weaponShopList": [{"name": "WeaponUpgradeVoucher1", "price": 100},
                               {"name": "WeaponUpgradeVoucher2", "price": 150},
                               {"name": "StationUpgradeVoucher1", "price": 100}]}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(state_dir=self.temp.name)
        self.agent = Agent(self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_request_and_idempotency(self):
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "docs/request.txt").read_text(encoding="utf-8"))
        result = self.agent.decide(data)
        self.assertEqual(result, self.agent.decide(data))
        self.assertEqual(set(result), {"roleCommandMap", "prompt", "executeCmd"})

    def test_shared_gold_cannot_be_spent_twice(self):
        data = payload(towers=False)
        data["teamOur"]["goldNum"] = 25
        data["teamOur"]["roles"][1]["pos"] = {"x": 7, "y": 6}
        data["teamOur"]["roles"][2]["pos"] = {"x": 7, "y": 4}
        result = self.agent.decide(data)
        builds = [v for v in result["roleCommandMap"].values() if v["action"] == "build" and v["name"] != "wall"]
        self.assertEqual(len(builds), 1)

    def test_world_reserves_movement_and_blocks_enemy(self):
        data = payload()
        w = World(data)
        first, second = w.workers
        target = (5, 7)
        self.assertTrue(w.put(first, command("move", [target])))
        self.assertFalse(w.put(second, command("move", [target])))
        self.assertIn((31, 24), w.blocked)  # Enemy station is a 2x2 obstacle.
        self.assertFalse(w.put(first, command("collect", [(3, 4)])))

    def test_return_uses_path_length_not_just_distance(self):
        data = payload(round_no=69)
        data["teamOur"]["roles"][1]["pos"] = {"x": 18, "y": 6}
        result = self.agent.decide(data)
        action = result["roleCommandMap"]["10010"]
        self.assertEqual(action["action"], "move")
        self.assertLess(distance((action["targetPos"][0]["x"], action["targetPos"][0]["y"]), (4, 5)), 14)

    def test_probe_has_three_identical_targets_and_no_double_action(self):
        result = self.agent.decide(payload(71))
        shot = result["roleCommandMap"]["10040"]
        self.assertEqual(len(shot["targetPos"]), 3)
        self.assertTrue(all(p == {"x": 30, "y": 25} for p in shot["targetPos"]))
        self.assertNotIn(shot["controllerId"], result["roleCommandMap"])

    def test_failed_probe_disables_auto_attack(self):
        data = payload(71)
        self.agent.decide(data)
        data["roundNo"] = 72
        data["lastRoundRoleActionResults"] = {"10040": False}
        result = self.agent.decide(data)
        self.assertEqual(self.agent.pvp, "unsupported")
        self.assertNotIn("10040", result["roleCommandMap"])

    def test_clean_probe_observes_damage(self):
        data = payload(71)
        self.agent.decide(data)
        data["roundNo"] = 72
        data["teamEnemy"]["roles"][0]["health"] = 1440
        data["lastRoundRoleActionResults"] = {"10040": True}
        self.agent.decide(data)
        self.assertEqual(self.agent.pvp, "supported")

    def test_robot_damage_does_not_confirm_probe(self):
        data = payload(71)
        data["robot"]["roles"] = [unit(30000, "smallRobot", (29, 25), hp=40, targetTeam="defender")]
        self.agent.decide(data)
        data["roundNo"] = 72
        data["teamEnemy"]["roles"][0]["health"] = 1495
        self.agent.decide(data)
        self.assertEqual(self.agent.pvp, "unknown")

    def test_defence_preempts_probe_and_rocket_uses_splash(self):
        data = payload(71)
        data["robot"]["roles"] = [unit(30000, "smallRobot", (8, 7), hp=40, targetTeam="challenger"),
                                  unit(30001, "smallRobot", (8, 8), hp=40, targetTeam="challenger")]
        result = self.agent.decide(data)
        shot = result["roleCommandMap"]["10040"]
        self.assertTrue(all(p["x"] < 12 for p in shot["targetPos"]))
        self.assertIsNone(self.agent.probe)

    def test_cooldown_prevents_fire(self):
        data = payload(71)
        data["teamOur"]["roles"][4]["cooldown"] = 2
        result = self.agent.decide(data)
        self.assertNotIn("10040", result["roleCommandMap"])

    def test_active_task_keeps_pioneer_at_point_even_during_recall(self):
        data = payload(71)
        self.agent.decide(data)
        self.agent.recall = True
        data["roundNo"] = 72
        data["phaseTask"] = "任务测试，请计算42"
        data["teamOur"]["roles"].append(unit(10041, "rocket", (6, 7)))
        result = self.agent.decide(data)
        self.assertTrue(result["prompt"])
        self.assertNotIn("10011", result["roleCommandMap"])
        self.assertFalse(any(c.get("controllerId") == "10011" for c in result["roleCommandMap"].values()))

    def test_night_base_upgrade_is_available(self):
        data = payload(71)
        data["teamOur"]["roles"][0]["health"] = 500
        data["teamOur"]["roles"][1]["backpack"] = ["StationUpgradeVoucher1"]
        result = self.agent.decide(data)
        self.assertEqual(result["roleCommandMap"]["10010"]["name"], "StationUpgradeVoucher1")
        self.assertNotIn("10040", result["roleCommandMap"])

    def test_task_llm_answer_cross_turn(self):
        data = payload()
        data["phaseTask"] = "测试任务：返回42"
        result = self.agent.decide(data)
        self.assertTrue(result["prompt"])
        data["roundNo"] = 2
        data["llmResp"] = '```json\n{"action":"answer","answer":{"value":42}}\n```'
        result = self.agent.decide(data)
        self.assertEqual(json.loads(result["roleCommandMap"]["10011"]["taskAnswer"]), {"value": 42})
        self.assertFalse(result["prompt"])

    def test_cache_only_after_verified_answer_and_reuses_in_two_turns(self):
        data = payload()
        data["phaseTask"] = "加法测试：1+2"
        self.agent.decide(data)
        data["roundNo"] = 2
        data["llmResp"] = json.dumps({"action": "skill", "match": ["加法测试"],
            "python": "def solve(task):\n import re\n return sum(map(int,re.findall(r'\\d+',task)))"})
        result = self.agent.decide(data)
        self.assertIn("python3 -c", result["executeCmd"])
        self.assertEqual(self.agent.tasks.skills, [])
        data["roundNo"] = 3
        data["lastCmdResult"] = "[exitCode:0]\n" + MARKER + "3"
        result = self.agent.decide(data)
        self.assertEqual(result["roleCommandMap"]["10011"]["taskAnswer"], "3")
        data["roundNo"] = 4
        data["phaseTask"] = ""
        data["lastRoundRoleActionResults"] = {"10011": True}
        self.agent.decide(data)
        self.assertEqual(len(self.agent.tasks.skills), 1)
        data["roundNo"] = 5
        data["phaseTask"] = "加法测试：20+22"
        result = self.agent.decide(data)
        self.assertTrue(result["executeCmd"])
        self.assertFalse(result["prompt"])
        data["roundNo"] = 6
        data["lastCmdResult"] = "[exitCode:0]\n" + MARKER + "42"
        result = self.agent.decide(data)
        self.assertEqual(result["roleCommandMap"]["10011"]["taskAnswer"], "42")

    def test_timeout_does_not_parse_partial_solver_answer(self):
        runner = TaskRunner(self.cfg, "timeout")
        data = payload()
        data["phaseTask"] = "测试任务"
        runner.step(World(data))
        runner.pending = "skill"
        data["lastCmdResult"] = "[TIMEOUT]\n" + MARKER + "42"
        w = World(data)
        runner.step(w)
        self.assertNotIn("10011", w.commands)

    def test_generated_solver_shell_quoting(self):
        # Executes a fixed test fixture, never model-generated code on the host.
        task = "引号'\"、换行\n、$()和`必须原样传入"
        cmd = skill_command("def solve(task):\n return task", task)
        args = shlex.split(cmd)
        result = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True,
                                encoding="utf-8", timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.split(MARKER)[1]), task)

    def test_llm_budget_stops_invalid_response_loop(self):
        self.cfg.task_llm_budget = 2
        data = payload()
        data["phaseTask"] = "测试任务"
        for turn in range(1, 5):
            data["roundNo"] = turn
            result = self.agent.decide(data)
        self.assertEqual(self.agent.tasks.llm_calls, 2)
        self.assertFalse(result["prompt"])

    def test_no_sandbox_command_without_active_task(self):
        data = payload()
        data["llmResp"] = '{"action":"command","command":"echo should-not-run"}'
        self.assertFalse(self.agent.decide(data)["executeCmd"])

    def test_new_half_resets_pvp_evidence(self):
        self.agent.decide(payload(71))
        self.agent.pvp = "supported"
        self.agent.decide(payload(1))
        self.assertEqual(self.agent.pvp, "unknown")

    def test_defender_uses_actual_role_ids_and_own_task_point(self):
        data = payload()
        data["teamOur"]["type"] = "defender"
        for actor in data["teamOur"]["roles"]:
            actor["id"] += 10000
        data["mapInfo"]["zones"][0]["neutralType"] = "defenderTaskPoint1"
        result = self.agent.decide(data)
        self.assertEqual(result["roleCommandMap"]["20011"]["action"], "acceptTask")
        self.assertNotIn("10011", result["roleCommandMap"])

    def test_confirmed_pvp_can_target_visible_operator(self):
        data = payload(71)
        self.agent.decide(data)
        self.agent.pvp = "supported"
        data["roundNo"] = 72
        data["teamEnemy"]["roles"].append(unit(20010, "worker", (29, 24), hp=60))
        result = self.agent.decide(data)
        self.assertEqual(result["roleCommandMap"]["10040"]["targetPos"], [{"x": 29, "y": 24}] * 3)

    def test_many_robots_response_under_deadline(self):
        data = payload(71)
        data["robot"]["roles"] = [unit(30000 + i, "smallRobot", (12 + i % 25, 2 + i // 25), hp=40,
                                           targetTeam="challenger") for i in range(500)]
        self.cfg.pvp_mode = "off"
        started = time.monotonic()
        self.agent.decide(data)
        self.assertLess(time.monotonic() - started, 4)

    def test_first_day_economy_and_return_with_small_rule_harness(self):
        # Only checks documented basic actions and Demo ring geometry, not combat or win rate.
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "docs/request.txt").read_text(encoding="utf-8"))
        team = data["teamOur"]
        team["goldNum"] = 75
        team["roles"] = [u for u in team["roles"] if u["roleType"] in {"station", "worker", "pioneer"}]
        for u in team["roles"]:
            u["backpack"] = []
        team["playerTasks"] = []
        data["robot"]["roles"] = []
        data["errors"] = []
        mine_stock = {}
        actions = Counter()
        for turn in range(1, 71):
            data["roundNo"] = turn
            result = self.agent.decide(data)
            by_id = {str(u["id"]): u for u in team["roles"]}
            results = {}
            for uid, cmd in result["roleCommandMap"].items():
                actor = by_id[uid]
                action = cmd["action"]
                actions[action] += 1
                results[uid] = True
                points = cmd.get("targetPos", [])
                point = (points[0]["x"], points[0]["y"]) if points else None
                if action == "move":
                    self.assertEqual(distance(pos(actor), point), 1)
                    actor["pos"] = points[0]
                elif action == "build":
                    self.assertLessEqual(distance(pos(actor), point), 1)
                    if cmd["name"] == "wall":
                        actor["backpack"].remove("stone")
                    else:
                        self.assertGreaterEqual(team["goldNum"], 25)
                        team["goldNum"] -= 25
                    team["roles"].append(unit(50000 + turn * 3 + len(results), cmd["name"], point))
                elif action == "collect":
                    zone = next(z for z in data["mapInfo"]["zones"] if pos(z) == point)
                    self.assertLessEqual(distance(pos(actor), point), 1)
                    actor["backpack"].append(zone["neutralType"])
                    mine_stock[point] = mine_stock.get(point, 10) - 1
                    if mine_stock[point] == 0:
                        data["mapInfo"]["zones"].remove(zone)
                elif action == "sell":
                    prices = {s["name"]: s["price"] for s in data["vendorShopList"]}
                    for _ in range(cmd["num"]):
                        actor["backpack"].remove(cmd["name"])
                    team["goldNum"] += prices[cmd["name"]] * cmd["num"]
                elif action == "buy":
                    prices = {s["name"]: s["price"] for s in data["weaponShopList"]}
                    self.assertGreaterEqual(team["goldNum"], prices[cmd["name"]])
                    team["goldNum"] -= prices[cmd["name"]]
                    actor["backpack"].append(cmd["name"])
                elif action == "use":
                    actor["backpack"].remove(cmd["name"])
                    target = next(u for u in team["roles"] if pos(u) == point)
                    target["level"] = target.get("level", 1) + 1
            data["lastRoundRoleActionResults"] = results
        towers = [u for u in team["roles"] if u["roleType"] in {"rocket", "gatling", "railgun"}]
        self.assertEqual(len(towers), 2)
        self.assertGreater(actions["collect"], 0)
        for actor in [u for u in team["roles"] if u["roleType"] == "worker"]:
            self.assertTrue(any(distance(pos(actor), pos(t)) <= 1 for t in towers),
                            f"worker did not return: {pos(actor)}; towers={[pos(t) for t in towers]}")


class ServerTests(unittest.TestCase):
    def test_http_real_request_and_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(0, Config(state_dir=tmp), host="127.0.0.1")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/"
                raw = json.dumps(payload()).encode()
                with urlopen(Request(url, data=raw, headers={"Content-Type": "application/json"}), timeout=4) as result:
                    value = json.load(result)
                self.assertIn("roleCommandMap", value)
                with self.assertLogs("zk_agent.__main__", level="ERROR"):
                    with urlopen(Request(url, data=b"{broken"), timeout=4) as result:
                        value = json.load(result)
                self.assertEqual(value, {"roleCommandMap": {}, "prompt": "", "executeCmd": ""})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
