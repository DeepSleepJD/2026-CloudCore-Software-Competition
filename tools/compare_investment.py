"""Compare an untouched pre-edit source snapshot against current code.

Synthetic tasks pay at most six times; task latency, kills and income are scripted.
This is an investment regression, not a replacement for the official game server.
"""
import argparse
import importlib
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.strategy import Agent
from tests.test_tasks import TaskRollout
from tests.support import EconomyRollout
from tests.test_survival import defended


def load_snapshot(path):
    package = types.ModuleType("investment_baseline")
    package.__path__ = [str(path.resolve())]
    sys.modules[package.__name__] = package
    return importlib.import_module("investment_baseline.strategy").Agent


def summary(p):
    roles = p["teamOur"]["roles"]
    return {"baseLevel": next(r["level"] for r in roles if r["roleType"] == "station"),
            "rocketLevels": sorted(r["level"] for r in roles if r["roleType"] == "rocket"),
            "reinforcedWalls": sum(r["roleType"] == "wall" and r["level"] >= 2 for r in roles),
            "maxWalls": sum(r["roleType"] == "wall" and r["level"] == 3 for r in roles),
            "gold": p["teamOur"]["goldNum"]}


def fixed_cash(factory):
    sim, agent = EconomyRollout(), factory()
    p = sim.request = defended(261, 276)
    p["mapInfo"]["zones"] = [z for z in p["mapInfo"]["zones"] if z["neutralType"] not in {"stone", "iron", "copper"}]
    for _ in range(100):
        sim.step(agent.decide(p))
    return summary(p)


def task_economy(factory, left, rounds):
    sim = TaskRollout(left)
    sim.agent = factory()
    successes = [0, 0]
    milestones = {}
    snapshots = {}
    for _ in range(rounds):
        n = sim.p["roundNo"]
        # Both versions see the same scripted wave: present on rounds 71..95,
        # cleared thereafter. This does NOT infer that either agent killed it.
        if (n - 1) % 130 >= 95:
            sim.p["robot"]["roles"] = []
        tasks = sim.p["teamOur"]["playerTasks"]
        for i, t in enumerate(tasks):
            if successes[i] >= 3:
                t["isValid"] = False
        response = sim.step()
        cmd = response["roleCommandMap"].get(sim.rid, {})
        if cmd.get("action") == "submitAnswer" and not sim.p.get("phaseTask"):
            successes[tasks.index(sim.accepted)] += 1
        state = summary(sim.p)
        if 3 in state["rocketLevels"]:
            milestones.setdefault("firstMaxRocket", n)
        if state["rocketLevels"] == [3, 3, 3]:
            milestones.setdefault("allMaxRockets", n)
        if state["reinforcedWalls"] >= 6:
            milestones.setdefault("sixReinforcedWalls", n)
        if n in (70, 200, 330, 460, 590, rounds):
            snapshots[str(n)] = state
    return {"side": "left" if left else "right", "taskSuccesses": sum(successes),
            "taskGold": 80 * sum(successes), "milestones": milestones, "snapshots": snapshots,
            "maxDecisionSeconds": max(sim.times)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True, help="Pre-edit snapshot's agent directory")
    parser.add_argument("--rounds", type=int, default=650)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"scope": "Controlled income and movement only; maximum six 80-gold synthetic tasks. Stationary robot wave is forcibly cleared after round 95 each day; no damage, official AI, real LLM or survival scores.",
              "baseline": str(args.baseline_dir.resolve()), "rounds": args.rounds}
    for name, factory in (("before", load_snapshot(args.baseline_dir)), ("after", Agent)):
        result[name] = {"fixed276GoldNoIncome": fixed_cash(factory),
                        "taskEconomy": [task_economy(factory, side, args.rounds) for side in (True, False)]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
