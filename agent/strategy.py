"""Daytime construction/economy and one-operator nighttime defence."""
from .model import World, Layout, ORES, command, distance, neighbours, wire
from .navigation import paths
from .combat import rocket_targets
from .state import Memory
from .missions import Missions
import os


class Planner:
    def __init__(self, world, avoided, memory=None, missions=None):
        self.w = world
        self.layout = Layout.for_world(world)
        self.avoided = avoided
        self.commands = {}
        self.reserved = set()
        self.sites = set()
        self.budget = world.gold
        self.memory = memory or Memory()
        self.missions = missions
        self.used = set()
        self.purchases = set()
        self.extra = {"prompt": "", "executeCmd": ""}
        self.build_count = len(world.towers)
        self.operator = min(world.actors, key=lambda r: (r.kind != "pioneer", distance(r.p, self.layout.operator), r.id)) if world.actors else None
        treasure = missions.news.treasure if missions else None
        prepare_night_trip = bool(treasure and not missions.news.treasure_done and treasure["day"] == world.day_no
                                  and treasure["phase"] == "night" and world.day and world.day_tick >= 45)
        relief_ready = bool(missions and missions.news.due(world) and not world.day and
                            any(r.p == self.layout.operator for r in world.workers))
        if world.workers and (world.raw.get("phaseTask") or prepare_night_trip or relief_ready):
            self.operator = min(world.workers, key=lambda r: (distance(r.p, self.layout.operator), r.id))
        if not world.day:
            previous_guard = next((r for r in world.actors if r.id == self.memory.night_guard), None)
            if previous_guard and not (previous_guard.kind == "pioneer" and
                                       (world.raw.get("phaseTask") or relief_ready)):
                self.operator = previous_guard
            self.memory.night_guard = self.operator.id if self.operator else None
        else:
            self.memory.night_guard = None
        self.wall_missing = [p for p in self.layout.walls if not any(r.p == p for r in world.walls)]
        self.danger = {p for r in world.robots for x in range(-4, 5) for y in range(-4, 5)
                       for p in [(r.p[0] + x, r.p[1] + y)] if world.inside(p)}

    def emit(self, actor, cmd):
        if actor.id in self.used:
            return False
        self.commands[str(actor.id)] = cmd
        self.used.add(actor.id)
        if cmd["action"] in ("move", "build"):
            p = cmd["targetPos"][0]
            self.reserved.add((p["x"], p["y"]))
        return True

    def emergency(self):
        base = self.w.station
        rate = self.memory.rate(base)
        return (rate > 0 and base.health / rate <= 12) or (base.health < 600 and
                any(self.w.base_distance(r.p) <= 4 for r in self.w.threats))

    def wall_need(self):
        choices = []
        for wall in self.w.walls:
            max_hp = 1000 + 500 * (wall.level - 1)
            incoming = sum(r.attack for r in self.w.threats if distance(r.p, wall.p) <= 3)
            rate = max(self.memory.rate(wall), incoming)
            ttl = wall.health / rate if rate else float("inf")
            prior = self.memory.pressure.get(wall.id, 0)
            if wall.health >= max_hp * 0.8 and ttl > 8 and prior * 8 <= max_hp:
                continue
            # Ten gold repair is the economical bridge to dawn. Upgrade only
            # sections that actually receive sustained damage, or need extra HP now.
            name = "WallFixer"
            remaining = 130 - self.w.day_tick if not self.w.day else 60
            if wall.level < 3 and remaining > 8 and (prior > 0 or ttl <= 8):
                name = f"WallUpgradeVoucher{wall.level}"
            if self.w.shop.get(name, 0) <= 0:
                name = "WallFixer"
            if self.w.shop.get(name, 0) > 0:
                choices.append((ttl, wall.health / max_hp, wall.id, name, wall))
        if choices:
            value = min(choices, key=lambda x: x[:3])
            return value[3], value[4]
        return None

    def defence_need(self):
        base = self.w.station
        if base.level < 3 and (base.health < 1500 * base.level * 0.6 or self.emergency()):
            name = f"StationUpgradeVoucher{base.level}"
            if self.w.shop.get(name, 0) > 0:
                return name, base
        return self.wall_need()

    def defence_reserve(self):
        reserve = 25 * max(0, 3 - self.build_count)
        need = self.defence_need()
        if need:
            name, _ = need
            if name not in self.purchases and not any(name in r.bag for r in self.w.actors):
                reserve += self.w.shop.get(name, 0)
        return reserve

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
            if self.missions and self.missions.news.closed(kind, self.w.day_no):
                continue
            travel = min((distances[q] for q in neighbours(target) if q in distances and q not in self.danger), default=10**6)
            price = 1 if stone_only else self.w.prices.get(kind, 0)
            if travel < 10**6 and price > 0:
                # Stay on productive nearby mines; copper wins when travel is comparable.
                vendor_trip = min((distance(target, p) for p, k in self.w.zones.items() if k == "vendor"), default=30)
                sticky = self.memory.mines.get(actor.id) == (target, stone_only)
                choices.append((not sticky, (travel + (0 if stone_only else vendor_trip) + 6) / price, target))
        for _, _, target in sorted(choices):
            if self.approach(actor, target, command("collect", target)):
                self.memory.mines[actor.id] = target, stone_only
                return True
        self.memory.mines.pop(actor.id, None)
        return False

    def sell(self, actor, force=False):
        reserve_stone = min(6, len(self.wall_missing)) if self.w.day else 2
        amounts = {k: actor.bag.count(k) - (reserve_stone if k == "stone" else 0) for k in ORES}
        amounts = {k: n for k, n in amounts.items() if n > 0 and self.w.prices.get(k, 0) > 0}
        if self.missions and not force and len(actor.bag) < actor.capacity and self.budget >= self.defence_reserve() + 50:
            amounts = {k: n for k, n in amounts.items() if not self.missions.news.hold(k, self.w.day_no)}
        if not amounts:
            self.memory.selling.discard(actor.id)
            return False
        vendors = [p for p, k in self.w.zones.items() if k == "vendor"]
        adjacent = any(distance(actor.p, p) == 1 for p in vendors)
        if not force and actor.id not in self.memory.selling and not adjacent and sum(amounts.values()) < 12 and len(actor.bag) < actor.capacity:
            return False
        self.memory.selling.add(actor.id)
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

    def urgent_local_supply(self, actor):
        if actor.health < 160 and "Medicine" in actor.bag:
            return self.emit(actor, command("use", name="Medicine"))
        targets = [self.w.station] + sorted(self.w.walls, key=lambda u: u.health)
        for unit in targets:
            if distance(actor.p, unit.p) != 1 or unit.p in self.sites:
                continue
            incoming = sum(r.attack for r in self.w.robots if distance(r.p, actor.p) <= 3)
            if actor.health <= incoming + 20:
                continue  # Do not sacrifice a carrier that cannot survive this turn.
            rate = self.memory.rate(unit)
            threshold = max(400, rate * 4)
            if unit.health >= threshold:
                continue
            prefix = "Station" if unit.kind == "station" else "Wall"
            options = [f"{prefix}UpgradeVoucher{unit.level}"] if unit.level < 3 else []
            if unit.kind == "wall":
                options.append("WallFixer")
            for name in options:
                if name in actor.bag and self.emit(actor, command("use", unit.p, name=name)):
                    self.sites.add(unit.p)
                    return True
        return False

    def rescue_delivery(self, actor):
        """Allow a short repair approach only when the carrier can afford exposure."""
        for wall in sorted(self.w.walls, key=lambda u: u.health):
            if wall.health >= max(650, self.memory.rate(wall) * 6) or wall.p in self.sites:
                continue
            usable = [f"WallUpgradeVoucher{wall.level}"] if wall.level < 3 else []
            usable.append("WallFixer")
            if not any(name in actor.bag for name in usable):
                continue
            distances, first = self.route(actor, False)
            options = []
            for stand in neighbours(wall.p):
                length = distances.get(stand, 1000)
                if not 1 <= length <= 5:
                    continue
                step = first[stand]
                risk = max(sum(r.attack for r in self.w.robots if distance(r.p, point) <= 3)
                           for point in (actor.p, step, stand))
                rate = self.memory.rate(wall)
                if actor.health <= risk * (length + 1) + 20 or (rate and wall.health / rate <= length + 1):
                    continue
                options.append((length, risk, stand, step))
            if options:
                step = min(options)[-1]
                if self.emit(actor, command("move", step)):
                    self.sites.add(wall.p)
                    return True
        return False

    def purchase(self, actor, name, reserve=0):
        if actor.id in self.used or name in self.purchases or name not in self.w.shop or len(actor.bag) >= actor.capacity:
            return False
        price = self.w.shop[name]
        if price <= 0 or self.budget - reserve < price:
            return False
        # Do not send a second courier for an item already being delivered.
        if any(name in r.bag for r in (self.w.actors if name != "Medicine" else [actor])):
            return False
        order = self.memory.orders.get(name)
        if order and order[0] != actor.id and order[1] > self.w.round and any(r.id == order[0] for r in self.w.actors):
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
                self.purchases.add(name)
                self.memory.orders[name] = (actor.id, self.w.round + 6)
                if self.commands[str(actor.id)]["action"] == "buy":
                    self.budget -= price
                    self.memory.orders.pop(name, None)
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
        if self.urgent_local_supply(actor) or self.rescue_delivery(actor) or self.retreat(actor):
            return
        if actor == self.operator and self.w.day:
            distances, _ = self.route(actor, False)
            if 70 - self.w.day_tick <= distances.get(self.layout.operator, 1000) + 8:
                self.operate(actor)
                return
        if self.use_supplies(actor):
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
            need = self.defence_need()
            if need:
                name, _ = need
                if self.purchase(actor, name, reserve=reserve):
                    return
                if name.startswith("Wall") and self.purchase(actor, "WallFixer", reserve=reserve):
                    return
        if not self.mine(actor):
            self.sell(actor, True)

    def run(self):
        if self.missions:
            self.missions.active(self)
        # Night control is assigned first so a fallback worker never also mines/moves.
        if not self.w.day and self.operator:
            self.operate(self.operator)
            self.used.add(self.operator.id)
        for worker in self.w.workers:
            if worker.id in self.used:
                continue
            self.worker(worker)
        for actor in self.w.actors:
            if actor.kind != "pioneer":
                continue
            if actor.id in self.used:
                continue
            if not self.w.day:
                if actor != self.operator:
                    if not (self.missions and self.missions.treasure(self, actor)):
                        self.use_supplies(actor)
                continue
            return_margin = distance(actor.p, self.layout.operator) + 8
            if actor == self.operator and 70 - self.w.day_tick <= return_margin:
                self.operate(actor)
                continue
            if self.urgent_local_supply(actor):
                continue
            if self.missions and self.missions.choose(self, actor):
                continue
            if self.use_supplies(actor):
                continue
            need = self.defence_need()
            if need and self.purchase(actor, need[0], reserve=25 * max(0, 3 - self.build_count)):
                continue
            upgrade = self.upgrade_order()
            if upgrade and self.purchase(actor, upgrade, reserve=self.defence_reserve()):
                continue
            self.operate(actor)
        if self.missions and not self.w.raw.get("phaseTask") and not self.extra["prompt"]:
            self.extra["prompt"] = self.missions.news.prompt(self.w)
        return {"roleCommandMap": self.commands, **self.extra}


