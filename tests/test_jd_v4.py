"""Risk-based scenarios plus generated action-contract checks for the JD policy."""
from copy import deepcopy
import json
import tempfile
import time
import unittest

from test_agent import unit
from agent.strategy import Agent, Planner
from agent.model import World, distance
from agent.state import Memory
from agent.news import News


def scene(round_no=1, gold=75):
    return {"roundNo": round_no, "mapInfo": {"width": 41, "height": 32, "zones": [
        {"neutralType": "stone", "pos": {"x": 33, "y": 14}},
        {"neutralType": "copper", "pos": {"x": 37, "y": 14}},
        {"neutralType": "iron", "pos": {"x": 35, "y": 17}},
        {"neutralType": "vendor", "pos": {"x": 20, "y": 16}},
        {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}},
        {"neutralType": "defenderTaskPoint1", "pos": {"x": 23, "y": 14}}]},
        "teamOur": {"teamId": "test-jd", "type": "defender", "goldNum": gold, "playerTasks": [], "roles": [
            unit(20013, "station", (30, 10), hp=1500),
            unit(20010, "worker", (30, 8), hp=220),
            unit(20012, "worker", (31, 8), hp=220),
            unit(20011, "pioneer", (32, 10), hp=200),
            unit(20040, "rocket", (32, 9), level=3),
            unit(20041, "rocket", (31, 11), level=2),
            unit(20042, "rocket", (32, 11), level=2)]},
        "teamEnemy": {"roles": [unit(10013, "station", (9, 22), hp=1500)]},
        "robot": {"roles": []}, "phaseTask": "", "errors": [],
        "lastRoundRoleActionResults": {}, "llmResp": "", "lastCmdResult": "",
        "vendorShopList": [{"name": k, "price": p} for k, p in [("stone", 1), ("iron", 3), ("copper", 5)]],
        "weaponShopList": [{"name": k, "price": p} for k, p in [("WeaponUpgradeVoucher1", 100),
            ("WeaponUpgradeVoucher2", 150), ("WallUpgradeVoucher1", 20), ("WallUpgradeVoucher2", 30),
            ("StationUpgradeVoucher1", 100), ("StationUpgradeVoucher2", 150), ("WallFixer", 10),
            ("Medicine", 10), ("AcientTablet", 15), ("StarSand", 15), ("FlameBreath", 15)]]}


def robot(uid=31000, point=(20, 10), team="defender", hp=800):
    return unit(uid, "bossRobot", point, hp=hp, targetTeam=team, attackPower=40)


class JDTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent = Agent(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def planner(self, data, memory=None):
        w = World(data)
        memory = memory or Memory()
        memory.observe(w)
        return Planner(w, {}, memory)

    def test_foreign_wave_is_not_targeted_even_when_in_range(self):
        d = scene(71)
        d["robot"]["roles"] = [robot(team="challenger")]
        self.assertFalse(any(c["action"] == "attack" for c in self.agent.decide(d)["roleCommandMap"].values()))

    def test_mixed_wave_only_aims_at_own_wave(self):
        d = scene(71)
        d["robot"]["roles"] = [robot(team="challenger", point=(20, 10)), robot(32000, (20, 20))]
        cmds = self.agent.decide(d)["roleCommandMap"]
        attack = next(c for c in cmds.values() if c["action"] == "attack")
        self.assertTrue(all(distance((p["x"], p["y"]), (20, 20)) <= 1 for p in attack["targetPos"]))

    def test_unknown_wave_needs_motion_or_immediate_threat(self):
        d = scene(71)
        d["robot"]["roles"] = [robot(team="", point=(20, 10))]
        result = self.agent.decide(d)
        self.assertFalse(any(c["action"] == "attack" for c in result["roleCommandMap"].values()))
        for n, x in [(72, 21), (73, 22)]:
            d["roundNo"] = n
            d["robot"]["roles"][0]["pos"]["x"] = x
            result = self.agent.decide(d)
        self.assertTrue(any(c["action"] == "attack" for c in result["roleCommandMap"].values()))

    def test_explicit_foreign_robot_at_base_can_be_stopped(self):
        d = scene(71)
        d["robot"]["roles"] = [robot(team="challenger", point=(27, 9))]
        self.assertTrue(any(c["action"] == "attack" for c in self.agent.decide(d)["roleCommandMap"].values()))

    def test_local_critical_wall_repaired_before_retreat(self):
        d = scene(90)
        d["teamOur"]["roles"][1].update(pos={"x": 29, "y": 9}, backpack=["WallFixer"])
        d["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=200))
        d["robot"]["roles"] = [robot(point=(27, 9))]
        action = self.agent.decide(d)["roleCommandMap"]["20010"]
        self.assertEqual(action["action"], "use")
        self.assertEqual(action["name"], "WallFixer")

    def test_dying_carrier_heals_before_repair(self):
        d = scene(90)
        d["teamOur"]["roles"][1].update(pos={"x": 29, "y": 9}, health=30, backpack=["WallFixer", "Medicine"])
        d["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=200))
        d["robot"]["roles"] = [robot(point=(27, 9))]
        self.assertEqual(self.agent.decide(d)["roleCommandMap"]["20010"]["name"], "Medicine")

    def test_healthy_repair_carrier_can_approach_threatened_wall(self):
        d = scene(90)
        d["teamOur"]["roles"][1].update(pos={"x": 29, "y": 12}, backpack=["WallFixer"])
        d["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=300))
        d["robot"]["roles"] = [robot(point=(27, 9))]
        action = self.agent.decide(d)["roleCommandMap"]["20010"]
        self.assertEqual(action["action"], "move")
        p = action["targetPos"][0]
        self.assertLess(distance((p["x"], p["y"]), (28, 9)), 3)

    def test_wall_budget_prevents_weapon_purchase(self):
        d = scene(20, gold=110)
        d["teamOur"]["roles"][3]["pos"] = {"x": 24, "y": 20}
        d["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=200))
        p = self.planner(d)
        p.memory.pressure[41000] = 100
        self.assertEqual(p.defence_need()[0], "WallUpgradeVoucher1")
        self.assertEqual(p.defence_reserve(), 20)
        self.assertFalse(p.purchase(p.w.pioneer, "WeaponUpgradeVoucher1", reserve=p.defence_reserve()))

    def test_repair_is_cheaper_bridge_to_dawn(self):
        d = scene(128)
        d["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=200))
        p = self.planner(d)
        p.memory.pressure[41000] = 100
        self.assertEqual(p.defence_need()[0], "WallFixer")

    def test_same_round_and_in_transit_procurement_not_duplicated(self):
        d = scene(20, 500)
        d["teamOur"]["roles"][1]["pos"] = {"x": 24, "y": 20}
        d["teamOur"]["roles"][2]["pos"] = {"x": 24, "y": 19}
        p = self.planner(d)
        self.assertTrue(p.purchase(p.w.workers[0], "WallFixer"))
        self.assertFalse(p.purchase(p.w.workers[1], "WallFixer"))

    def test_pioneer_accepts_available_task_instead_of_waiting(self):
        d = scene(10)
        d["teamOur"]["roles"][3]["pos"] = {"x": 24, "y": 14}
        d["teamOur"]["playerTasks"] = [{"taskType": "自进化类1", "isValid": True, "timeoutRounds": 15}]
        self.assertEqual(self.agent.decide(d)["roleCommandMap"]["20011"]["action"], "acceptTask")

    def test_task_llm_command_answer_pipeline_and_worker_relief(self):
        d = scene(71)
        d["phaseTask"] = "Read the task file and return its token."
        d["teamOur"]["roles"][3]["pos"] = {"x": 24, "y": 14}
        d["teamOur"]["roles"][1]["pos"] = {"x": 32, "y": 10}
        d["robot"]["roles"] = [robot()]
        r = self.agent.decide(d)
        self.assertTrue(r["prompt"])
        self.assertNotIn("20011", r["roleCommandMap"])
        shot = next(c for c in r["roleCommandMap"].values() if c["action"] == "attack")
        self.assertEqual(shot["controllerId"], "20010")
        d.update(roundNo=72, llmResp=json.dumps({"action": "command", "command": "cat task.md"}))
        self.assertEqual(self.agent.decide(d)["executeCmd"], "cat task.md")
        d.update(roundNo=73, llmResp="", lastCmdResult="[exitCode:0]\nReturn token abc")
        self.assertIn("Return token abc", self.agent.decide(d)["prompt"])
        d.update(roundNo=74, llmResp=json.dumps({"action": "answer", "answer": {"token": "abc"}}))
        action = self.agent.decide(d)["roleCommandMap"]["20011"]
        self.assertEqual(action["action"], "submitAnswer")
        self.assertEqual(json.loads(action["taskAnswer"]), {"token": "abc"})
        d.update(roundNo=75, phaseTask="", llmResp="", lastRoundRoleActionResults={"20011": True})
        result = self.agent.decide(d)
        shot = next(c for c in result["roleCommandMap"].values() if c["action"] == "attack")
        self.assertEqual(shot["controllerId"], "20010", "relief must not leave before pioneer returns")

    def test_task_exhaustion_exits_instead_of_freezing(self):
        d = scene(20)
        d["phaseTask"] = "unknown task"
        d["teamOur"]["roles"][3]["pos"] = {"x": 24, "y": 14}
        self.agent.decide(d)
        self.agent.missions.runner.config.task_llm_budget = 1
        d["roundNo"] += 1
        action = self.agent.decide(d)["roleCommandMap"]["20011"]
        self.assertEqual(action["action"], "move")

    def test_duplicate_request_does_not_consume_llm_quota(self):
        d = scene(20)
        d["worldNews"] = {"officialNews": "铁矿明天停采两天，价格将上涨。"}
        first = self.agent.decide(d)
        self.assertTrue(first["prompt"])
        self.assertEqual(first, self.agent.decide(deepcopy(d)))
        self.assertEqual(self.agent.missions.news.calls, 1)

    def news_plan(self):
        return {"market": [{"ore": "iron", "closed_from": 3, "closed_until": 4,
                            "sell_from": 3, "hold_until": 4, "evidence": ["铁矿明天停采两天"]}],
                "treasure": {"position": {"x": 3, "y": 3}, "day": 8, "phase": "day",
                             "items": ["AcientTablet", "StarSand", "FlameBreath"], "confidence": 0.95,
                             "evidence": ["第八日白昼三三石门", "古符石板星辰之沙烈焰之息"]}}

    def test_news_validation_budget_and_market_dates(self):
        news = News()
        w = World(scene(131))
        news.history = [{"day": 2, "text": "铁矿明天停采两天；第八日白昼三三石门；古符石板星辰之沙烈焰之息"}]
        news.accept(self.news_plan(), w)
        self.assertTrue(news.closed("iron", 3))
        self.assertFalse(news.closed("iron", 5))
        self.assertTrue(news.hold("iron", 2))
        self.assertFalse(news.hold("iron", 3))
        self.assertEqual(news.treasure["position"], (3, 3))
        for _ in range(3):
            news.revision += 1
            self.assertTrue(news.prompt(w))
            news.pending = False
        news.revision += 1
        self.assertEqual(news.prompt(w), "")
        invalid = self.news_plan()
        invalid["treasure"]["evidence"] = ["invented quote"]
        other = News()
        other.accept(invalid, w)
        self.assertIsNone(other.treasure)

    def test_treasure_exact_items_and_success_stop(self):
        d = scene(930)
        d["teamOur"]["roles"][3].update(pos={"x": 4, "y": 3}, backpack=["AcientTablet", "StarSand", "FlameBreath"])
        self.agent.decide(d)
        news = self.agent.missions.news
        news.treasure = {"position": (3, 3), "day": 8, "phase": "day", "items": ["AcientTablet", "StarSand", "FlameBreath"]}
        d["roundNo"] += 1
        action = self.agent.decide(d)["roleCommandMap"]["20011"]
        self.assertEqual(action["action"], "summonTreasure")
        self.assertEqual(len(action["item"]), 3)
        d.update(roundNo=932, lastSummonTreasureResult=1)
        self.agent.decide(d)
        self.assertTrue(news.treasure_done)
        d["roundNo"] += 1
        self.assertFalse(any(c["action"] == "summonTreasure" for c in self.agent.decide(d)["roleCommandMap"].values()))

    def test_wrong_material_feedback_cancels_treasure_plan(self):
        news = News()
        news.treasure = {"anything": True}
        news.awaiting_treasure = True
        d = scene(930)
        d["lastSummonTreasureResult"] = 3
        news.observe(World(d))
        self.assertIsNone(news.treasure)

    def test_news_holds_only_surplus_and_avoids_closed_mine(self):
        d = scene(131, 200)
        d["teamOur"]["roles"][1].update(pos={"x": 19, "y": 16}, backpack=["iron"] * 12)
        self.agent.decide(d)
        self.agent.missions.news.market = {"iron": {"sell_from": 3, "hold_until": 4}}
        w = World(d)
        p = Planner(w, {}, self.agent.memory, self.agent.missions)
        self.assertFalse(p.sell(w.workers[0]))
        p.budget = 0
        self.assertTrue(p.sell(w.workers[0]))
        self.assertEqual(p.commands["20010"]["action"], "sell")
        d = scene(261, 200)
        d["mapInfo"]["zones"] = [{"neutralType": "iron", "pos": {"x": 29, "y": 8}}]
        self.agent.missions.news.market["iron"].update(closed_from=3, closed_until=4)
        p = Planner(World(d), {}, self.agent.memory, self.agent.missions)
        self.assertFalse(p.mine(p.w.workers[0]))

    def test_null_fields_do_not_silently_empty_the_turn(self):
        d = scene()
        d.update(robot=None, teamEnemy=None, vendorShopList=None, weaponShopList=None)
        d["teamOur"]["playerTasks"] = None
        for u in d["teamOur"]["roles"]:
            u.update(backpack=None, cooldown=None)
        self.assertTrue(self.agent.decide(d)["roleCommandMap"])

    def validate(self, d, result):
        w = World(d)
        ours = {u.id: u for u in w.ours}
        used, destinations = set(), set()
        spent = 0
        self.assertEqual(set(result), {"roleCommandMap", "prompt", "executeCmd"})
        for key, cmd in result["roleCommandMap"].items():
            actor = ours[int(key)]
            uid = int(cmd.get("controllerId", key))
            self.assertNotIn(uid, used)
            used.add(uid)
            if cmd["action"] == "attack":
                self.assertFalse(w.day)
                self.assertEqual(distance(ours[uid].p, actor.p), 1)
                self.assertEqual(len(cmd["targetPos"]), actor.level)
                for p in cmd["targetPos"]:
                    self.assertLessEqual(distance((p["x"], p["y"]), actor.p), actor.reach)
            if cmd["action"] in {"move", "build"}:
                p = cmd["targetPos"][0]
                point = p["x"], p["y"]
                self.assertTrue(w.inside(point))
                self.assertEqual(distance(actor.p, point), 1)
                self.assertNotIn(point, w.blocked | destinations)
                destinations.add(point)
            if cmd["action"] == "build":
                self.assertTrue(w.day)
                if cmd["name"] == "wall":
                    self.assertIn("stone", actor.bag)
                else:
                    spent += 25
            if cmd["action"] == "buy":
                spent += w.shop[cmd["name"]] * cmd["num"]
                self.assertTrue(any(k == "weaponShop" and distance(actor.p, p) == 1 for p, k in w.zones.items()))
            if cmd["action"] == "sell":
                self.assertLessEqual(cmd["num"], actor.bag.count(cmd["name"]))
        self.assertLessEqual(spent, w.gold)

    def test_generated_contract_matrix_both_sides(self):
        # Boundary values, ownership and mirror variants; no brute-force seed fishing.
        for n in [1, 69, 71, 129]:
            for gold in [0, 20, 75, 110, 300]:
                for team in ["defender", "challenger", ""]:
                    for mirror in [False, True]:
                        with self.subTest(round=n, gold=gold, target=team, mirror=mirror):
                            d = scene(n, gold)
                            d["robot"]["roles"] = [robot(team=team)] if n >= 71 else []
                            if mirror:
                                d["teamOur"]["type"] = "challenger"
                                for block in [d["teamOur"], d["teamEnemy"], d["robot"]]:
                                    for u in block["roles"]:
                                        p = u["pos"]
                                        p["x"], p["y"] = 40 - p["x"], 31 - p["y"]
                                        if u["roleType"] == "station":
                                            p["x"] -= 1
                                            p["y"] += 1
                                for z in d["mapInfo"]["zones"]:
                                    z["pos"] = {"x": 40-z["pos"]["x"], "y": 31-z["pos"]["y"]}
                            result = Agent(self.temp.name).decide(d)
                            self.validate(d, result)

    def test_heavy_wave_within_http_decision_deadline(self):
        d = scene(981)
        d["robot"]["roles"] = [robot(31000+i, (i % 20, 1+i // 20)) for i in range(160)]
        start = time.perf_counter()
        result = self.agent.decide(d)
        self.assertLess(time.perf_counter()-start, 4)
        self.validate(d, result)

    def test_continuous_economy_tasks_and_gunner_with_two_resource_seeds(self):
        from jd_harness import Harness
        for seed in (7, 42):
            with self.subTest(seed=seed):
                d = scene()
                d["teamOur"]["roles"] = d["teamOur"]["roles"][:4]
                d["teamOur"]["roles"][3]["pos"] = {"x": 32, "y": 8}
                h = Harness(d, seed)
                agent = Agent(self.temp.name)
                for n in range(1, 261):
                    h.step(agent, n)
                self.assertGreaterEqual(h.actions["submitAnswer"], 2)
                self.assertGreater(h.actions["sell"], 0)
                self.assertGreater(h.actions["use"], 0)
                self.assertTrue(h.guard_ready)
                self.assertEqual(len([u for u in h.data["teamOur"]["roles"] if u["roleType"] == "rocket"]), 3)


if __name__ == "__main__":
    unittest.main()
