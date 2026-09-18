"""Compare both teams in multiple replays; optionally validate offline decisions."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time

from analyze_replay import read_frames, request_from_frame
from agent.strategy import Agent
from tests.support import check


def analyze(path, validate=False):
    frames, markers = read_frames(path)
    start = frames[0]
    kinds = {v["mapType"]: k for k, v in start["roles"].items()}
    result = {"file": path.name, "ignoredTrailerLines": markers, "teams": [],
              "validatedSnapshots": 0, "maxDecisionSeconds": 0}
    for t in start["teams"]:
        result["teams"].append({"id": t["teamId"], "name": t["teamName"],
                               "baseDestroyedRound": None, "completedTasks": [],
                               "upgradeEvents": [], "snapshots": {}, "actions": Counter()})
    shots = {}
    for frame in frames:
        if frame.get("type") != "round":
            continue
        n = frame["round"]
        for team in frame["teams"]:
            for r in team["roles"]:
                for c in r.get("commands", []):
                    if kinds[r["roleType"]] == "rocket" and c["action"] == "attack" and c["valid"]:
                        shots[r["id"]] = n
        for index, team in enumerate(frame["teams"]):
            stats = result["teams"][index]
            roles = team["roles"]
            station = next((r for r in roles if kinds[r["roleType"]] == "station"), None)
            if stats["baseDestroyedRound"] is None and (not station or station["health"] <= 0):
                stats["baseDestroyedRound"] = n
            for r in roles:
                if kinds[r["roleType"]] not in {"worker", "pioneer", "rocket", "station", "wall"}:
                    continue
                for c in r.get("commands", []):
                    if not c.get("valid"):
                        continue
                    action, name = c["action"], c.get("targetName") or ""
                    stats["actions"][action + (":" + name if name else "")] += 1
                    if action == "use" and "UpgradeVoucher" in name:
                        stats["upgradeEvents"].append({"round": n, "name": name, "actor": r["id"],
                                                       "target": c.get("targetPos")})
                    task = team.get("task") or {}
                    if action == "submitAnswer" and task.get("isTaskComplete"):
                        stats["completedTasks"].append({"round": n, "passRate": task.get("passRate"),
                            "rewardReported": task.get("reward"), "roundCost": task.get("roundCost"),
                            "night": (n - 1) % 130 >= 70,
                            "ownRobotsAlive": sum(kinds[u["roleType"]].endswith("Robot") and u["health"] > 0 for u in roles)})
            if n in (70, 200, 330, 460, 590, 720, 1240):
                stats["snapshots"][str(n)] = {
                    "gold": team["goldNum"], "base": {k: station[k] for k in ("level", "health")} if station else None,
                    "rockets": sorted(r["level"] for r in roles if kinds[r["roleType"]] == "rocket" and r["health"] > 0),
                    "wallLevels": dict(Counter(r["level"] for r in roles if kinds[r["roleType"]] == "wall" and r["health"] > 0))}
            if validate and station and station["health"] > 0:
                request = request_from_frame(frame, start, index, shots)
                began = time.perf_counter()
                response = Agent().decide(request)
                result["maxDecisionSeconds"] = max(result["maxDecisionSeconds"], time.perf_counter() - began)
                try:
                    check(request, response)
                except Exception as e:
                    raise AssertionError(f"{path.name} round={n} team={index} response={response}") from e
                result["validatedSnapshots"] += 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replays", type=Path, nargs="+")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"scope": "Post-action replay observations and offline action legality; no real survival prediction. No solver transcript or reusable answers.",
              "matches": [analyze(p, args.validate) for p in args.replays]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{k: m[k] for k in ("file", "validatedSnapshots", "maxDecisionSeconds")} for m in result["matches"]], ensure_ascii=False))
