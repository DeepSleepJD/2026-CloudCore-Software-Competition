"""Small deterministic contract harness, not the official judge or survival model.

Models movement, finite mines, trading, construction, upgrades, task responses,
rocket cooldown and damage to stationary targets. Walls receive scripted damage.
No robot AI, opposing policy, score ranking or official build-zone validation.
"""
from collections import Counter
from copy import deepcopy
import json
import random

from agent.model import World, distance, neighbours


class Harness:
    def __init__(self, request, seed=1):
        self.data = deepcopy(request)
        self.rng = random.Random(seed)
        self.actions = Counter()
        self.taken = 0
        self.cool_until = 0
        self.next_id = 50000
        self.stock = {}
        self.gold_min = self.data["teamOur"]["goldNum"]
        self.last_llm = ""
        self.failures = []
        self.guard_ready = False

    def step(self, agent, turn):
        d, team = self.data, self.data["teamOur"]
        d["roundNo"] = turn
        d["llmResp"] = self.last_llm
        d["errors"] = []
        self.last_llm = ""
        d["teamOur"]["playerTasks"] = [{"taskType": "自进化类1", "isValid": self.taken < 3 and turn >= self.cool_until,
                "coldDownRounds": max(0, self.cool_until-turn), "timeoutRounds": 15, "goldReward": 80, "scoreReward": 80}]
        if (turn - 1) % 130 == 70:
            d["robot"]["roles"] = [{"id": 31000+i, "roleType": "smallRobot", "pos": {"x": 24+i % 3, "y": 8+i // 3},
                    "health": 40, "targetTeam": team["type"], "attackPower": 5} for i in range(9)]
        if (turn - 1) % 130 == 0:
            d["robot"]["roles"] = []
        w = World(d)
        result = agent.decide(deepcopy(d))
        if result["prompt"]:
            assert d.get("phaseTask"), "test fixture only issues task model replies"
            self.last_llm = json.dumps({"action": "answer", "answer": {"value": 4}})
        assert not result["executeCmd"], "fixture answers its synthetic arithmetic without shell commands"
        old_units = {u.id: u for u in w.ours}
        units = {u["id"]: u for u in team["roles"]}
        used, destinations, feedback = set(), set(), {}
        for key, cmd in result["roleCommandMap"].items():
            source = old_units[int(key)]
            uid = int(cmd.get("controllerId", key))
            assert uid not in used, "double role action"
            used.add(uid)
            self.actions[cmd["action"]] += 1
            feedback[key] = True
            actor = units[uid]
            points = cmd.get("targetPos") or []
            target = (points[0]["x"], points[0]["y"]) if points else None
            action = cmd["action"]
            if action in {"move", "collect", "build"}:
                assert distance(source.p, target) == 1
            if action in {"move", "build"}:
                assert target not in w.blocked | destinations and w.inside(target)
                destinations.add(target)
            if action == "move":
                actor["pos"] = points[0]
            elif action == "build":
                assert w.day and source.kind == "worker"
                if cmd["name"] == "wall":
                    actor["backpack"].remove("stone")
                else:
                    assert len([u for u in team["roles"] if u["roleType"] == "rocket"]) < 3
                    team["goldNum"] -= 25
                self.next_id += 1
                team["roles"].append({"id": self.next_id, "roleType": cmd["name"], "pos": points[0],
                    "health": 1000, "level": 1, "cooldown": 0, "backpack": []})
            elif action == "collect":
                zone = next((z for z in d["mapInfo"]["zones"] if z["pos"] == points[0]), None)
                if zone is None:
                    feedback[key] = False
                    continue
                assert source.kind == "worker"
                actor["backpack"].append(zone["neutralType"])
                self.stock[target] = self.stock.get(target, 10) - 1
                if self.stock[target] <= 0:
                    occupied = set(w.blocked) | destinations
                    for _ in range(1000):
                        p = self.rng.randrange(17, 40), self.rng.randrange(13, 29)
                        if p not in occupied and w.base_distance(p) > 3:
                            zone["pos"] = {"x": p[0], "y": p[1]}
                            self.stock[p] = 10
                            break
            elif action == "sell":
                assert any(k == "vendor" and distance(source.p, p) == 1 for p, k in w.zones.items())
                assert 0 < cmd["num"] <= actor["backpack"].count(cmd["name"])
                for _ in range(cmd["num"]):
                    actor["backpack"].remove(cmd["name"])
                team["goldNum"] += w.prices[cmd["name"]] * cmd["num"]
            elif action == "buy":
                assert any(k == "weaponShop" and distance(source.p, p) == 1 for p, k in w.zones.items())
                assert len(actor["backpack"]) + cmd["num"] <= source.capacity
                team["goldNum"] -= w.shop[cmd["name"]] * cmd["num"]
                actor["backpack"].extend([cmd["name"]] * cmd["num"])
            elif action == "use":
                name = cmd["name"]
                actor["backpack"].remove(name)
                if name == "Medicine":
                    actor["health"] = 220 if source.kind == "worker" else 200
                else:
                    assert distance(source.p, target) == 1
                    building = next(u for u in team["roles"] if u["pos"] == points[0])
                    if "UpgradeVoucher" in name:
                        assert int(name[-1]) == building["level"] < 3
                        building["level"] += 1
                    building["health"] = 1500*building["level"] if building["roleType"] == "station" else 1000+500*(building["level"]-1)
            elif action == "acceptTask":
                assert source.kind == "pioneer" and not d.get("phaseTask")
                assert self.taken < 3 and turn >= self.cool_until
                assert any(k == team["type"] + "TaskPoint1" and distance(source.p, p) == 1 for p, k in w.zones.items())
                self.taken += 1
                d["phaseTask"] = f"Task {self.taken}: compute 2+2 and return value."
            elif action == "submitAnswer":
                assert d.get("phaseTask") and json.loads(cmd["taskAnswer"]) == {"value": 4}
                team["goldNum"] += 80
                d["phaseTask"] = ""
                self.cool_until = turn + 30
            elif action == "attack":
                assert not w.day and source.cooldown == 0
                assert distance(old_units[uid].p, source.p) == 1
                self.guard_ready = True
                assert len(points) == source.level
                units[int(key)]["cooldown"] = 4
                for point in points:
                    p = point["x"], point["y"]
                    assert distance(source.p, p) <= source.reach
                    for r in d["robot"]["roles"]:
                        gap = distance(p, (r["pos"]["x"], r["pos"]["y"]))
                        r["health"] -= 20 if gap == 0 else 10 if gap == 1 else 0
            else:
                raise AssertionError(action)
            assert team["goldNum"] >= 0, "overspent shared gold"
            self.gold_min = min(self.gold_min, team["goldNum"])
        for u in team["roles"]:
            u["cooldown"] = max(0, u.get("cooldown", 0)-1)
            if u["roleType"] == "wall" and u["pos"] == {"x": 28, "y": 9} and 80 <= (turn-1) % 130 <= 110:
                u["health"] -= 25
        team["roles"] = [u for u in team["roles"] if u["health"] > 0]
        d["robot"]["roles"] = [r for r in d["robot"]["roles"] if r["health"] > 0]
        if d.get("phaseTask") and not any(u["roleType"] == "pioneer" and
                any(z["neutralType"] == team["type"]+"TaskPoint1" and
                    distance((u["pos"]["x"], u["pos"]["y"]), (z["pos"]["x"], z["pos"]["y"])) == 1
                    for z in d["mapInfo"]["zones"]) for u in team["roles"]):
            d["phaseTask"] = ""
            self.cool_until = turn + 30
        d["lastRoundRoleActionResults"] = feedback
        return result
