import tempfile
import unittest

from test_agent import payload, unit
from zk_agent.config import Config
from zk_agent.strategy import Agent
from zk_agent.world import World, around, footprint, pos


class ActiveStrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(state_dir=self.temp.name, third_tower_reserve=10000)
        self.agent = Agent(self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def surround_towers(data):
        occupied = set().union(*(footprint(u) for u in data["teamOur"]["roles"]))
        for tower in list(data["teamOur"]["roles"]):
            if tower["roleType"] not in {"rocket", "gatling"}:
                continue
            for point in around(pos(tower)):
                if point not in occupied:
                    data["teamOur"]["roles"].append(unit(40000 + len(occupied), "wall", point))
                    occupied.add(point)

    def test_unreachable_towers_do_not_freeze_workers_in_daylight(self):
        data = payload(20)
        data["teamOur"]["roles"][1]["pos"] = {"x": 18, "y": 12}
        data["teamOur"]["roles"][2]["pos"] = {"x": 19, "y": 12}
        self.surround_towers(data)
        commands = self.agent.decide(data)["roleCommandMap"]
        for uid in ("10010", "10012"):
            self.assertIn(uid, commands)
            self.assertIn(commands[uid]["action"], {"move", "collect", "remove", "sell"})

    def test_missing_stone_falls_back_to_copper(self):
        data = payload(20)
        data["mapInfo"]["zones"] = [z for z in data["mapInfo"]["zones"] if z["neutralType"] != "stone"]
        data["teamOur"]["roles"][1]["pos"] = {"x": 8, "y": 6}
        data["teamOur"]["goldNum"] = 0
        action = self.agent.decide(data)["roleCommandMap"]["10010"]
        self.assertEqual(action["action"], "collect")
        self.assertEqual(action["targetPos"], [{"x": 9, "y": 6}])

    def test_completed_front_wall_releases_builder_to_economy(self):
        data = payload(20)
        data["teamOur"]["roles"][1]["pos"] = {"x": 8, "y": 6}
        data["teamOur"]["goldNum"] = 0
        w = World(data)
        _, walls = self.agent.layout(w)
        for index, point in enumerate(walls):
            data["teamOur"]["roles"].append(unit(40000 + index, "wall", point))
        action = self.agent.decide(data)["roleCommandMap"]["10010"]
        self.assertEqual(action["action"], "collect")
        self.assertEqual(action["targetPos"], [{"x": 9, "y": 6}])

    def test_unreachable_vendor_does_not_block_other_jobs(self):
        data = payload(20)
        data["teamOur"]["roles"][1]["pos"] = {"x": 8, "y": 6}
        data["teamOur"]["roles"][1]["backpack"] = ["copper"] * 8
        data["teamOur"]["goldNum"] = 0
        self.cfg.wall_limit = 0
        for index, point in enumerate(around((12, 12))):
            data["teamOur"]["roles"].append(unit(41000 + index, "wall", point))
        action = self.agent.decide(data)["roleCommandMap"]["10010"]
        self.assertEqual(action["action"], "collect")

    def test_front_walls_mirror_between_corners(self):
        left = payload()
        left["teamOur"]["roles"][0]["pos"] = {"x": 10, "y": 24}
        _, walls_left = self.agent.layout(World(left))
        self.assertTrue(all(x == 13 or y == 21 for x, y in walls_left))
        right = payload()
        right["teamOur"]["roles"][0]["pos"] = {"x": 29, "y": 8}
        _, walls_right = self.agent.layout(World(right))
        mirrored_offsets = {(1 - (x - 10), -1 - (y - 24)) for x, y in walls_left}
        self.assertEqual(mirrored_offsets, {(x - 29, y - 8) for x, y in walls_right})
        self.assertEqual(len(walls_left), self.cfg.wall_limit)

    def test_rocket_cooldown_is_used_for_adjacent_mining(self):
        data = payload(73)
        self.cfg.pvp_mode = "off"
        data["teamOur"]["roles"][4]["cooldown"] = 2
        data["mapInfo"]["zones"].append({"neutralType": "copper", "pos": {"x": 3, "y": 6}})
        action = self.agent.decide(data)["roleCommandMap"]["10010"]
        self.assertEqual(action["action"], "collect")

    def test_two_rockets_rotate_under_one_worker(self):
        data = payload(71)
        self.cfg.pvp_mode = "off"
        data["teamOur"]["roles"] = [unit(10013, "station", (5, 6), hp=1500),
            unit(10010, "worker", (6, 7), hp=220), unit(10012, "worker", (7, 4), hp=220),
            unit(10011, "pioneer", (10, 9), hp=200),
            unit(10040, "rocket", (7, 6), level=1, cooldown=0),
            unit(10041, "rocket", (7, 7), level=1, cooldown=0),
            unit(10020, "gatling", (6, 4), level=1, cooldown=0)]
        data["robot"]["roles"] = [unit(30000, "bossRobot", (10, 7), hp=800, targetTeam="challenger")]
        fires = []
        for offset, (a, b) in enumerate([(0, 0), (3, 0), (2, 3), (1, 2), (0, 1), (3, 0)]):
            data["roundNo"] = 71 + offset
            data["teamOur"]["roles"][4]["cooldown"] = a
            data["teamOur"]["roles"][5]["cooldown"] = b
            commands = self.agent.decide(data)["roleCommandMap"]
            shots = [(uid, cmd["controllerId"]) for uid, cmd in commands.items()
                     if uid in {"10040", "10041"} and cmd["action"] == "attack"]
            self.assertLessEqual(len(shots), 1)
            fires += shots
            for _, controller in shots:
                self.assertNotIn(controller, commands)
        self.assertEqual(fires, [("10040", "10010"), ("10041", "10010"),
                                ("10040", "10010"), ("10041", "10010")])

    def test_pioneer_delivers_upgrades_while_no_task_is_available(self):
        data = payload(20)
        data["teamOur"]["playerTasks"] = []
        data["teamOur"]["roles"][3]["backpack"] = ["WeaponUpgradeVoucher2"]
        data["teamOur"]["roles"][4]["level"] = 2
        self.assertEqual(self.agent.decide(data)["roleCommandMap"]["10011"]["action"], "move")

    def test_exhausted_task_budget_exits_instead_of_waiting_for_timeout(self):
        data = payload(20)
        data["phaseTask"] = "测试任务：无法完成"
        self.cfg.task_llm_budget = 1
        self.agent.decide(data)
        data["roundNo"] += 1
        result = self.agent.decide(data)
        action = result["roleCommandMap"]["10011"]
        self.assertEqual(action["action"], "move")
        p = action["targetPos"][0]
        self.assertGreater(max(abs(p["x"] - 11), abs(p["y"] - 9)), 1)
        self.assertEqual(result["prompt"], "")

    def test_rejected_collect_switches_target(self):
        data = payload(20)
        data["teamOur"]["roles"][1]["pos"] = {"x": 8, "y": 6}
        data["teamOur"]["goldNum"] = 0
        self.cfg.wall_limit = 0
        first = self.agent.decide(data)["roleCommandMap"]["10010"]
        self.assertEqual(first["action"], "collect")
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"10010": False}
        second = self.agent.decide(data)["roleCommandMap"]["10010"]
        self.assertNotEqual(first, second)

    def test_raids_do_not_purchase_without_surplus(self):
        data = payload(261)
        data["teamOur"]["playerTasks"] = []
        data["teamOur"]["roles"][0]["level"] = 2
        data["teamOur"]["roles"][0]["health"] = 3000
        data["teamOur"]["roles"][3]["pos"] = {"x": 13, "y": 12}
        data["weaponShopList"] = [{"name": "LargeRobotSummonOrder", "price": 100}]
        data["teamOur"]["goldNum"] = 120
        commands = self.agent.decide(data)["roleCommandMap"]
        self.assertFalse(any(c.get("action") == "buy" and c.get("name") == "LargeRobotSummonOrder" for c in commands.values()))

    def test_surplus_can_buy_a_raid_order(self):
        data = payload(261)
        data["teamOur"]["playerTasks"] = []
        data["teamOur"]["roles"][0].update(level=2, health=3000)
        data["teamOur"]["roles"][3]["pos"] = {"x": 13, "y": 12}
        data["weaponShopList"] = [{"name": "LargeRobotSummonOrder", "price": 100}]
        data["teamOur"]["goldNum"] = 250
        action = self.agent.decide(data)["roleCommandMap"]["10011"]
        self.assertEqual(action, {"action": "buy", "name": "LargeRobotSummonOrder", "num": 1})

    def test_scouting_only_after_pvp_evidence_and_safe_defence(self):
        data = payload(20)
        data["teamOur"]["playerTasks"] = []
        data["teamOur"]["goldNum"] = 0
        data["teamOur"]["roles"][3]["pos"] = {"x": 20, "y": 20}
        self.agent.decide(data)
        self.agent.pvp = "supported"
        data["roundNo"] += 1
        action = self.agent.decide(data)["roleCommandMap"]["10011"]
        self.assertEqual(action["action"], "move")
        self.assertGreaterEqual(action["targetPos"][0]["x"], 20)

    def test_no_pointless_attack_or_walk_when_guard_has_no_useful_action(self):
        data = payload(73)
        self.cfg.pvp_mode = "off"
        self.agent.decide(data)
        self.assertIn(10010, self.agent.idle)
        self.assertGreater(self.agent.idle[10010], 0)

    def test_owned_wall_can_be_removed_to_restore_operator_access(self):
        data = payload(20)
        data["teamOur"]["roles"][1]["pos"] = {"x": 2, "y": 5}
        self.surround_towers(data)
        w = World(data)
        self.agent.reset(w, "test:challenger")
        self.agent.wall_sites = []
        self.agent.duty_groups = {10010: [next(t for t in w.towers if t["roleType"] == "rocket")]}
        self.assertTrue(self.agent.open_exit(w, w.workers[0]))
        action = w.commands["10010"]
        self.assertEqual(action["action"], "remove")
        target = action["targetPos"][0]
        self.assertIn((target["x"], target["y"]), {pos(t) for t in w.walls})

    def test_summon_daily_limit_is_not_exceeded(self):
        data = payload(20)
        w = World(data)
        self.agent.reset(w, "test:challenger")
        self.agent.summons_used = 10
        w.workers[0]["backpack"] = ["LargeRobotSummonOrder"]
        self.assertFalse(self.agent.use_summon(w, w.workers[0]))
        self.assertEqual(w.commands, {})


if __name__ == "__main__":
    unittest.main()
