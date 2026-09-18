"""Compare a committed baseline against working code without changing the checkout.

These are controlled regressions and economy rollouts, not simulated match scores.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.strategy import Agent
from tests.support import EconomyRollout, check, point, unit
from tests.test_survival import defended


def baseline_agent(ref):
    namespace = "survival_baseline"
    package = types.ModuleType(namespace)
    package.__path__ = []
    sys.modules[namespace] = package
    # Only read explicitly requested local Git objects; no checkout or network.
    available = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", ref, "agent"],
                                        cwd=ROOT, encoding="utf-8").splitlines()
    for name in ("model", "navigation", "combat", "logistics", "tasks", "treasure", "strategy"):
        if f"agent/{name}.py" not in available:
            continue
        source = subprocess.check_output(
            ["git", "-c", f"safe.directory={ROOT.as_posix()}", "show", f"{ref}:agent/{name}.py"],
            cwd=ROOT, encoding="utf-8")
        module = types.ModuleType(f"{namespace}.{name}")
        module.__package__ = namespace
        sys.modules[module.__name__] = module
        exec(compile(source, f"git:{ref}:agent/{name}.py", "exec"), module.__dict__)
    return sys.modules[f"{namespace}.strategy"].Agent


def economy(factory, left, rounds):
    sim, agent = EconomyRollout(left), factory()
    upgrades, previous = [], {}
    for _ in range(rounds):
        n = sim.request["roundNo"]
        sim.step(agent.decide(sim.request))
        for role in sim.request["teamOur"]["roles"]:
            if role["roleType"] not in {"station", "rocket"}:
                continue
            if role["level"] > previous.get(role["id"], 1):
                upgrades.append({"round":n, "kind":role["roleType"], "level":role["level"], "id":role["id"]})
            previous[role["id"]] = role["level"]
    return {"side":"left" if left else "right", "rounds":rounds, "upgrades":upgrades,
            "actions":dict(sim.counts), "remainingGold":sim.request["teamOur"]["goldNum"]}


def fixed_cash(factory):
    sim, agent = EconomyRollout(), factory()
    sim.request = defended(261, 276)
    sim.request["mapInfo"]["zones"] = [z for z in sim.request["mapInfo"]["zones"]
                                          if z["neutralType"] not in {"stone", "iron", "copper"}]
    milestones = {}
    for _ in range(130):
        n = sim.request["roundNo"]
        sim.step(agent.decide(sim.request))
        station = next(r for r in sim.request["teamOur"]["roles"] if r["roleType"] == "station")
        if station["level"] > 1:
            milestones.setdefault(str(station["level"]), n)
    return {"initialGold":276, "income":0, "baseUpgradeRounds":milestones,
            "finalBaseLevel":station["level"], "remainingGold":sim.request["teamOur"]["goldNum"]}


def scripted_wall_pressure(factory):
    sim, agent = EconomyRollout(), factory()
    p = sim.request = defended(331, 0)
    p["mapInfo"]["zones"] = [z for z in p["mapInfo"]["zones"] if z["neutralType"] not in {"stone", "iron", "copper"}]
    worker = p["teamOur"]["roles"][0]
    worker.update(pos=point(29,9), backpack=["WallFixer"] * 6)
    wall = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "wall" and r["pos"] == point(28,9))
    wall.update(level=3, health=2000)
    p["robot"]["roles"] = [unit(30000, "largeRobot", 26, 9, health=500, targetTeam="defender")]
    repairs, first_breach = 0, None
    for _ in range(60):
        n = p["roundNo"]
        response = agent.decide(p)
        check(p, response)
        repairs += sum(c["action"] == "use" and c.get("name") == "WallFixer"
                       for c in response["roleCommandMap"].values())
        sim.step(response)
        # Deliberately fixed, independent of robot movement and our weapon shots.
        wall["health"] = max(0, wall["health"] - 80)
        if wall["health"] == 0 and first_breach is None:
            first_breach = n
    return {"rounds":60, "scriptedDamagePerRound":80, "repairs":repairs,
            "firstWallBreachRound":first_breach, "finalWallHealth":wall["health"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="a7b2b00")
    parser.add_argument("--rounds", type=int, default=650)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = {"scope":"Controlled economy and scripted wall damage; no official robot AI or survival score.",
               "baselineCommit":args.baseline}
    for name, factory in (("before", baseline_agent(args.baseline)), ("after", Agent)):
        results[name] = {"fixedCash":fixed_cash(factory), "wallPressure":scripted_wall_pressure(factory),
                         "economy":[economy(factory, left, args.rounds) for left in (True, False)]}
    text = json.dumps(results, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
