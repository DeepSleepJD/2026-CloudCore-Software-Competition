"""Regressions derived from the survival report and champion repair positions."""
import unittest
from agent.model import World, Layout
from agent.strategy import Agent, Planner
from tests.support import initial, unit, point, check, EconomyRollout


def defended(round_no=261, gold=276, left=False):
    request = initial(left)
    request["roundNo"] = round_no
    request["teamOur"]["goldNum"] = gold
    layout = Layout.for_world(World(request))
    roles = request["teamOur"]["roles"]
    prefix = 10000 if left else 20000
    roles[1]["pos"] = point(*layout.operator)
    for i, (x, y) in enumerate(layout.towers):
        level = 3 if i == 0 else 1
        roles.append(unit(prefix + 40 + i, "rocket", x, y, level=level,
                          attackRange=2147483647 if level == 3 else 10))
    for i, (x, y) in enumerate(layout.walls):
        roles.append(unit(40000 + i, "wall", x, y))
    return request


class SurvivalTests(unittest.TestCase):
    def test_base_upgrade_has_deadline_before_large_robot_nights(self):
        p = defended()
        self.assertEqual("StationUpgradeVoucher1", Planner(World(p), {}).upgrade_order())
        p["roundNo"] = 391
        p["teamOur"]["roles"][3].update(level=2, health=3000)
        self.assertEqual("StationUpgradeVoucher2", Planner(World(p), {}).upgrade_order())

    def test_worker_can_buy_base_upgrade_at_night_while_pioneer_fires(self):
        p = defended(331)
        p["teamOur"]["roles"][0]["pos"] = point(24, 19)
        p["teamOur"]["roles"][2]["pos"] = point(34, 14)
        p["robot"]["roles"] = [unit(30000, "smallRobot", 23, 8, health=40, targetTeam="defender")]
        response = Agent().decide(p)
        check(p, response)
        commands = response["roleCommandMap"]
        self.assertEqual("StationUpgradeVoucher1", commands["20010"].get("name"))
        self.assertTrue(any(c["action"] == "attack" for c in commands.values()))

    def test_night_repair_from_inside_is_not_cancelled_by_retreat(self):
        p = defended(331, 0)
        worker = p["teamOur"]["roles"][0]
        worker.update(pos=point(29, 9), backpack=["WallFixer", "WallFixer"])
        wall = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "wall" and r["pos"] == point(28, 9))
        wall["health"] = 250
        p["robot"]["roles"] = [unit(30000, "largeRobot", 26, 9, health=500, targetTeam="defender")]
        response = Agent().decide(p)
        check(p, response)
        self.assertEqual({"action":"use", "name":"WallFixer", "targetPos":[point(28, 9)]},
                         response["roleCommandMap"]["20010"])

    def test_prebuy_repair_batch_before_night(self):
        p = defended(315, 90)
        p["teamOur"]["roles"][0]["pos"] = point(24, 19)
        response = Agent().decide(p)
        check(p, response)
        purchase = response["roleCommandMap"]["20010"]
        self.assertEqual("WallFixer", purchase.get("name"))
        self.assertGreaterEqual(purchase.get("num", 0), 2)

    def test_sell_small_batch_when_it_unlocks_upgrade(self):
        p = defended(261, 90)
        p["teamOur"]["roles"][0].update(pos=point(22, 16), backpack=["copper", "copper"])
        response = Agent().decide(p)
        check(p, response)
        self.assertEqual("move", response["roleCommandMap"]["20010"]["action"])
        target = response["roleCommandMap"]["20010"]["targetPos"][0]
        self.assertLessEqual(max(abs(target["x"] - 20), abs(target["y"] - 16)), 1)

    def test_no_duplicate_strategic_voucher_when_carried(self):
        p = defended()
        p["teamOur"]["roles"][0].update(pos=point(29, 10), backpack=["StationUpgradeVoucher1"])
        p["teamOur"]["roles"][2]["pos"] = point(24, 19)
        response = Agent().decide(p)
        check(p, response)
        commands = response["roleCommandMap"].values()
        self.assertTrue(any(c["action"] == "use" and c.get("name") == "StationUpgradeVoucher1" for c in commands))
        self.assertFalse(any(c["action"] == "buy" and c.get("name") == "StationUpgradeVoucher1" for c in commands))

    def test_saved_cash_converts_to_base_levels_with_workers_in_doorway(self):
        sim, agent = EconomyRollout(), Agent()
        p = sim.request = defended(261, 276)
        p["mapInfo"]["zones"] = [z for z in p["mapInfo"]["zones"]
                                   if z["neutralType"] not in {"stone", "iron", "copper"}]
        for _ in range(100):
            sim.step(agent.decide(p))
        station = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "station")
        self.assertEqual(3, station["level"])

    def test_healthy_repair_worker_keeps_post_until_wall_needs_repair(self):
        p = defended(331, 0)
        p["teamOur"]["roles"][0].update(pos=point(29, 9), backpack=["WallFixer"] * 4)
        p["robot"]["roles"] = [unit(30000, "largeRobot", 26, 9, health=500, targetTeam="defender")]
        response = Agent().decide(p)
        check(p, response)
        self.assertNotIn("20010", response["roleCommandMap"])


if __name__ == "__main__":
    unittest.main()
