"""Daytime construction/economy and one-operator nighttime defence."""
from .model import World, Layout, ORES, command, distance, neighbours, wire
from .navigation import paths
from .combat import rocket_targets
from .logistics import upgrade_goal, repair_stock, max_health


class Planner:
    def __init__(self, world, avoided, memory=None):
        self.w = world
        self.layout = Layout.for_world(world)
        self.avoided = avoided
        self.memory = memory if memory is not None else {}
        self.commands = {}
        self.reserved = set()
        self.sites = set()
        self.budget = world.gold
        self.build_count = len(world.towers)
        self.upgrade_reserve = 0
        self.delivery_owner = None
        self.bought = set()
        self.operator = min(world.actors, key=lambda r: (r.kind != "pioneer", distance(r.p, self.layout.operator), r.id)) if world.actors else None
        self.wall_missing = [p for p in self.layout.walls if not any(r.p == p for r in world.walls)]
        self.danger = {p for r in world.robots for x in range(-4, 5) for y in range(-4, 5)
                       for p in [(r.p[0] + x, r.p[1] + y)] if world.inside(p)}
        self.maintainer = max(world.workers, key=lambda r: (r.health >= 80, r.bag.count("WallFixer"), -r.id), default=None)

    def maintenance_cells(self, actor):
        if actor.health < 80:
            return set()
        # A bounded risk allowance inside the base perimeter. This is not a
        # claim that walls block ranged damage; nearby robots still exclude cells.
        return {p for wall in self.w.walls for p in neighbours(wall.p)
                if self.w.inside(p) and self.w.base_distance(p) == 1
                and all(distance(p, robot.p) >= 3 for robot in self.w.robots)}

    def emit(self, actor, cmd):
        self.commands[str(actor.id)] = cmd
        if cmd["action"] in ("move", "build"):
            p = cmd["targetPos"][0]
            self.reserved.add((p["x"], p["y"]))
        return True

    def route(self, actor, avoid_danger=True, maintenance=False):
        forbidden = set(self.layout.towers)
        if actor != self.operator:
            forbidden.add(self.layout.operator)
        if avoid_danger:
            forbidden |= self.danger - (self.maintenance_cells(actor) if maintenance else set())
        return paths(self.w, actor, self.reserved, forbidden)

    def approach(self, actor, target, action=None, exact=False, safe=True, maintenance=False):
        distances, first = self.route(actor, safe, maintenance)
        danger = self.danger - (self.maintenance_cells(actor) if maintenance else set())
        goals = [target] if exact else neighbours(target)
        goals = [p for p in goals if p in distances and (not safe or p not in danger)]
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
        goal = upgrade_goal(self.w)
        gap = self.w.shop.get(goal[0], 0) - self.budget if goal else 0
        value = sum(n * self.w.prices[k] for k, n in amounts.items())
        unlocks_upgrade = 0 < gap <= value
        if not force and not adjacent and not unlocks_upgrade and sum(amounts.values()) < 12 and len(actor.bag) < actor.capacity:
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
            if unit.kind == "wall" and self.repair_needed(unit) and "WallFixer" in actor.bag:
                options.append((0 if unit.health < 350 else 2 + unit.health / max_health(unit), distance(actor.p, unit.p), unit.p, "WallFixer"))
        for _, _, target, name in sorted(options):
            if self.approach(actor, target, command("use", target, name=name), maintenance=True):
                self.sites.add(target)
                return True
        return False

    def purchase(self, actor, name, reserve=0, quantity=1):
        if name not in self.w.shop or len(actor.bag) >= actor.capacity:
            return False
        price = self.w.shop[name]
        if actor != self.delivery_owner:
            reserve += self.upgrade_reserve
        quantity = min(quantity, actor.capacity - len(actor.bag), (self.budget - reserve) // max(1, price))
        if quantity < 1:
            return False
        # Do not send a second courier for an item already being delivered.
        if name not in {"WallFixer", "Medicine"} and (name in self.bought or any(name in r.bag for r in self.w.actors)):
            return False
        shops = sorted((p for p, k in self.w.zones.items() if k == "weaponShop"), key=lambda p: distance(actor.p, p))
        for target in shops:
            if actor == self.operator:
                distances, _ = self.route(actor)
                travel = min((distances[q] for q in neighbours(target) if q in distances), default=10**6)
                # A conservative return allowance keeps the gunner home at dusk.
                if not self.w.day or travel + distance(target, self.layout.operator) + 8 >= 70 - self.w.day_tick:
                    continue
            if self.approach(actor, target, command("buy", name=name, num=quantity)):
                if self.commands[str(actor.id)]["action"] == "buy":
                    self.budget -= price * quantity
                    self.bought.add(name)
                return True
        return False

    def upgrade_order(self):
        goal = upgrade_goal(self.w)
        return goal[0] if goal else None

    def home_distance(self, actor):
        distances, _ = self.route(actor, avoid_danger=False)
        return distances.get(self.layout.operator, 10**6)

    def repair_needed(self, wall):
        pressure = self.memory.get("damage", {}).get(wall.id, 0)
        threshold = max(0.65 * max_health(wall), 2 * pressure + 200)
        return wall.health < min(max_health(wall) - 100, threshold)

    def delivery(self):
        """Assign one courier, reserve its purchase and keep it until delivery.

        The night operator is never assigned. A future task scheduler can also
        exclude its pioneer here while phaseTask is active.
        """
        goal = upgrade_goal(self.w)
        self.memory.pop("delivery_active", None)
        if not goal:
            self.memory.pop("courier", None)
            return
        name, target = goal
        # When both base upgrades are due, do not spend their second voucher's
        # money on repeated shopping while the first voucher is in transit.
        followup_reserve = (self.w.shop.get("StationUpgradeVoucher2", 150)
                            if name == "StationUpgradeVoucher1" and self.w.round >= 261
                            and any(r.kind == "rocket" and r.level == 3 for r in self.w.towers) else 0)
        carriers = [r for r in self.w.actors if name in r.bag]
        price = self.w.shop.get(name)
        build_reserve = 25 * max(0, 3 - self.build_count)
        if not carriers and (price is None or self.budget < price + build_reserve):
            return
        candidates = []
        for actor in carriers or self.w.actors:
            if not self.w.day and actor == self.operator:
                continue
            if actor.kind == "pioneer" and self.w.raw.get("phaseTask"):
                continue
            if actor.health < 80 or (not carriers and len(actor.bag) >= actor.capacity):
                continue
            if (not carriers and not self.w.day and actor == self.maintainer
                    and "WallFixer" in actor.bag and any(self.w.base_distance(r.p) <= 8 for r in self.w.robots)):
                continue
            distances, _ = self.route(actor, maintenance=True)
            if carriers:
                travel = min((distances.get(q, 10**6) for q in neighbours(target.p)), default=10**6)
            else:
                shops = [q for p, k in self.w.zones.items() if k == "weaponShop" for q in neighbours(p) if q in distances]
                if not shops:
                    continue
                shop = min(shops, key=lambda q: distances[q])
                travel = distances[shop]
                from_shop, _ = paths(self.w, actor, self.reserved, self.danger, start=shop, ignore_actors=True)
                target_trip = min((from_shop.get(q, 10**6) for q in neighbours(target.p)), default=10**6)
                if target_trip >= 10**6:
                    continue
            if actor == self.operator:
                # Return distance comes from BFS too, including the built walls.
                home_paths, _ = paths(self.w, actor, self.reserved, start=self.layout.operator)
                after_delivery = min((home_paths.get(q, 10**6) for q in neighbours(target.p)
                                      if q not in self.w.blocked or q == actor.p), default=10**6)
                trip = travel + (0 if carriers else target_trip + 1) + after_delivery + 5
                if not self.w.day or trip >= 70 - self.w.day_tick:
                    continue
            previous = self.memory.get("courier") == (actor.id, name, target.id)
            candidates.append((not previous, travel + (2 if actor.kind == "worker" else 0), actor.id, actor))
        if not candidates:
            return
        actor = min(candidates, key=lambda v: v[:3])[3]
        self.delivery_owner = actor
        self.memory["courier"] = (actor.id, name, target.id)
        self.memory["delivery_active"] = name
        if carriers:
            self.upgrade_reserve = followup_reserve
            success = self.approach(actor, target.p, command("use", target.p, name=name), maintenance=True)
        else:
            self.upgrade_reserve = price + followup_reserve
            success = self.purchase(actor, name, reserve=build_reserve)
            if success and self.commands[str(actor.id)]["action"] == "buy":
                self.upgrade_reserve = followup_reserve
        if success:
            self.sites.add(target.p)
        else:
            self.upgrade_reserve = 0

    def maintenance(self, actor):
        if actor != self.maintainer or self.build_count < 3 or self.wall_missing:
            return False
        stock = repair_stock(self.w)
        carried = actor.bag.count("WallFixer")
        damaged = sorted((r for r in self.w.walls if self.repair_needed(r)), key=lambda r: (r.health, r.id))
        if damaged and damaged[0].level < 3:
            if self.purchase(actor, f"WallUpgradeVoucher{damaged[0].level}"):
                return True
        if carried < stock:
            if self.purchase(actor, "WallFixer", quantity=stock - carried):
                return True
        if stock and carried and (not self.w.day or self.w.day_tick >= 50):
            if not self.w.robots and not self.w.day:
                return False
            # Stand on the interior side of the front wall, adjacent to several
            # sections. Recompute if walls are destroyed or another actor blocks it.
            cells = self.maintenance_cells(actor) - self.w.blocked - self.reserved - set(self.layout.towers) - {self.layout.operator}
            if actor.p in self.maintenance_cells(actor):
                cells.add(actor.p)
            front = self.layout.walls[:6]
            goals = sorted(cells, key=lambda p: (-sum(distance(p, q) == 1 for q in front),
                                                distance(p, actor.p), p))
            for p in goals:
                if self.approach(actor, p, exact=True, maintenance=True):
                    return True
        return False

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
        if actor == self.maintainer and "WallFixer" in actor.bag and actor.p in self.maintenance_cells(actor):
            return False
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
        if self.use_supplies(actor) or self.retreat(actor):
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
        if self.maintenance(actor):
            return
        if not self.mine(actor):
            if self.sell(actor, True):
                return
            # An idle worker at the narrow rear entrance can prevent every base
            # delivery. Yield outwards; real movement still respects all actors.
            if self.w.base_distance(actor.p) <= 1:
                distances, first = self.route(actor)
                exits = [p for p in distances if self.w.base_distance(p) >= 3]
                if exits:
                    goal = min(exits, key=lambda p: (distances[p], p))
                    self.emit(actor, command("move", first[goal]))

    def run(self):
        # Night control is assigned first so a fallback worker never also mines/moves.
        if not self.w.day and self.operator:
            self.operate(self.operator)
        self.delivery()
        for worker in self.w.workers:
            if (not self.w.day and worker == self.operator) or str(worker.id) in self.commands:
                continue
            self.worker(worker)
        for actor in self.w.actors:
            if actor.kind != "pioneer":
                continue
            if str(actor.id) in self.commands:
                continue
            if not self.w.day:
                if actor != self.operator:
                    self.use_supplies(actor)
                continue
            return_margin = self.home_distance(actor) + 5
            if 70 - self.w.day_tick <= return_margin:
                self.operate(actor)
                continue
            if self.use_supplies(actor):
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
        self.memory = {}
        self.health = {}

    def decide(self, request):
        world = World(request)
        if not world.station or not world.actors:
            return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
        key = (request["teamOur"].get("teamId"), world.team, world.station.p)
        if key != self.key or world.round <= self.last_round:
            self.previous, self.avoided = {}, {}
            self.memory, self.health = {}, {}
        if world.round == self.last_round + 1:
            previous_damage = self.memory.get("damage", {})
            self.memory["damage"] = {r.id: max(0, self.health.get(r.id, r.health) - r.health,
                                                 previous_damage.get(r.id, 0) * 0.8) for r in world.walls}
        else:
            self.memory.pop("damage", None)
        if world.round == self.last_round + 1:
            results = request.get("lastRoundRoleActionResults", {})
            for role, cmd in self.previous.items():
                if results.get(role) is False and cmd["action"] == "collect":
                    target = cmd["targetPos"][0]
                    self.avoided[(target["x"], target["y"])] = world.round + 10
        self.avoided = {p: expiry for p, expiry in self.avoided.items() if expiry > world.round}
        response = Planner(world, self.avoided, self.memory).run()
        self.health = {r.id: r.health for r in world.ours}
        self.key, self.last_round = key, world.round
        self.previous = response["roleCommandMap"]
        return response
