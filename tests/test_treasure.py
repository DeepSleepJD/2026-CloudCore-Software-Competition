import copy
import json
import unittest

from agent.model import World, Layout
from agent.strategy import Agent, Planner
from agent.treasure import TreasureScheduler
from tests.support import initial, point, unit, check


class TreasureTests(unittest.TestCase):
    def fixture(self):
        p = initial()
        p["weaponShopList"] += [{"name": "TestRelic", "price": 15}]
        p["worldNews"] = {"officialNews": "news", "folkLegends": "祭坛在24,20，第一天白天开放，仅需TestRelic一枚。"}
        actor = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "pioneer")
        actor["pos"] = point(24, 19)
        p["teamOur"]["goldNum"] = 500
        evidence = {k: [{"day": 1, "quote": p["worldNews"]["folkLegends"]}] for k in ("position", "window", "items")}
        plan = {"targetPos": point(24, 20), "item": ["TestRelic"], "openRound": 1,
                "closeRound": 70, "confidence": "high", "evidence": evidence}
        return p, actor, plan

    def scheduler(self, p, memory):
        planner = Planner(World(p), {}, memory)
        treasure = TreasureScheduler(planner)
        return planner, treasure

    def test_news_accumulates_and_quota_resets_per_day(self):
        p, _, _ = self.fixture(); memory = {}
        for turn in range(1, 6):
            p["roundNo"] = turn
            p["worldNews"]["folkLegends"] = "线索更新" + str(turn)
            _, t = self.scheduler(p, memory)
            prompt = t.infer(False)
            self.assertEqual(turn <= 3, bool(prompt))
        p["roundNo"] = 131
        p["worldNews"]["folkLegends"] = "第二天新线索"
        _, t = self.scheduler(p, memory)
        self.assertTrue(t.infer(False))
        self.assertEqual({1, 2}, {n["day"] for n in t.m["news"]})
        self.assertEqual({1: 3, 2: 1}, t.m["calls"])

    def test_task_calls_do_not_spend_daily_quota(self):
        p, _, _ = self.fixture(); _, t = self.scheduler(p, {})
        self.assertEqual("", t.infer(True))
        self.assertEqual({}, t.m["calls"])

    def test_evidence_exact_items_and_coordinates_are_required(self):
        p, _, plan = self.fixture(); _, t = self.scheduler(p, {})
        self.assertIsNotNone(t.validate(plan))
        for field, value in (("item", ["Medicine"]), ("item", ["Invented"]),
                             ("targetPos", point(99, 99)), ("confidence", "low"),
                             ("evidence", {}), ("openRound", 90)):
            bad = copy.deepcopy(plan); bad[field] = value
            self.assertIsNone(t.validate(bad))

    def test_buy_and_summon_then_consume_all_result_codes_once(self):
        for result in range(5):
            p, actor, plan = self.fixture(); memory = {}
            planner, t = self.scheduler(p, memory)
            self.assertTrue(t.infer(False))
            p["roundNo"] += 1; p["llmResp"] = json.dumps(plan)
            planner, t = self.scheduler(p, memory)
            self.assertFalse(t.infer(False))
            self.assertTrue(t.act(t.actor))
            self.assertEqual("buy", planner.commands[str(actor["id"])]["action"])
            actor["backpack"] = ["TestRelic"]
            p["roundNo"] += 1; p["llmResp"] = ""
            planner, t = self.scheduler(p, memory)
            self.assertTrue(t.act(t.actor))
            response = {"roleCommandMap": planner.commands, "prompt": "", "executeCmd": ""}
            check(p, response)
            self.assertEqual(["TestRelic"], planner.commands[str(actor["id"])]["item"])
            actor["backpack"] = []
            p["roundNo"] += 1; p["lastSummonTreasureResult"] = result
            planner, t = self.scheduler(p, memory)
            self.assertEqual(result, t.m["history"][-1]["result"])
            self.assertFalse(t.act(t.actor))
            self.assertIsNone(t.validate(plan))
            self.assertEqual(result in (1, 4), bool(t.m.get("done")))

    def test_budget_and_dusk_prevent_treasure_spending(self):
        p, _, plan = self.fixture(); memory = {}
        p["teamOur"]["goldNum"] = 100
        planner, t = self.scheduler(p, memory); t.m["plan"] = t.validate(plan)
        self.assertFalse(t.act(t.actor))
        self.assertEqual({}, planner.commands)
        p["teamOur"]["goldNum"] = 1000; p["roundNo"] = 65
        planner, t = self.scheduler(p, memory)
        self.assertFalse(t.act(t.actor))

    def test_future_opening_waits_and_new_evidence_invalidates(self):
        p, actor, plan = self.fixture(); memory = {}
        actor["backpack"] = ["TestRelic"]
        plan["openRound"] = 5
        planner, t = self.scheduler(p, memory); t.m["plan"] = t.validate(plan)
        self.assertTrue(t.act(t.actor)); self.assertFalse(planner.commands)
        p["worldNews"]["folkLegends"] += "但今天不开门。"
        _, t = self.scheduler(p, memory)
        self.assertTrue(t.infer(False))
        self.assertIsNone(t.m["plan"])

    def test_live_inference_does_not_override_task_prompt(self):
        from tests.test_tasks import TaskRollout
        sim = TaskRollout(); p, _, _ = self.fixture()
        sim.p["weaponShopList"] += [{"name": "TestRelic", "price": 15}]
        sim.p["worldNews"] = p["worldNews"]
        sim.reach("llm")
        self.assertEqual({}, sim.agent.memory["treasure"]["calls"])

    def test_repeated_news_on_new_day_is_not_new_sacrifice_evidence(self):
        p, _, _ = self.fixture(); memory = {}
        _, t = self.scheduler(p, memory)
        self.assertTrue(t.infer(False))
        p["roundNo"] = 131
        _, t = self.scheduler(p, memory)
        self.assertFalse(t.infer(False))

    def test_revived_pioneer_does_not_displace_night_worker(self):
        from tests.test_strategy import StrategyTests
        p = StrategyTests().armed()
        worker, pioneer = p["teamOur"]["roles"][:2]
        worker["pos"] = point(*Layout.for_world(World(p)).operator)
        pioneer["health"] = 0
        agent = Agent(); agent.decide(p)
        p["roundNo"] += 1
        pioneer["health"] = 200; pioneer["pos"] = point(35, 10)
        response = agent.decide(p); check(p, response)
        attacks = [c for c in response["roleCommandMap"].values() if c["action"] == "attack"]
        self.assertEqual(str(worker["id"]), attacks[0]["controllerId"])


if __name__ == "__main__":
    unittest.main()
