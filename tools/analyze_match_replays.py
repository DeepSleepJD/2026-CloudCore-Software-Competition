"""Read replay JSONL snapshots; measure observed actions, never infer new-policy wins."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

ACTORS = {6, 7}
BUILDINGS = {4, 5, 30, 31, 32}
GUNS = {30, 31, 32}
PRICES = {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150,
          "StationUpgradeVoucher1": 100, "StationUpgradeVoucher2": 150,
          "WallUpgradeVoucher1": 20, "WallUpgradeVoucher2": 30,
          "WallFixer": 10, "Medicine": 10}


def position(u):
    return u["pos"]["x"], u["pos"]["y"]


def distance(a, b):
    return max(abs(a[0]-b[0]), abs(a[1]-b[1]))


def analyze(path):
    rows, skipped = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        try:
            rows.append(json.loads(line))
        except ValueError:
            skipped.append(n)
    start = next(r for r in rows if r["type"] == "start")
    rounds = [r for r in rows if r["type"] == "round"]
    finish = next((r for r in rows if r["type"] == "finish"), {})
    report = {"file": path.name, "rounds": len(rounds), "skipped_lines": skipped,
              "finish": finish, "teams": []}
    for ti, initial in enumerate(start["teams"]):
        out = {"name": initial["teamName"], "id": initial["teamId"], "side": initial["type"],
               "events": [], "task_submissions": [], "task_accepts": [], "deaths": [], "respawns": [],
               "treasures": [], "daily": {}, "actors": {}, "purchases": Counter(),
               "uses": Counter(), "income": Counter(), "spend": Counter(), "unexplained_gold": []}
        daily = defaultdict(lambda: {"worker_actions": Counter(), "pioneer_actions": Counter(),
                                     "shots": Counter(), "invalid_gun_actions": 0, "sale_gold": 0,
                                     "task_gold": 0, "move_reversals": 0})
        previous = initial
        trajectories = defaultdict(list)
        actor_stats = defaultdict(lambda: {"actions": Counter(), "failed": Counter(), "reversals": [], "empty_commands": 0,
                                          "in_control": 0, "night_collect": 0, "longest_reversal_run": None})
        reversal_run = Counter()
        task_session = None
        lost = set()
        for row in rounds:
            n = row["round"]
            day = (n-1)//130+1
            d = daily[day]
            team = row["teams"][ti]
            prev_units = {u["id"]: u for u in previous["roles"]}
            units = {u["id"]: u for u in team["roles"]}
            for uid in list(lost):
                if uid in units and units[uid]["health"] > 0:
                    out["respawns"].append({"round": n, "id": uid, "bag": units[uid]["backpacks"]})
                    lost.remove(uid)
            task = team["task"]
            known_income = known_spend = 0
            for uid, old in prev_units.items():
                if old["roleType"] in ACTORS | BUILDINGS and old["health"] > 0 and (uid not in units or units[uid]["health"] <= 0) and uid not in lost:
                    lost.add(uid)
                    death = {"round": n, "id": uid, "kind": old["roleType"], "pos": old["pos"],
                             "before_hp": old["health"], "bag": old["backpacks"],
                             "base_cleanup": not any(v["roleType"] == 4 and v["health"] > 0 for v in previous["roles"])}
                    out["deaths"].append(death)
            for u in team["roles"]:
                uid, kind = u["id"], u["roleType"]
                if kind not in ACTORS | BUILDINGS:
                    continue
                old = prev_units.get(uid)
                if kind in BUILDINGS and (not old or old["level"] != u["level"]):
                    out["events"].append({"round": n, "event": "build" if not old else "upgrade",
                                          "id": uid, "kind": kind, "level": u["level"], "pos": u["pos"], "hp": u["health"]})
                if kind in ACTORS and u["health"] > 0:
                    a = actor_stats[uid]
                    a["empty_commands"] += not bool(u["commands"])
                    a["in_control"] += bool(u.get("inControl"))
                    trajectory = trajectories[uid]
                    trajectory.append((n, position(u)))
                    if len(trajectory) >= 3 and trajectory[-3][0] == n-2 and trajectory[-3][1] == position(u) != trajectory[-2][1]:
                        a["reversals"].append(n)
                        d["move_reversals"] += 1
                        reversal_run[uid] += 1
                        best = a["longest_reversal_run"]
                        if not best or reversal_run[uid] > best["reversal_count"]:
                            a["longest_reversal_run"] = {"reversal_count": reversal_run[uid], "end_round": n,
                                                          "positions": [trajectory[-2][1], position(u)]}
                    else:
                        reversal_run[uid] = 0
                for cmd in u["commands"]:
                    action = cmd["action"]
                    name = cmd.get("targetName")
                    valid = cmd.get("valid") is True
                    if kind in ACTORS:
                        actor_stats[uid]["actions"][action] += 1
                        if not valid:
                            actor_stats[uid]["failed"][action] += 1
                        d["worker_actions" if kind == 6 else "pioneer_actions"][action] += 1
                        if action == "collect" and (n-1)%130 >= 70 and valid:
                            actor_stats[uid]["night_collect"] += 1
                    if kind in GUNS:
                        if action == "attack" and valid:
                            d["shots"][uid] += 1
                        elif not valid:
                            d["invalid_gun_actions"] += 1
                    if not valid:
                        continue
                    old_bag = Counter(old["backpacks"]) if old else Counter()
                    bag = Counter(u["backpacks"])
                    if action == "build" and name in {"rocket", "gatling", "railgun"}:
                        known_spend += 25
                        out["spend"]["build_guns"] += 25
                    if action == "buy":
                        qty = max(0, bag[name]-old_bag[name])
                        out["purchases"][name] += qty
                        if name in PRICES:
                            value = qty*PRICES[name]
                            known_spend += value
                            out["spend"][name] += value
                    if action == "sell":
                        qty = max(0, old_bag[name]-bag[name])
                        price = next((p["price"] for p in row["vendorShopList"] if p["name"] == name), 0)
                        value = qty*price
                        known_income += value
                        out["income"][name] += value
                        d["sale_gold"] += value
                    if action == "use":
                        out["uses"][name] += 1
                        out["events"].append({"round": n, "event": "use", "id": uid, "name": name, "target": cmd.get("targetPos")})
                    if action == "acceptTask":
                        task_session = {"round": n, "type": task["taskType"], "description": task["description"],
                                        "remaining_own_robots": sum(v["roleType"] in {11, 12, 13, 14} and v["health"] > 0 for v in team["roles"])}
                        out["task_accepts"].append(task_session)
                    if action == "submitAnswer":
                        out["task_submissions"].append({"round": n, "type": task["taskType"], "complete": task["isTaskComplete"],
                                                        "rate": task["passRate"], "cost": task["roundCost"],
                                                        "answer": cmd.get("taskAnswer")})
                    if action == "summonTreasure":
                        out["treasures"].append({"round": n, "result": cmd.get("summonTreasureResult"),
                                                 "position": cmd.get("targetPos")})
            if task["isTaskComplete"] and not previous["task"]["isTaskComplete"]:
                reward = task["reward"] * task["passRate"]
                known_income += reward
                out["income"]["task"] += reward
                d["task_gold"] += reward
            actual_delta = team["goldNum"]-previous["goldNum"]
            residual = actual_delta-known_income+known_spend
            if residual:
                out["unexplained_gold"].append({"round": n, "delta": residual})
            d.update(end_gold=team["goldNum"], score=team["totalScore"], tasks=team["completeTaskCount"],
                     invalid_tasks=team["invalidTaskCount"],
                     base=next(({"hp": u["health"], "level": u["level"]} for u in team["roles"] if u["roleType"] == 4), None),
                     guns=[u["level"] for u in team["roles"] if u["roleType"] in GUNS and u["health"] > 0],
                     walls=dict(Counter(u["level"] for u in team["roles"] if u["roleType"] == 5 and u["health"] > 0)))
            previous = team
        out["daily"] = daily
        out["actors"] = actor_stats
        out["ledger_balance"] = initial["goldNum"] + sum(out["income"].values()) - sum(out["spend"].values()) + sum(v["delta"] for v in out["unexplained_gold"])
        assert out["ledger_balance"] == previous["goldNum"]
        report["teams"].append(out)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    results = [analyze(p) for p in sorted(args.directory.glob("对战_*.json"))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(args.output))
