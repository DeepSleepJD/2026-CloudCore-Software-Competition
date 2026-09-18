"""Regressions for the three September replay losses and their task income."""
import unittest

from agent.logistics import upgrade_goal, shopping_goal
from agent.model import World, Layout
from agent.strategy import Agent
from tests.support import check, point, unit
from tests.test_survival import defended
from tests.test_tasks import TaskRollout


class InvestmentTests(unittest.TestCase):
    def test_main_then_support_launchers_then_front_wall(self):
        p = defended()
        rockets = [r for r in p["teamOur"]["roles"] if r["roleType"] == "rocket"]
        rockets[0]["level"] = 2
        self.assertEqual("WeaponUpgradeVoucher2", upgrade_goal(World(p))[0])
        rockets[0]["level"] = 3
        for r in rockets[1:]:
            r["level"] = 2
        name, wall = upgrade_goal(World(p))
        self.assertEqual("WallUpgradeVoucher1", name)
        self.assertIn(wall.p, Layout.for_world(World(p)).walls[:6])

    def test_batch_remaining_level_two_launchers_and_keep_repair_money(self):
        p = defended(261, 220)
        p["teamOur"]["roles"][0]["pos"] = point(24, 19)
        response = Agent().decide(p)
        check(p, response)
        self.assertEqual({"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 2},
                         response["roleCommandMap"]["20010"])
        purchases = [c for c in response["roleCommandMap"].values() if c["action"] == "buy"]
        self.assertFalse(any(c.get("name", "").startswith("Station") for c in purchases))

    def test_single_trip_can_buy_level_one_and_two_vouchers(self):
        p = defended(131, 170)
        for r in p["teamOur"]["roles"]:
            if r["roleType"] == "rocket":
                r["level"] = 1
        p["teamOur"]["roles"][0].update(pos=point(24, 19), backpack=["WeaponUpgradeVoucher1"])
        response = Agent().decide(p)
        check(p, response)
        self.assertEqual("WeaponUpgradeVoucher2", response["roleCommandMap"]["20010"].get("name"))
        self.assertEqual(1, response["roleCommandMap"]["20010"]["num"])

    def test_virtual_cart_does_not_buy_past_three_launchers(self):
        p = defended()
        goal, levels = shopping_goal(World(p), ["WeaponUpgradeVoucher1"] * 2)
        self.assertEqual("WallUpgradeVoucher1", goal[0])
        self.assertEqual([2, 2], sorted(levels.values()))

    def test_no_spending_unconfirmed_task_reward(self):
        p = defended(261, 90)
        p["teamOur"]["roles"][0]["pos"] = point(24, 19)
        p["teamOur"]["playerTasks"] = TaskRollout().p["teamOur"]["playerTasks"]
        response = Agent().decide(p)
        check(p, response)
        self.assertFalse(any(c["action"] == "buy" and "UpgradeVoucher" in c.get("name", "")
                             for c in response["roleCommandMap"].values()))

    def test_front_wall_upgrade_interleaves_with_second_max_launcher(self):
        p = defended()
        for r in p["teamOur"]["roles"]:
            if r["roleType"] == "rocket" and r["level"] == 1:
                r["level"] = 2
        for _ in range(2):
            name, target = upgrade_goal(World(p))
            self.assertEqual("WallUpgradeVoucher1", name)
            next(r for r in p["teamOur"]["roles"] if r["id"] == target.id).update(level=2, health=1500)
        self.assertEqual("WeaponUpgradeVoucher2", upgrade_goal(World(p))[0])

    def test_repair_worker_stays_with_partial_stock_and_missing_wall(self):
        p = defended(331, 500)
        p["teamOur"]["roles"][0].update(pos=point(29, 9), backpack=["WallFixer"])
        p["teamOur"]["roles"].pop()  # A cap missing must not disable the front post.
        p["robot"]["roles"] = [unit(30000, "largeRobot", 26, 9, health=500, targetTeam="defender")]
        response = Agent().decide(p)
        check(p, response)
        self.assertNotIn("20010", response["roleCommandMap"])

    def test_local_repair_outranks_upgrade_shopping(self):
        p = defended(331, 500)
        p["teamOur"]["roles"][0].update(pos=point(29, 9), backpack=["WallFixer"])
        wall = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "wall" and r["pos"] == point(28, 9))
        wall["health"] = 500
        response = Agent().decide(p)
        check(p, response)
        self.assertEqual("WallFixer", response["roleCommandMap"]["20010"].get("name"))

    def test_cleared_night_can_complete_task_on_both_sides(self):
        for left in (False, True):
            sim = TaskRollout(left)
            sim.p["roundNo"] = 101
            for _ in range(25):
                sim.step()
            self.assertTrue(any(h["reason"] == "completed_inferred"
                                for h in sim.agent.memory["tasks"]["history"]))

    def test_spawn_window_and_distant_own_robot_keep_gunner_home(self):
        for round_no, robots in ((71, []), (101, [unit(30000, "bossRobot", 2, 2, health=800, targetTeam="defender")]),
                                 (101, [unit(30000, "bossRobot", 2, 2, health=800)])):
            p = defended(round_no)
            p["teamOur"]["playerTasks"] = TaskRollout().p["teamOur"]["playerTasks"]
            p["robot"]["roles"] = robots
            agent = Agent()
            response = agent.decide(p)
            check(p, response)
            self.assertIsNone(agent.memory["tasks"]["active"])

    def test_reappearing_threat_interrupts_night_task(self):
        sim = TaskRollout()
        sim.p["roundNo"] = 101
        sim.reach("llm")
        sim.p["robot"]["roles"] = [unit(30000, "bossRobot", 2, 2, health=800, targetTeam="defender")]
        response = sim.step(False)
        self.assertEqual("defense_return_deadline", sim.agent.memory["tasks"]["history"][-1]["reason"])
        self.assertFalse(response["prompt"] or response["executeCmd"])


if __name__ == "__main__":
    unittest.main()