class Agent:
    """Small match-local memory; authoritative positions/resources always come from requests."""
    def __init__(self, state_dir=None):
        self.key = None
        self.last_round = 0
        self.previous = {}
        self.avoided = {}
        self.memory = Memory()
        self.missions = None
        self.state_dir = state_dir or os.environ.get("ZK_STATE_DIR", ".zk_state")
        self.last_response = None

    def decide(self, request):
        world = World(request)
        if not world.station or not world.actors:
            return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
        key = (request["teamOur"].get("teamId"), world.team, world.station.p)
        if key == self.key and world.round == self.last_round and self.last_response is not None:
            return self.last_response
        if key != self.key or world.round < self.last_round:
            self.previous, self.avoided = {}, {}
            self.memory = Memory()
            self.missions = Missions(str(key), self.state_dir)
        if world.round == self.last_round + 1:
            results = request.get("lastRoundRoleActionResults") or {}
            for role, cmd in self.previous.items():
                if results.get(role) is False and cmd["action"] == "collect":
                    target = cmd["targetPos"][0]
                    self.avoided[(target["x"], target["y"])] = world.round + 10
        self.avoided = {p: expiry for p, expiry in self.avoided.items() if expiry > world.round}
        self.memory.observe(world, world.round == self.last_round + 1)
        self.missions.news.observe(world)
        response = Planner(world, self.avoided, self.memory, self.missions).run()
        self.key, self.last_round = key, world.round
        self.previous = response["roleCommandMap"]
        self.last_response = response
        return response
