"""Daytime construction/economy and one-operator nighttime defence."""
from .model import World, Layout, ORES, command, distance, neighbours, wire
from .navigation import paths
from .combat import rocket_targets


class Planner:
    def __init__(self, world, avoided):
        self.w = world
        self.layout = Layout.for_world(world)
        self.avoided = avoided
        self.commands = {}
        self.reserved = set()
        self.sites = set()
        self.budget = world.gold
        self.build_count = len(world.towers)
        self.operator = min(world.actors, key=lambda r: (r.kind != "pioneer", distance(r.p, self.layout.operator), r.id)) if world.actors else None
        self.wall_missing = [p for p in self.layout.walls if not any(r.p == p for r in world.walls)]
        self.danger = {p for r in world.robots for x in range(-4, 5) for y in range(-4, 5)
                       for p in [(r.p[0] + x, r.p[1] + y)] if world.inside(p)}

    def emit(self, actor, cmd):
        self.commands[str(actor.id)] = cmd
        if cmd["action"] in ("move", "build"):
            p = cmd["targetPos"][0]
            self.reserved.add((p["x"], p["y"]))
        return True

    def route(self, actor, avoid_danger=True):
        forbidden = set(self.layout.towers)
        if actor != self.operator:
            forbidden.add(self.layout.operator)
        if avoid_danger:
            forbidden |= self.danger
        return paths(self.w, actor, self.reserved, forbidden)

    def approach(self, actor, target, action=None, exact=False, safe=True):
        distances, first = self.route(actor, safe)
        goals = [target] if exact else neighbours(target)
        goals = [p for p in goals if p in distances and (not safe or p not in self.danger)]
        if not goals:
            return False
        goal = min(goals, key=lambda p: (distances[p], p))
        if goal == actor.p:
            return self.emit(actor, action) if action else True
        return self.emit(actor, command("move", first[goal]))

    def build(self, actor, kind, targets):
        distances, _ = self.route(actor)
        options = []
        for index, p in enumerate(targets):
            if p in self.w.blocked or p in self.sites or p in self.reserved:
                continue
            length = min((distances[q] for q in neighbours(p) if q in distances), default=10**6)
            if length < 10**6:
                options.append((length + index * 0.15, p))
        for _, target in sorted(options):
            if self.approach(actor, target, command("build", target, name=kind)):
                self.sites.add(target)
                if self.commands[str(actor.id)]["action"] == "build" and kind == "rocket":
                    self.budget -= 25
                    self.build_count += 1
                return True
        return False

    def mine(self, actor, stone_only=False):
        if len(actor.bag) >= actor.capacity:
            return False
        distances, _ = self.route(actor)
        choices = []
        for target, kind in self.w.zones.items():
            if kind not in ORES or (stone_only and kind != "stone") or target in self.avoided:
                continue
            travel = min((distances[q] for q in neighbours(target) if q in distances and q not in self.danger), default=10**6)
            price = 1 if stone_only else self.w.prices.get(kind, 0)
            if travel < 10**6 and price > 0:
                # Stay on productive nearby mines; copper wins when travel is comparable.
                choices.append(((travel + 6) / price, target))
        for _, target in sorted(choices):
            if self.approach(actor, target, command("collect", target)):
                return True
        return False

    def sell(self, actor, force=False):
        reserve_stone = min(6, len(self.wall_missing)) if self.w.day else 2
        amounts = {k: actor.bag.count(k) - (reserve_stone if k == "stone" else 0) for k in ORES}
        amounts = {k: n for k, n in amounts.items() if n > 0 and self.w.prices.get(k, 0) > 0}
        if not amounts:
            return False
        vendors = [p for p, k in self.w.zones.items() if k == "vendor"]
        adjacent = any(distance(actor.p, p) == 1 for p in vendors)
        if not force and not adjacent and sum(amounts.values()) < 12 and len(actor.bag) < actor.capacity:
            return False
        name = max(amounts, key=lambda k: (amounts[k] * self.w.prices[k], k))
        for target in sorted(vendors, key=lambda p: distance(actor.p, p)):
            if self.approach(actor, target, command("sell", name=name, num=amounts[name])):
                return True
        return False

    def use_supplies(self, actor):
        if actor.health < (140 if actor.kind == "worker" else 120) and "Medicine" in actor.bag:
            return self.emit(actor, command("use", name="Medicine"))
        options = []
        for unit in self.w.ours:
            prefix = {"rocket": "Weapon", "station": "Station", "wall": "Wall"}.get(unit.kind)
            if not prefix or unit.p in self.sites:
                continue
            name = f"{prefix}UpgradeVoucher{unit.level}"
            if unit.level < 3 and name in actor.bag:
                priority = 0 if unit.kind == "station" and unit.health < 1200 else 1 if unit.kind == "rocket" else 2
                if unit.kind == "wall":
                    priority = (0.5 if unit.health < 350 else 2) + unit.health / (1000 + 500 * (unit.level - 1))
                options.append((priority, distance(actor.p, unit.p), unit.p, name))
            max_health = 1000 + 500 * (unit.level - 1)
            if unit.kind == "wall" and unit.health < max_health * 0.65 and "WallFixer" in actor.bag:
                options.append((0 if unit.health < 350 else 3, distance(actor.p, unit.p), unit.p, "WallFixer"))
        for _, _, target, name in sorted(options):
            if self.approach(actor, target, command("use", target, name=name)):
                self.sites.add(target)
                return True
        return False

    def purchase(self, actor, name, reserve=0):
        if name not in self.w.shop or len(actor.bag) >= actor.capacity:
            return False
        price = self.w.shop[name]
        if self.budget - reserve < price:
            return False
        # Do not send a second courier for an item already being delivered.
        if any(name in r.bag for r in self.w.actors):
            return False
        shops = sorted((p for p, k in self.w.zones.items() if k == "weaponShop"), key=lambda p: distance(actor.p, p))
        for target in shops:
            if actor == self.operator:
                distances, _ = self.route(actor)
                travel = min((distances[q] for q in neighbours(target) if q in distances), default=10**6)
                # A conservative return allowance keeps the gunner home at dusk.
                if not self.w.day or travel + distance(target, self.layout.operator) + 8 >= 70 - self.w.day_tick:
                    continue
            if self.approach(actor, target, command("buy", name=name, num=1)):
                if self.commands[str(actor.id)]["action"] == "buy":
                    self.budget -= price
                return True
        return False

    def upgrade_order(self):
        rockets = sorted((r for r in self.w.towers if r.kind == "rocket"), key=lambda r: (-r.level, r.id))
        station = self.w.station
        if station.level < 3 and station.health < 1500 * station.level * 0.6:
            return f"StationUpgradeVoucher{station.level}"
        if rockets:
            main = rockets[0]
            if main.level < 3:
                return f"WeaponUpgradeVoucher{main.level}"
            other = min(rockets, key=lambda r: (r.level, r.id))
            if other.level < 3:
                return f"WeaponUpgradeVoucher{other.level}"
        if station.level < 3:
            return f"StationUpgradeVoucher{station.level}"
        return None

    def operate(self, actor):
        if not self.w.day:
            choices = []
            for tower in self.w.towers:
                if tower.kind != "rocket" or tower.cooldown > 0 or distance(actor.p, tower.p) != 1:
                    continue
                targets, score = rocket_targets(self.w, tower)
                if targets:
                    choices.append((score, -tower.id, tower, targets))
            if choices:
                _, _, tower, targets = max(choices, key=lambda c: c[:2])
                self.commands[str(tower.id)] = {"action": "attack", "controllerId": str(actor.id),
                                                 "targetPos": [wire(p) for p in targets]}
                return True
        return self.approach(actor, self.layout.operator, exact=True, safe=False)

    def retreat(self, actor):
        if actor.p not in self.danger:
            return False
        distances, first = self.route(actor)
        safe = [p for p in distances if p not in self.danger and p != actor.p]
        if safe:
            goal = min(safe, key=lambda p: (distances[p], distance(p, self.layout.operator), p))
            return self.emit(actor, command("move", first[goal]))
        # If all immediate exits are threatened, choose the least exposed free step.
        free = [p for p in neighbours(actor.p) if self.w.inside(p)
                and p not in self.w.blocked | self.reserved and p not in self.layout.towers]
        if free:
            risk = lambda p: sum(max(0, 5 - distance(p, r.p)) for r in self.w.robots)
            best = min(free, key=lambda p: (risk(p), distance(p, self.layout.operator)))
            if risk(best) < risk(actor.p):
                return self.emit(actor, command("move", best))
        return False

    def worker(self, actor):
        if self.retreat(actor) or self.use_supplies(actor):
            return
        if self.w.day:
            if self.build_count < 3 and self.budget >= 25 and self.build(actor, "rocket", self.layout.towers):
                return
            if self.wall_missing:
                stones = actor.bag.count("stone")
                adjacent_mine = any(k == "stone" and distance(actor.p, p) == 1 and p not in self.avoided for p, k in self.w.zones.items())
                if adjacent_mine and stones < min(6, len(self.wall_missing)) and self.mine(actor, True):
                    return
                if stones and self.build(actor, "wall", self.wall_missing):
                    return
                if not stones:
                    if len(actor.bag) >= actor.capacity and self.sell(actor, True):
                        return
                    if self.mine(actor, True):
                        return
        if self.sell(actor):
            return
        reserve = 25 * max(0, 3 - self.build_count)
        if actor.health < 80 and self.purchase(actor, "Medicine", reserve=reserve):
            return
        # One worker handles repairs; the other continues funding the defence.
        if self.w.workers and actor.id == self.w.workers[0].id:
            if self.w.station.level < 3 and self.w.station.health < 800:
                if self.purchase(actor, f"StationUpgradeVoucher{self.w.station.level}", reserve=reserve):
                    return
            damaged = sorted((r for r in self.w.walls if r.health < (1000 + 500 * (r.level - 1)) * 0.6),
                             key=lambda r: (r.health, r.id))
            if damaged:
                wall = damaged[0]
                # Upgrading both repairs and strengthens the exposed section.
                if wall.level < 3 and self.purchase(actor, f"WallUpgradeVoucher{wall.level}", reserve=reserve):
                    return
                if self.purchase(actor, "WallFixer", reserve=reserve):
                    return
        if not self.mine(actor):
            self.sell(actor, True)

    def run(self):
        # Night control is assigned first so a fallback worker never also mines/moves.
        if not self.w.day and self.operator:
            self.operate(self.operator)
        for worker in self.w.workers:
            if not self.w.day and worker == self.operator:
                continue
            self.worker(worker)
        for actor in self.w.actors:
            if actor.kind != "pioneer":
                continue
            if not self.w.day:
                if actor != self.operator:
                    self.use_supplies(actor)
                continue
            return_margin = distance(actor.p, self.layout.operator) + 8
            if 70 - self.w.day_tick <= return_margin:
                self.operate(actor)
                continue
            if self.use_supplies(actor):
                continue
            upgrade = self.upgrade_order()
            if upgrade and self.purchase(actor, upgrade, reserve=25 * max(0, 3 - self.build_count)):
                continue
            self.operate(actor)
        return {"roleCommandMap": self.commands, "prompt": "", "executeCmd": ""}


class Agent:
    """Small match-local memory; authoritative positions/resources always come from requests."""
    def __init__(self):
        self.key = None
        self.last_round = 0
        self.previous = {}
        self.avoided = {}

    def decide(self, request):
        world = World(request)
        if not world.station or not world.actors:
            return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
        key = (request["teamOur"].get("teamId"), world.team, world.station.p)
        if key != self.key or world.round <= self.last_round:
            self.previous, self.avoided = {}, {}
        if world.round == self.last_round + 1:
            results = request.get("lastRoundRoleActionResults", {})
            for role, cmd in self.previous.items():
                if results.get(role) is False and cmd["action"] == "collect":
                    target = cmd["targetPos"][0]
                    self.avoided[(target["x"], target["y"])] = world.round + 10
        self.avoided = {p: expiry for p, expiry in self.avoided.items() if expiry > world.round}
        response = Planner(world, self.avoided).run()
        self.key, self.last_round = key, world.round
        self.previous = response["roleCommandMap"]
        return response
