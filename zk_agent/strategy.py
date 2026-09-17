"""Fixed economy/defence rules, skill reuse, and evidence-gated rocket attacks."""
from collections import Counter
from itertools import permutations
import logging

from .tasks import TaskRunner
from .world import World, TOWERS, around, command, distance, footprint, pos

LOG = logging.getLogger(__name__)


class Agent:
    def __init__(self, config):
        self.config = config
        self.identity = None
        self.last_round = 0
        self.last_response = None

    def reset(self, world, identity):
        self.identity = identity
        self.tasks = TaskRunner(self.config, identity)
        self.pvp = "unknown"
        self.probe = None
        self.probe_misses = 0
        self.failed_sites = {}
        self.builds = {}
        self.last_base_hp = None
        self.last_day = world.day
        self.night_hurt = False
        self.recall = False
        self.last_response = None
        self.last_round = 0

    def decide(self, data):
        w = World(data)
        identity = str(data["teamOur"].get("teamId", "team")) + ":" + w.team
        if identity != self.identity or w.round < self.last_round:
            self.reset(w, identity)
        if w.round == self.last_round and self.last_response is not None:
            return self.last_response
        self.observe(w)
        response = {"roleCommandMap": w.commands, "prompt": "", "executeCmd": ""}
        if w.station:
            sites, walls = self.layout(w)
            self.build_sites = sites
            self.wall_sites = walls
            self.assignments = self.assign(w)
            # During an active task the pioneer stays put; the command/LLM pipeline
            # runs independently of the two workers. Base emergencies cancel it.
            if data.get("phaseTask") and w.pioneer and not self.emergency(w):
                extra = self.tasks.step(w)
                response.update({k: v for k, v in extra.items() if k in ("prompt", "executeCmd")})
                w.used.add(w.pioneer["id"])  # Waiting for a result also means staying at the task point.
            else:
                if not data.get("phaseTask"):
                    self.tasks.step(w)  # Confirm completion and save successful solvers.
                self.pioneer(w)
            if w.daylight:
                for worker in w.workers:
                    self.worker(w, worker)
            else:
                self.fight(w)
        self.last_round = w.round
        self.last_response = response
        return response

    def observe(self, w):
        results = w.data.get("lastRoundRoleActionResults") or {}
        for actor, (site, kind) in self.builds.items():
            if results.get(str(actor), results.get(actor)) is False:
                self.failed_sites[(site, kind)] = w.round + 130
                LOG.info("build rejected: %s %s; trying another site", kind, site)
        self.builds = {}
        if w.day != self.last_day:
            self.recall = self.night_hurt
            self.night_hurt = False
            self.last_day = w.day
        if w.station:
            hp = w.station["health"]
            if self.last_base_hp is not None and hp < self.last_base_hp:
                self.night_hurt = True
            self.last_base_hp = hp
        if self.probe:
            old = self.probe
            self.probe = None
            if w.round != old["round"] + 1:
                return  # No attribution across skipped turns.
            valid = results.get(str(old["id"]), results.get(old["id"]))
            enemy = w.enemy_station
            if valid is False:
                self.pvp = "unsupported"
            elif enemy and enemy.get("level", 1) == old["level"] and old["clean"]:
                damage = old["hp"] - enemy["health"]
                if damage > 0:
                    self.pvp = "supported"
                    LOG.info("rocket PvP observed damage=%s expected_direct=%s; attribution remains empirical",
                             damage, old["expected"])
                elif valid is True and damage == 0:
                    self.probe_misses += 1
                    if self.probe_misses >= 2:
                        self.pvp = "unsupported"
            LOG.info("rocket PvP mode evidence=%s", self.pvp)

    def emergency(self, w):
        return bool(w.station and not w.healthy_base() and
                    any(w.base_distance(pos(r)) <= 6 for r in w.home_threats()))

    def layout(self, w):
        x, y = pos(w.station)
        base = footprint(w.station)
        centre = pos(w.enemy_station) if w.enemy_station else (w.width // 2, w.height // 2)

        def ring(radius):
            return [(a, b) for a in range(x - radius, x + 2 + radius)
                    for b in range(y - 1 - radius, y + 1 + radius)
                    if min(distance((a, b), p) for p in base) == radius and w.inside((a, b))]

        towers = [(x + dx, y + dy) for dx, dy in self.config.tower_offsets] or ring(1)
        walls = [(x + dx, y + dy) for dx, dy in self.config.wall_offsets] or ring(2)
        towers.sort(key=lambda p: (distance(p, centre), p))
        walls.sort(key=lambda p: (distance(p, centre), p))
        if not self.config.wall_offsets:
            # Always leave the merchant-facing gate and its immediate neighbours open.
            vendor = w.zone_cells("vendor")
            gate = min(walls, key=lambda p: distance(p, vendor[0] if vendor else centre)) if walls else None
            walls = [p for p in walls if gate is None or distance(p, gate) > 1]
        return towers, walls

    def assign(self, w):
        people = list(w.workers)
        if w.pioneer and (self.recall or self.emergency(w)):
            people.append(w.pioneer)
        towers = sorted(w.towers, key=lambda t: (-t.get("level", 1), t["roleType"] != "rocket", t["id"]))[:len(people)]
        if not towers:
            return {}
        best = min(permutations(people, len(towers)),
                   key=lambda units: sum(distance(pos(a), pos(t)) for a, t in zip(units, towers)))
        return {a["id"]: t for a, t in zip(best, towers)}

    def home(self, w, actor):
        tower = self.assignments.get(actor["id"])
        return {pos(tower)} if tower else footprint(w.station)

    def must_return(self, w, actor):
        path = w.adjacent_path(actor, self.home(w, actor))
        return path is None or w.day_left <= path.length + self.config.return_margin

    def pioneer(self, w):
        actor = w.pioneer
        if not actor:
            return
        if self.emergency(w) or (self.recall and (not w.daylight or self.must_return(w, actor))):
            w.walk(actor, self.home(w, actor))
            return
        if actor["id"] in self.assignments and not w.daylight:
            return
        candidates = []
        for index, task in enumerate(w.tasks):
            if not task.get("isValid") or task.get("coldDownRounds", 0) > 0:
                continue
            name = task.get("taskType", "")
            suffix = "2" if "2" in name else "1"
            cells = w.zone_cells(w.team + "TaskPoint" + suffix)
            if not cells:
                value = task.get("taskPosition")
                cells = [(value["x"], value["y"])] if value else []
            path = w.adjacent_path(actor, cells)
            if path is None:
                continue
            if self.recall:
                duration = task.get("timeoutRounds", 30)
                back = w.adjacent_path(actor, self.home(w, actor))
                if back is None or path.length + duration + back.length + self.config.return_margin >= w.day_left:
                    continue
            # Reachable, high-reward tasks first; no learned world model.
            reward = task.get("scoreReward", 0) + 5 * task.get("timeoutRounds", 0)
            candidates.append((path.length, -reward, index, cells, path))
        if candidates:
            _, _, _, cells, path = min(candidates)
            if path.length == 0:
                w.put(actor, command("acceptTask"))
            else:
                w.put(actor, command("move", [path.step]))

    def worker(self, w, actor):
        if actor["id"] in w.used:
            return
        if self.maintain(w, actor):
            return
        bag = Counter(actor.get("backpack", []))
        if self.must_return(w, actor):
            w.walk(actor, self.home(w, actor))
            return
        target_count = min(3, max(self.config.initial_towers, 3 if self.recall else 0))
        pending = sum(1 for _, kind in self.builds.values() if kind in TOWERS)
        if len(w.towers) + pending < target_count and w.gold >= 25:
            index = len(w.towers) + pending
            kind = self.config.tower_loadout[index]
            if self.build(w, actor, self.build_sites, kind):
                return
        # Deliver any purchased voucher before buying another one.
        for building in [w.station] + sorted(w.towers, key=lambda u: u["roleType"] != "rocket"):
            level = building.get("level", 1)
            if level >= 3:
                continue
            prefix = "Station" if building["roleType"] == "station" else "Weapon"
            name = f"{prefix}UpgradeVoucher{level}"
            if bag[name]:
                cells = footprint(building)
                if w.at(actor, cells):
                    w.put(actor, command("use", [pos(building)], name=name))
                else:
                    w.walk(actor, cells)
                return
        upgrade = self.upgrade_choice(w)
        if upgrade:
            name, target = upgrade
            price = w.shop.get(name)
            held = any(name in p.get("backpack", []) for p in w.people)
            purchased = any(c.get("action") == "buy" and c.get("name") == name for c in w.commands.values())
            reserve = 25 * max(0, target_count - len(w.towers) - pending)
            if price is not None and price > 0 and w.gold >= price + reserve and not held and not purchased:
                shops = w.zone_cells("weaponShop")
                trip = w.adjacent_path(actor, shops)
                capacity = actor.get("backPackCapability", 100)
                if trip and len(actor.get("backpack", [])) < capacity:
                    if w.at(actor, shops):
                        if w.put(actor, command("buy", name=name, num=1)):
                            w.gold -= price
                        return
                    # Do not start a shopping trip too late to bring the voucher home.
                    back = min((distance(p, pos(target)) for p in shops), default=999)
                    if trip.length + back + 4 + self.config.return_margin < w.day_left:
                        w.put(actor, command("move", [trip.step]))
                        return
        wall_count = len(w.walls) + sum(kind == "wall" for _, kind in self.builds.values())
        if bag["stone"] and wall_count < self.config.wall_limit:
            if self.build(w, actor, self.wall_sites, "wall"):
                return
        need_stone = wall_count < self.config.wall_limit and actor == w.workers[0]
        self.mine_or_sell(w, actor, need_stone)

    def build(self, w, actor, sites, kind):
        options = []
        for site in sites:
            if not w.inside(site) or site in w.blocked or site in w.reserved:
                continue
            if self.failed_sites.get((site, kind), 0) >= w.round:
                continue
            path = w.adjacent_path(actor, {site})
            if path:
                options.append((path.length, sites.index(site), site, path))
        if not options:
            return False
        _, _, site, path = min(options)
        if path.length:
            return w.put(actor, command("move", [path.step]))
        if w.put(actor, command("build", [site], name=kind)):
            w.reserved.add(site)
            self.builds[actor["id"]] = (site, kind)
            if kind in TOWERS:
                w.gold -= 25
            return True
        return False

    def upgrade_choice(self, w):
        station_level = w.station.get("level", 1)
        if station_level < 3 and not w.healthy_base():
            return f"StationUpgradeVoucher{station_level}", w.station
        rockets = sorted([t for t in w.towers if t["roleType"] == "rocket"], key=lambda t: -t.get("level", 1))
        for tower in rockets:
            level = tower.get("level", 1)
            if level < 3:
                return f"WeaponUpgradeVoucher{level}", tower
        if station_level < 3:
            return f"StationUpgradeVoucher{station_level}", w.station
        for tower in w.towers:
            level = tower.get("level", 1)
            if level < 3:
                return f"WeaponUpgradeVoucher{level}", tower
        return None

    def maintain(self, w, actor):
        bag = actor.get("backpack", [])
        if actor["health"] < 100 and "Medicine" in bag:
            return w.put(actor, command("use", name="Medicine"))
        level = w.station.get("level", 1)
        voucher = f"StationUpgradeVoucher{level}"
        if level < 3 and not w.healthy_base() and voucher in bag and w.at(actor, footprint(w.station)):
            return w.put(actor, command("use", [pos(w.station)], name=voucher))
        if "WallFixer" in bag:
            for wall in w.walls:
                if wall["health"] < 400 and w.at(actor, footprint(wall)):
                    return w.put(actor, command("use", [pos(wall)], name="WallFixer"))
        return False

    def mine_or_sell(self, w, actor, need_stone):
        bag = Counter(actor.get("backpack", []))
        ores = {k: v for k, v in bag.items() if k in w.prices and v > 0 and w.prices[k] > 0}
        if need_stone:
            ores.pop("stone", None)
        vendors = w.zone_cells("vendor")
        stock = sum(ores.values())
        full = len(actor.get("backpack", [])) >= actor.get("backPackCapability", 100)
        if stock and (stock >= self.config.sale_batch or full or w.at(actor, vendors)):
            if w.at(actor, vendors):
                ore = max(ores, key=lambda k: ores[k] * w.prices[k])
                w.put(actor, command("sell", name=ore, num=ores[ore]))
                # Proceeds are usable next turn only: same-turn ordering is unspecified.
            else:
                w.walk(actor, vendors)
            return
        if full:
            return
        mines = []
        for zone in w.zones:
            kind = zone["neutralType"]
            if kind not in {"stone", "iron", "copper"} or (need_stone and kind != "stone"):
                continue
            point = pos(zone)
            if self.failed_sites.get((point, "mine"), 0) >= w.round:
                continue
            path = w.adjacent_path(actor, {point})
            if path:
                price = 1 if need_stone else w.prices.get(kind, 0)
                if price <= 0:
                    continue
                sale_distance = min((distance(point, v) for v in vendors), default=999)
                # Fixed batch estimate; prices come from the actual request.
                cost = path.length + (0 if need_stone else sale_distance + 1) + (1 if need_stone else 8)
                mines.append((-(price * (1 if need_stone else 8)) / max(1, cost), point, path))
        if mines:
            _, point, path = min(mines)
            if path.length == 0:
                w.put(actor, command("collect", [point]))
                self.builds[actor["id"]] = (point, "mine")
            else:
                w.put(actor, command("move", [path.step]))
        elif stock:
            if w.at(actor, vendors):
                ore = max(ores, key=lambda k: ores[k] * w.prices[k])
                w.put(actor, command("sell", name=ore, num=ores[ore]))
            else:
                w.walk(actor, vendors)

    def fight(self, w):
        projected = {r["id"]: r["health"] for r in w.robots}
        for actor in w.people:
            if actor["id"] in w.used:
                continue
            if self.maintain(w, actor):
                continue
            tower = self.assignments.get(actor["id"])
            if not tower:
                if actor["roleType"] == "worker":
                    w.walk(actor, footprint(w.station))
                continue
            if distance(pos(actor), pos(tower)) > 1:
                w.walk(actor, {pos(tower)})
                continue
            if tower.get("cooldown", 0) > 0:
                continue
            level = min(3, max(1, tower.get("level", 1)))
            kind = tower["roleType"]
            tables = {"rocket": [10, 15, 10**9], "gatling": [3, 5, 7], "railgun": [6, 8, 10]}
            reach = tower.get("attackRange") or tables[kind][level - 1]
            urgent = any(w.base_distance(pos(r)) <= 8 or
                         any(distance(pos(r), pos(a)) <= 4 for a in w.workers)
                         for r in w.home_threats())
            enemy = w.enemy_station
            can_pvp = (kind == "rocket" and level == 3 and enemy and
                       distance(pos(tower), pos(enemy)) <= reach and not urgent and w.healthy_base())
            allowed = self.config.pvp_mode == "on" or (self.config.pvp_mode == "auto" and self.pvp != "unsupported")
            # At most one unverified probe per turn; use other weapons for defence.
            if can_pvp and allowed and (self.pvp == "supported" or self.config.pvp_mode == "on" or self.probe is None):
                aim = pos(enemy)
                if self.pvp == "supported" or self.config.pvp_mode == "on":
                    visible_operators = [u for u in w.enemy if u["roleType"] in {"worker", "pioneer"}
                                         and distance(pos(tower), pos(u)) <= reach]
                    if visible_operators:
                        victim = min(visible_operators, key=lambda u: (u["health"], u["roleType"] != "pioneer", u["id"]))
                        aim = pos(victim)
                shots = [aim] * level
                if w.put(actor, command("attack", shots, controllerId=str(actor["id"])), key=tower["id"]):
                    if self.pvp == "unknown" and self.config.pvp_mode == "auto":
                        clean = not any(w.base_distance(pos(r), enemy=True) <= 4 for r in w.robots)
                        self.probe = {"round": w.round, "id": tower["id"], "hp": enemy["health"],
                                      "level": enemy.get("level", 1), "clean": clean, "expected": 20 * level}
                    continue
            targets = [r for r in w.home_threats() if distance(pos(tower), pos(r)) <= reach and projected[r["id"]] > 0]
            if not targets:
                continue
            if kind == "rocket":
                shots = []
                for _ in range(level):
                    # Include neighbouring cells to exploit splash without assuming base hit duplication.
                    values = {}
                    for robot in targets:
                        centre = pos(robot)
                        weight = 3 if w.base_distance(centre) <= 4 else 1
                        for point in [centre] + around(centre):
                            if w.inside(point) and distance(pos(tower), point) <= reach:
                                damage = 20 if point == centre else 10
                                values[point] = values.get(point, 0) + min(projected[robot["id"]], damage) * weight
                    best = max(sorted(values), key=values.get)
                    shots.append(best)
                    for robot in w.robots:
                        d = distance(best, pos(robot))
                        damage = 20 if d == 0 else (10 if d == 1 else 0)
                        projected[robot["id"]] = max(0, projected[robot["id"]] - damage)
            else:
                target = min(targets, key=lambda r: (w.base_distance(pos(r)), projected[r["id"]], r["id"]))
                # One repeated direction always satisfies Gatling's 90-degree cone.
                shots = [pos(target)] * (level if kind == "gatling" else 1)
                # Do not predict direct damage: line interception/penetration is engine-defined.
            w.put(actor, command("attack", shots, controllerId=str(actor["id"])), key=tower["id"])
