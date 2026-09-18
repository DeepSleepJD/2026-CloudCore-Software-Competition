"""Replay-informed baseline: shared rocket crew and persistent economic trips.

No replay answers, model training, or assumptions about direct PvP damage.
The legacy policy remains available for controlled comparisons.
"""
from collections import Counter
from itertools import combinations

from .strategy import Agent
from .world import TOWERS, around, command, distance, footprint, pos


class BaselineAgent(Agent):
    def reset(self, world, identity):
        super().reset(world, identity)
        self.mine_jobs = {}
        self.selling = set()
        self.guard = None
        self.previous_health = {}
        self.damage = {}

    def observe(self, w):
        super().observe(w)
        self.damage = {u["id"]: max(0, self.previous_health.get(u["id"], u["health"]) - u["health"])
                       for u in w.ours}
        self.previous_health = {u["id"]: u["health"] for u in w.ours}
        alive = {u["id"] for u in w.people}
        self.mine_jobs = {uid: job for uid, job in self.mine_jobs.items() if uid in alive}
        self.selling.intersection_update(alive)

    def layout(self, w):
        x, y = pos(w.station)
        right = x >= w.width / 2
        # Observed lower-right formation; rotate around the 2x2 base centre.
        def transform(offset):
            dx, dy = offset
            return (x + dx, y + dy) if right else (x + 1 - dx, y - 1 - dy)
        towers = [transform(p) for p in [(2, -1), (1, 1), (2, 1)]]
        walls = [transform(p) for p in [(-2, -1), (-2, 0), (-2, -2),
                 (-2, 1), (-2, -3), (-2, 2), (-1, 2), (0, 2),
                 (-1, -3), (0, -3), (1, 2), (1, -3)]]
        if self.config.tower_offsets:
            towers = [(x + dx, y + dy) for dx, dy in self.config.tower_offsets]
        if self.config.wall_offsets:
            walls = [(x + dx, y + dy) for dx, dy in self.config.wall_offsets]
        self.tower_slots = list(zip(self.config.tower_loadout, towers))
        self.facing = (-1, 1) if right else (1, -1)
        return towers, [p for p in walls if w.inside(p)][:self.config.wall_limit]

    def path(self, w, actor, goals):
        blocked = {point for (uid, point) in self.bad_steps if uid == actor["id"]}
        # Avoid walking carriers through currently visible robot attack ranges.
        for robot in w.home_threats():
            if robot.get("abnormalState") == "dizzy":
                continue
            x, y = pos(robot)
            blocked.update((a, b) for a in range(x - 3, x + 4) for b in range(y - 3, y + 4))
        return w.path(actor, goals, extra_blocked=blocked)

    def assign(self, w):
        people = [p for p in w.people if not (p["roleType"] == "pioneer" and w.data.get("phaseTask"))]
        if not people or not w.towers:
            return {}
        rockets = [t for t in w.towers if t["roleType"] == "rocket"]
        structures = w.blocked - {pos(p) for p in w.people}
        groups = []
        for size in range(min(3, len(rockets)), 1, -1):
            group = next((list(g) for g in combinations(rockets, size)
                          if any(w.inside(p) and p not in structures for p in self.group_stands(g))), None)
            if group:
                groups.append(group)
                break
        grouped = {t["id"] for g in groups for t in g}
        groups.extend([[t] for t in w.towers if t["id"] not in grouped])
        result = {}
        for group in groups:
            choices = []
            for actor in people:
                if actor["id"] in result:
                    continue
                route = self.path(w, actor, self.group_stands(group))
                if route is None:
                    continue
                # Keep the seated night crew stable. Daytime workers cover tasks.
                preference = 0 if actor["id"] == self.guard and not w.daylight else 1
                task_worker = int(actor["roleType"] == "pioneer" and w.daylight and w.tick < 50)
                if w.daylight and w.tick >= 50:
                    task_worker = int(actor["roleType"] != "pioneer" or route.length + 6 >= w.day_left)
                choices.append((preference, task_worker, route.length, actor["id"], actor))
            if choices:
                actor = min(choices, key=lambda c: c[:4])[-1]
                result[actor["id"]] = group
        self.guard = next(iter(result), None)
        return result

    def must_return(self, w, actor):
        return actor["id"] in self.duty_groups and super().must_return(w, actor)

    def upgrade_choice(self, w):
        level = w.station.get("level", 1)
        if level < 3 and (not w.healthy_base() or (w.day >= 3 and level == 1) or w.day >= 5):
            return f"StationUpgradeVoucher{level}", w.station
        # First two rounds of upgrades spread missiles across the whole crew.
        # Once every rocket is level 2, focus one at a time to level 3.
        rockets = [t for t in w.towers if t["roleType"] == "rocket" and t.get("level", 1) < 3]
        if rockets:
            target = min(rockets, key=lambda t: (t.get("level", 1), t["id"]))
            return f"WeaponUpgradeVoucher{target.get('level', 1)}", target
        return super().upgrade_choice(w)

    def maintain(self, w, actor):
        bag = actor.get("backpack", [])
        if actor["health"] < 160 and "Medicine" in bag:
            return w.put(actor, command("use", name="Medicine"))
        if "WallFixer" in bag:
            for wall in sorted(w.walls, key=lambda u: u["health"]):
                threshold = max(400, self.damage.get(wall["id"], 0) * 3)
                if wall["health"] < threshold and w.at(actor, {pos(wall)}):
                    if w.put(actor, command("use", [pos(wall)], name="WallFixer")):
                        return True
        return super().maintain(w, actor)

    def repair_delivery(self, w, actor):
        if "WallFixer" not in actor.get("backpack", []):
            return False
        walls = [u for u in w.walls if u["health"] < max(650, self.damage.get(u["id"], 0) * 5)]
        for wall in sorted(walls, key=lambda u: (u["health"], distance(pos(actor), pos(u)))):
            # A second carrier should keep working rather than crowd the same wall.
            key = ("repair", wall["id"])
            if key in self.claimed_jobs:
                continue
            route = self.path(w, actor, around(pos(wall)))
            if route is None:
                continue
            self.claimed_jobs.add(key)
            if route.length:
                return w.put(actor, command("move", [route.step]))
            if self.maintain(w, actor):
                return True
            if not w.daylight and any(distance(pos(r), pos(wall)) <= 4 for r in w.home_threats()):
                w.used.add(actor["id"])
                w.reasons[actor["id"]] = "repair_guard"
                return True
        return False

    def buy_supplies(self, w, actor, adjacent_only=False):
        shops = w.zone_cells("weaponShop")
        if not shops or len(actor.get("backpack", [])) >= actor.get("backPackCapability", 100):
            return False
        bag = Counter(actor.get("backpack", []))
        all_fixers = sum(p.get("backpack", []).count("WallFixer") for p in w.people)
        wanted = []
        if actor["health"] < 160 and not bag["Medicine"]:
            wanted.append(("Medicine", 1))
        if all_fixers < 2 and any(u["health"] < 650 for u in w.walls):
            wanted.append(("WallFixer", min(2 - all_fixers, 2)))
        reserve = 25 * max(0, self.target_tower_count(w) - len(w.towers)
                           - sum(kind in TOWERS for _, kind in self.builds.values()))
        for name, count in wanted:
            price = w.shop.get(name, 0)
            if price <= 0 or name in self.claimed_jobs or w.gold < price + reserve:
                continue
            if name != "Medicine" and not self.best_buyer(w, actor, shops):
                continue
            count = min(count, (w.gold - reserve) // price,
                        actor.get("backPackCapability", 100) - len(actor.get("backpack", [])))
            if w.at(actor, shops):
                if w.put(actor, command("buy", name=name, num=count)):
                    w.gold -= price * count
                    self.claimed_jobs.add(name)
                    return True
            elif not adjacent_only and w.daylight:
                route = self.path(w, actor, {p for s in shops for p in around(s)} - set(shops))
                if route and route.length + min(w.base_distance(s) for s in shops) + 8 < w.day_left:
                    if w.put(actor, command("move", [route.step])):
                        self.claimed_jobs.add(name)
                        return True
        return False

    def shopping(self, w, actor, adjacent_only=False):
        # Heal the carrier before expensive procurement. Deliver carried vouchers first.
        if actor["health"] < 160:
            return self.buy_supplies(w, actor, adjacent_only)
        if any("UpgradeVoucher" in item for item in actor.get("backpack", [])):
            return False
        if self.buy_supplies(w, actor, adjacent_only):
            return True
        return super().shopping(w, actor, adjacent_only)

    def worker(self, w, actor):
        if self.maintain(w, actor):
            return
        if actor["health"] < 100 and self.evade(w, actor):
            return
        if self.repair_delivery(w, actor) or self.deliver_upgrade(w, actor):
            return
        super().worker(w, actor)

    def mine_or_sell(self, w, actor, need_stone=False, adjacent_only=False):
        uid = actor["id"]
        bag = Counter(actor.get("backpack", []))
        ores = {k: v for k, v in bag.items() if k in {"stone", "iron", "copper"} and w.prices.get(k, 0) > 0}
        if need_stone:
            ores.pop("stone", None)
        full = len(actor.get("backpack", [])) >= actor.get("backPackCapability", 100)
        vendors = w.zone_cells("vendor")
        if not ores:
            self.selling.discard(uid)
        if ores and (sum(ores.values()) >= self.config.sale_batch or full or w.at(actor, vendors)
                     or actor["roleType"] == "pioneer"):
            self.selling.add(uid)
        if uid in self.selling and ores:
            if w.at(actor, vendors):
                ore = max(ores, key=lambda k: ores[k] * w.prices[k])
                return w.put(actor, command("sell", name=ore, num=ores[ore]))
            if not adjacent_only and self.walk(w, actor, vendors):
                return True
        if full or actor["roleType"] != "worker":
            return False
        options = []
        for zone in w.zones:
            kind, point = zone["neutralType"], pos(zone)
            if kind not in {"stone", "iron", "copper"} or (need_stone and kind != "stone"):
                continue
            if self.failed_sites.get((point, "mine"), 0) >= w.round:
                continue
            route = self.path(w, actor, around(point))
            if route is None or (adjacent_only and route.length):
                continue
            if uid in self.duty_groups and w.daylight and route.length + w.base_distance(point) + 8 >= w.day_left:
                continue
            price = 1 if need_stone else w.prices.get(kind, 0)
            if price <= 0:
                continue
            return_cost = 0 if need_stone else min((distance(point, v) for v in vendors), default=999)
            batch = min(3, len(self.wall_jobs(w))) if need_stone else self.config.sale_batch
            value = price * batch / max(1, route.length + return_cost + batch + 1)
            stick = self.mine_jobs.get(uid) == (point, need_stone)
            options.append((not stick, -value, point, route))
        if options:
            _, _, point, route = min(options, key=lambda v: v[:3])
            self.mine_jobs[uid] = point, need_stone
            if route.length:
                return w.put(actor, command("move", [route.step]))
            if w.put(actor, command("collect", [point])):
                self.builds[uid] = (point, "mine")
                return True
        self.mine_jobs.pop(uid, None)
        if ores and not adjacent_only:
            return self.walk(w, actor, vendors)
        return False

    def fallback(self, w, actor):
        if actor["id"] not in self.duty_groups:
            if self.maintain(w, actor) or (actor["health"] < 100 and self.evade(w, actor)):
                return
            if self.repair_delivery(w, actor) or self.deliver_upgrade(w, actor):
                return
        super().fallback(w, actor)

    def target_weight(self, w, robot):
        power = robot.get("attackPower", 5)
        if w.base_distance(pos(robot)) <= 3:
            return 20 + power
        if any(u["health"] < 650 and distance(pos(robot), pos(u)) <= 3 for u in w.walls):
            return 3 + power / 10
        return 1
