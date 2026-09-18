"""Extract observations only; replay fields are never fed into the live solver."""
import argparse
import json
from pathlib import Path
from analyze_replay import read_frames


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("replay", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    frames, trailers = read_frames(args.replay)
    start = frames[0]
    kinds = {v["mapType"]: k for k, v in start["roles"].items()}
    evidence = {"source": str(args.replay.resolve()), "teamId": "4284", "teamName": "Agentic麻辣烫",
                "ignoredTrailerLines": trailers, "events": [], "news": [],
                "limitations": "Post-action replay snapshots; no complete prompt/executeCmd/llmResp/lastCmdResult chain. Answers are observations, never a reusable answer table."}
    previous = {}
    seen_news = set()
    for frame in frames:
        if frame.get("type") != "round":
            continue
        team = next((t for t in frame["teams"] if str(t["teamId"]) == "4284"), None)
        if not team or team["teamName"] != evidence["teamName"]:
            continue
        actor = next(r for r in team["roles"] if kinds[r["roleType"]] == "pioneer")
        news_key = json.dumps(frame.get("news"), sort_keys=True, ensure_ascii=False)
        if any((frame.get("news") or {}).values()) and news_key not in seen_news:
            evidence["news"].append({"round": frame["round"], "news": frame.get("news")})
            seen_news.add(news_key)
        for cmd in actor.get("commands", []):
            if cmd["action"] in ("acceptTask", "submitAnswer", "summonTreasure"):
                evidence["events"].append({
                    "round": frame["round"], "daytime": (frame["round"] - 1) % 130 < 70,
                    "position": actor["pos"], "task": team.get("task"), "command": cmd,
                    "backpackBefore": previous.get("backpacks"), "backpackAfter": actor.get("backpacks"),
                    "gold": team["goldNum"], "score": team["totalScore"],
                    "livingRobotsInSnapshot": sum(r["health"] > 0 and kinds[r["roleType"]].endswith("Robot")
                                                  for t in frame["teams"] for r in t["roles"]),
                    "livingRobotsInChampionTeam": sum(r["health"] > 0 and kinds[r["roleType"]].endswith("Robot")
                                                       for r in team["roles"]),
                    "weaponCommands": [{"id": r["id"], "commands": r["commands"]} for r in team["roles"]
                                       if kinds[r["roleType"]] in ("rocket", "gatling", "railgun") and r.get("commands")]})
        previous = actor
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"events": len(evidence["events"]), "rounds": [e["round"] for e in evidence["events"]],
                      "nightRobotCounts": [{"round": e["round"], "robots": e["livingRobotsInSnapshot"]}
                                           for e in evidence["events"] if not e["daytime"]]}, indent=2))


if __name__ == "__main__":
    main()
