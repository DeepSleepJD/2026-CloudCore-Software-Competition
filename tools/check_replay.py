"""Decision checks on replay snapshots, NOT a game simulation or win-rate test.

Replay omits task sandbox results, current rocket cooldown and shop catalog.
Disable tasks; conservatively assume guns are ready. Never feed our alternative
commands back into the historical state or label subsequent history our outcome.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.strategy import Agent
from zk_agent.world import World, distance, pos

KINDS = {4: "station", 5: "wall", 6: "worker", 7: "pioneer", 11: "smallRobot",
         12: "middleRobot", 13: "largeRobot", 14: "bossRobot", 30: "gatling",
         31: "railgun", 32: "rocket"}
ORES = {"石头": "stone", "铁": "iron", "铜": "copper"}
PRICES = {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150,
          "StationUpgradeVoucher1": 100, "StationUpgradeVoucher2": 150,
          "WallFixer": 10, "Medicine": 10}


def adapt(start, row, index):
    teams = deepcopy(row["teams"])
    robots = []
    for team in teams:
        people = []
        for role in team["roles"]:
            role["roleType"] = KINDS[role["roleType"]]
            role["backpack"] = role.pop("backpacks", [])
            role["backPackCapability"] = 40 if role["roleType"] == "pioneer" else 100
            role["cooldown"] = 0
            if role["roleType"].endswith("Robot"):
                role["targetTeam"] = team["type"]
                robots.append(role)
            else:
                people.append(role)
        team["roles"] = people
        team["playerTasks"] = []
    zones = [{"pos": r["pos"], "neutralType": ORES[r["resName"]]} for r in row["resources"]]
    zones += [{"pos": n["pos"], "neutralType": n["roleName"]} for n in row["npc"]]
    return {"roundNo": row["round"], "mapInfo": {"width": start["map"]["width"],
            "height": start["map"]["height"], "zones": zones},
            "teamOur": teams[index], "teamEnemy": teams[1-index], "robot": {"roles": robots},
            "phaseTask": "", "llmResp": "", "lastCmdResult": "", "errors": [],
            "lastRoundRoleActionResults": {}, "vendorShopList": row["vendorShopList"],
            "weaponShopList": [{"name": k, "price": v} for k, v in PRICES.items()]}


def validate(data, reply):
    w = World(data)
    ours = {u["id"]: u for u in w.ours}
    used, destinations = set(), set()
    spending = 0
    for key, cmd in reply["roleCommandMap"].items():
        role = ours[int(key)]
        uid = int(cmd.get("controllerId", key))
        assert uid not in used, ("double action", uid)
        used.add(uid)
        if cmd["action"] == "attack":
            assert not w.daylight
            assert distance(pos(ours[uid]), pos(role)) <= 1
            assert len(cmd["targetPos"]) <= role["level"]
            reach = [10, 15, 999][role["level"]-1]
            assert all(distance(pos(role), (p["x"], p["y"])) <= reach for p in cmd["targetPos"])
        if cmd["action"] == "move":
            p = cmd["targetPos"][0]
            target = p["x"], p["y"]
            assert target not in w.blocked and target not in destinations
            assert w.inside(target) and distance(pos(role), target) == 1
            destinations.add(target)
        if cmd["action"] == "buy":
            spending += PRICES[cmd["name"]] * cmd["num"]
        if cmd["action"] == "build":
            assert w.daylight and role["roleType"] == "worker"
            spending += 25 if cmd["name"] != "wall" else 0
    assert spending <= data["teamOur"]["goldNum"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path)
    parser.add_argument("--stride", type=int, default=10)
    args = parser.parse_args()
    rows, skipped = [], []
    for number, line in enumerate(args.replay.read_text(encoding="utf-8-sig").splitlines(), 1):
        try:
            rows.append(json.loads(line))
        except ValueError:
            if line.strip():
                skipped.append(number)
    start = next(r for r in rows if r["type"] == "start")
    times, commands = [], 0
    with tempfile.TemporaryDirectory() as state:
        for row in rows:
            if row["type"] != "round" or (row["round"]-1) % args.stride:
                continue
            for index in (0, 1):
                data = adapt(start, row, index)
                agent = Agent(state_dir=state)
                before = time.perf_counter()
                result = agent.decide(data)
                times.append(time.perf_counter() - before)
                validate(data, result)
                commands += len(result["roleCommandMap"])
    print(json.dumps({"snapshots": len(times), "commands": commands,
                      "max_seconds": round(max(times, default=0), 4), "skipped_lines": skipped,
                      "scope": "snapshot action checks only; no combat simulation or win-rate claim"}, indent=2))


if __name__ == "__main__":
    main()
