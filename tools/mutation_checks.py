"""Check that critical regression tests actually reject deliberate bad behavior.

In-memory patches only; never edits production files or the submitted package.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from test_jd_v4 import JDTests
from agent.strategy import Planner
from agent.combat import rocket_targets


def target_every_robot(world, tower):
    world.threats = world.robots
    return rocket_targets(world, tower)


def main():
    mutants = [
        ("shoot_foreign_wave", "agent.strategy.rocket_targets", target_every_robot,
         "test_foreign_wave_is_not_targeted_even_when_in_range"),
        ("spend_wall_budget_on_gun", "agent.strategy.Planner.defence_reserve", lambda self: 0,
         "test_wall_budget_prevents_weapon_purchase"),
        ("pioneer_never_accepts_tasks", "agent.missions.Missions.choose", lambda *args: False,
         "test_pioneer_accepts_available_task_instead_of_waiting"),
        ("retreat_instead_of_urgent_repair", "agent.strategy.Planner.urgent_local_supply", lambda *args: False,
         "test_local_critical_wall_repaired_before_retreat"),
    ]
    results = {}
    for name, target, bad, test in mutants:
        with patch(target, bad):
            result = unittest.TestResult()
            JDTests(test).run(result)
            results[name] = bool(result.failures or result.errors)
    print(json.dumps({"detected": sum(results.values()), "total": len(results), "mutants": results}, indent=2))
    if not all(results.values()):
        raise SystemExit("Some deliberate regressions escaped the tests")


if __name__ == "__main__":
    main()
