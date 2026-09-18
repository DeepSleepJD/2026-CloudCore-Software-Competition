import json
import tempfile
import threading
import unittest
from collections import Counter
from urllib.request import Request, urlopen

from test_agent import payload, unit
from zk_agent.baseline import BaselineAgent
from zk_agent.config import Config
from zk_agent.__main__ import make_server
from zk_agent.world import World, pos, distance


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config.baseline(state_dir=self.temp.name)
        self.agent = BaselineAgent(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def formation(self, round_no=71):
        data = payload(round_no, towers=False)
        data["teamOur"]["type"] = "defender"
        data["teamOur"]["playerTasks"] = []
        data["teamOur"]["goldNum"] = 0
        data["teamOur"]["roles"] = [
            unit(20013, "station", (30, 10), hp=1500),
            unit(20010, "worker", (35, 20), hp=220),
            unit(20012, "worker", (36, 20), hp=220),
            unit(20011, "pioneer", (32, 10), hp=200),
            unit(20040, "rocket", (32, 9), level=2, cooldown=0),
            unit(20041, "rocket", (31, 11), level=2, cooldown=0),
            unit(20042, "rocket", (32, 11), level=3, cooldown=0)]
        return data

    def world(self, data):
        w = World(data)
        if self.agent.identity is None:
            self.agent.reset(w, "test")
        self.agent.build_sites, self.agent.wall_sites = self.agent.layout(w)
        self.agent.duty_groups = self.agent.assign(w)
        self.agent.assignments = {uid: g[0] for uid, g in self.agent.duty_groups.items()}
        self.agent.claimed_jobs = set()
        return w

    def test_default_runtime_uses_baseline(self):
        cfg = Config.load(None)
        self.assertEqual(cfg.profile, "baseline")
        self.assertEqual(cfg.tower_loadout, ["rocket"] * 3)
        self.assertEqual(cfg.initial_towers, 3)
        self.assertFalse(cfg.enable_raids)
        self.assertEqual(cfg.pvp_mode, "off")

    def test_observed_formation_and_rotated_common_stand(self):
        d = self.formation()
        right, walls = self.agent.layout(World(d))
        self.assertEqual(right, [(32, 9), (31, 11), (32, 11)])
        self.assertIn((28, 9), walls)
        d["teamOur"]["roles"][0]["pos"] = {"x": 9, "y": 22}
        left, _ = self.agent.layout(World(d))
        towers = [unit(i, "rocket", p) for i, p in enumerate(left)]
        self.assertIn((8, 21), self.agent.group_stands(towers))

    def test_one_operator_fires_all_three_fifteen_times(self):
        data = self.formation()
        data["robot"]["roles"] = [unit(30000, "bossRobot", (24, 10), hp=10000,
                                               targetTeam="defender", attackPower=40)]
        shots = Counter()
        for n in range(71, 131):
            data["roundNo"] = n
            result = self.agent.decide(data)["roleCommandMap"]
            attacks = [(uid, c) for uid, c in result.items() if c["action"] == "attack"]
            self.assertLessEqual(len(attacks), 1)
            for uid, c in attacks:
                self.assertEqual(c["controllerId"], "20011")
                self.assertNotIn("20011", result)
                shots[uid] += 1
            for u in data["teamOur"]["roles"]:
                if u["roleType"] == "rocket":
                    u["cooldown"] = 3 if str(u["id"]) in dict(attacks) else max(0, u["cooldown"] - 1)
        self.assertEqual(dict(shots), {"20042": 15, "20040": 15, "20041": 15})

    def test_mine_trip_keeps_target_when_another_price_changes(self):
        data = self.formation(10)
        data["mapInfo"]["zones"] = [
            {"neutralType": "copper", "pos": {"x": 34, "y": 22}},
            {"neutralType": "iron", "pos": {"x": 40, "y": 25}},
            {"neutralType": "vendor", "pos": {"x": 30, "y": 25}}]
        data["vendorShopList"].append({"name": "iron", "price": 1})
        w = self.world(data)
        self.agent.mine_or_sell(w, w.workers[0])
        chosen = self.agent.mine_jobs[20010]
        data["vendorShopList"][-1]["price"] = 100
        w = self.world(data)
        self.agent.mine_or_sell(w, w.workers[0])
        self.assertEqual(chosen, self.agent.mine_jobs[20010])
        data["mapInfo"]["zones"] = data["mapInfo"]["zones"][1:]
        w = self.world(data)
        self.agent.mine_or_sell(w, w.workers[0])
        self.assertNotEqual(chosen, self.agent.mine_jobs[20010])

    def test_sell_trip_finishes_mixed_bag_below_batch_size(self):
        data = self.formation(10)
        actor = data["teamOur"]["roles"][1]
        actor["backpack"] = ["copper"] * 8 + ["stone"] * 2
        self.agent.mine_or_sell(self.world(data), World(data).workers[0])
        self.assertIn(20010, self.agent.selling)
        actor["backpack"] = ["stone"] * 2
        actor["pos"] = {"x": 11, "y": 12}
        w = self.world(data)
        self.assertTrue(self.agent.mine_or_sell(w, w.workers[0]))
        self.assertEqual(w.commands["20010"], {"action": "sell", "name": "stone", "num": 2})

    def test_low_health_carrier_uses_medicine_before_delivery(self):
        data = self.formation(10)
        data["teamOur"]["roles"][1].update(health=30, backpack=["Medicine", "WallFixer"])
        result = self.agent.decide(data)["roleCommandMap"]
        self.assertEqual(result["20010"], {"action": "use", "name": "Medicine"})

    def test_repair_carrier_returns_to_critical_wall(self):
        data = self.formation(900)
        data["teamOur"]["roles"][1]["backpack"] = ["WallFixer"] * 2
        data["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=200))
        w = self.world(data)
        before = distance(pos(w.workers[0]), (28, 9))
        self.assertTrue(self.agent.repair_delivery(w, w.workers[0]))
        target = w.commands["20010"]["targetPos"][0]
        self.assertLess(distance((target["x"], target["y"]), (28, 9)), before)

    def test_team_supply_stock_prevents_duplicate_purchase(self):
        data = self.formation(200)
        data["teamOur"]["goldNum"] = 500
        data["teamOur"]["roles"][1]["pos"] = {"x": 12, "y": 13}
        data["teamOur"]["roles"][2]["backpack"] = ["WallFixer"] * 2
        data["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=200))
        data["weaponShopList"] = [{"name": "WallFixer", "price": 10}]
        w = self.world(data)
        self.assertFalse(self.agent.buy_supplies(w, w.workers[0]))
        self.assertEqual(w.commands, {})

    def test_incoming_damage_triggers_repair_before_fixed_threshold(self):
        data = self.formation(900)
        data["teamOur"]["roles"][1].update(pos={"x": 29, "y": 9}, backpack=["WallFixer"])
        data["teamOur"]["roles"].append(unit(41000, "wall", (28, 9), hp=600))
        w = self.world(data)
        self.agent.damage = {41000: 245}
        self.assertTrue(self.agent.maintain(w, w.workers[0]))
        self.assertEqual(w.commands["20010"]["name"], "WallFixer")

    def test_base_attacking_boss_outweighs_remote_small_cluster(self):
        data = self.formation()
        data["robot"]["roles"] = [unit(30000, "bossRobot", (27, 9), hp=800,
                                                      targetTeam="defender", attackPower=40)]
        data["robot"]["roles"] += [unit(30010 + i, "smallRobot", (20 + i % 3, 15 + i // 3),
                hp=40, attackPower=5, targetTeam="defender") for i in range(9)]
        result = self.agent.decide(data)["roleCommandMap"]
        attack = next(c for c in result.values() if c["action"] == "attack")
        self.assertEqual(attack["targetPos"], [{"x": 27, "y": 9}] * 3)

    def test_full_request_over_http_uses_baseline(self):
        server = make_server(0, self.config, host="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = self.formation()
            data["robot"]["roles"] = [unit(30000, "bossRobot", (24, 10), hp=800, targetTeam="defender")]
            req = Request(f"http://127.0.0.1:{server.server_port}/", json.dumps(data).encode(),
                          {"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as response:
                result = json.load(response)
            self.assertEqual(result["roleCommandMap"]["20042"]["controllerId"], "20011")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_first_day_builds_three_rockets_and_seats_one_guard(self):
        # Basic movement/economy only. Does not simulate robots, task rewards or combat.
        data = self.formation(1)
        team = data["teamOur"]
        team["goldNum"] = 75
        team["roles"] = team["roles"][:4]
        for actor, point in zip(team["roles"][1:], [(30, 8), (31, 8), (32, 8)]):
            actor["pos"] = {"x": point[0], "y": point[1]}
        data["weaponShopList"] = []
        data["mapInfo"]["zones"] = [
            {"neutralType": "stone", "pos": {"x": 33, "y": 14}},
            {"neutralType": "copper", "pos": {"x": 37, "y": 14}},
            {"neutralType": "vendor", "pos": {"x": 20, "y": 16}}]
        counts = Counter()
        for turn in range(1, 71):
            data["roundNo"] = turn
            reply = self.agent.decide(data)["roleCommandMap"]
            by_id = {str(u["id"]): u for u in team["roles"]}
            for uid, cmd in reply.items():
                actor = by_id[uid]
                counts[cmd["action"]] += 1
                if cmd["action"] == "move":
                    actor["pos"] = cmd["targetPos"][0]
                elif cmd["action"] == "build":
                    target = cmd["targetPos"][0]
                    if cmd["name"] == "wall":
                        actor["backpack"].remove("stone")
                    else:
                        team["goldNum"] -= 25
                    self.assertGreaterEqual(team["goldNum"], 0)
                    team["roles"].append(unit(50000 + len(team["roles"]), cmd["name"],
                                               (target["x"], target["y"])))
                elif cmd["action"] == "collect":
                    target = cmd["targetPos"][0]
                    zone = next(z for z in data["mapInfo"]["zones"] if z["pos"] == target)
                    actor["backpack"].append(zone["neutralType"])
                elif cmd["action"] == "sell":
                    for _ in range(cmd["num"]):
                        actor["backpack"].remove(cmd["name"])
                    price = next(v["price"] for v in data["vendorShopList"] if v["name"] == cmd["name"])
                    team["goldNum"] += price * cmd["num"]
            data["lastRoundRoleActionResults"] = {uid: True for uid in reply}
        w = World(data)
        self.assertEqual(len(w.towers), 3)
        self.assertGreater(counts["collect"], 10)
        self.assertGreater(counts["sell"], 0)
        self.assertEqual(len(self.agent.duty_groups), 1)
        guard = next(u for u in w.people if u["id"] in self.agent.duty_groups)
        self.assertTrue(all(distance(pos(guard), pos(t)) <= 1 for t in w.towers))


if __name__ == "__main__":
    unittest.main()
