"""Fixed economy/defence rules, skill reuse, and evidence-gated rocket attacks."""
from collections import Counter
from itertools import permutations
import logging

from .tasks import TaskRunner
from .world import World, TOWERS, action_key, around, command, distance, footprint, pos

LOG = logging.getLogger(__name__)


class Agent:
    def __init__(self, config):
        self.config = config
        self.identity = None
        self.last_round = 0
        self.idle = {}
        self.last_positions = {}
        self.bad_steps = {}
        self.bad_actions = {}
        self.claimed_jobs = set()
        self.abandoned_task = None
        self.summons_used = 0
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
        self.idle = {}
        self.last_positions = {}
        self.bad_steps = {}
        self.bad_actions = {}
        self.abandoned_task = None
        self.summons_used = 0

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
            self.claimed_jobs = set()
            self.duty_groups = self.assign(w)
            self.assignments = {uid: group[0] for uid, group in self.duty_groups.items()}
            # During an active task the pioneer stays put; the command/LLM pipeline
            # runs independently of the two workers. Base emergencies cancel it.
            if data.get("phaseTask") and w.pioneer and not self.emergency(w):
                extra = {} if self.abandoned_task else self.tasks.step(w)
                if extra.get("exhausted") or self.abandoned_task:
                    self.abandoned_task = data["phaseTask"]
                    self.leave_task(w)
                else:
                    response.update({k: v for k, v in extra.items() if k in ("prompt", "executeCmd")})
                    w.used.add(w.pioneer["id"])
                    w.reasons[w.pioneer["id"]] = "task_result_pending"
            else:
                if not data.get("phaseTask"):
                    self.abandoned_task = None
                    self.tasks.step(w)  # Confirm completion and save successful solvers.
                self.pioneer(w)
            if w.daylight:
                for worker in w.workers:
                    self.worker(w, worker)
            else:
                self.fight(w)
            for actor in w.people:
                if actor["id"] not in w.used:
                    self.fallback(w, actor)
        self.record_utilization(w)
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
        if self.last_response and w.round == self.last_round + 1:
            for uid, value in self.last_response["roleCommandMap"].items():
                if results.get(uid, results.get(int(uid))) is False:
                    self.bad_actions[action_key(int(uid), value)] = w.round + 3
                if value["action"] == "move" and results.get(uid, results.get(int(uid))) is False:
                    point = value["targetPos"][0]
                    self.bad_steps[(int(uid), (point["x"], point["y"]))] = w.round + 2
        self.bad_steps = {key: until for key, until in self.bad_steps.items() if until >= w.round}
        self.bad_actions = {key: until for key, until in self.bad_actions.items() if until >= w.round}
        w.blocked_actions = set(self.bad_actions)
        if w.day != self.last_day:
            self.recall = self.night_hurt
            self.night_hurt = False
            self.last_day = w.day
            self.summons_used = 0
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
        return bool(w.station and (
            (not w.healthy_base() and any(w.base_distance(pos(r)) <= 6 for r in w.home_threats()))
            or (len(w.workers) < 2 and any(w.base_distance(pos(r)) <= 8 for r in w.home_threats()))))

    def layout(self, w):
        x, y = pos(w.station)
        base = footprint(w.station)
        # Spawn direction is central; lower-right is the 180-degree mirror of upper-left.
        sx = 1 if x < w.width / 2 else -1
        sy = -1 if y > w.height / 2 else 1
        self.facing = sx, sy

        def ring(radius):
            return [(a, b) for a in range(x - radius, x + 2 + radius)
                    for b in range(y - 1 - radius, y + 1 + radius)
                    if min(distance((a, b), p) for p in base) == radius and w.inside((a, b))]

        if sx == 1:
            fixed = [(x + 2, y), (x + 1, y - 2), (x + 2, y + 1)]
        else:
            fixed = [(x - 1, y - 1), (x, y + 1), (x - 1, y - 2)]
        towers = [(x + dx, y + dy) for dx, dy in self.config.tower_offsets] or fixed
        self.tower_slots = list(zip(self.config.tower_loadout, towers[:3]))
        if not self.config.tower_offsets:
            towers += [p for p in ring(1) if p not in towers]
        front_x = x + 3 if sx > 0 else x - 2
        front_y = y + 2 if sy > 0 else y - 3
        walls = [(front_x, front_y)]
        for step in range(1, 6):
            walls.extend([(front_x - sx * step, front_y), (front_x, front_y - sy * step)])
        if self.config.wall_offsets:
            walls = [(x + dx, y + dy) for dx, dy in self.config.wall_offsets]
        walls = [p for p in walls if w.inside(p)][:self.config.wall_limit]
        return towers, walls

    def assign(self, w):
        people = list(w.workers)
        if w.pioneer and (self.emergency(w) or (not w.data.get("phaseTask") and len(people) < 2)):
            people.append(w.pioneer)
        if not people or not w.towers:
            return {}
        towers = sorted(w.towers, key=lambda t: (-t.get("level", 1), t["roleType"] != "rocket", t["id"]))
        rockets = [t for t in towers if t["roleType"] == "rocket"]
        groups = []
        if len(rockets) >= 2:
            pair = rockets[:2]
            stands = self.group_stands(pair)
            # A person temporarily occupying the common stand must not destroy the plan.
            structures = w.blocked - {pos(p) for p in w.people}
            if any(w.inside(p) and p not in structures for p in stands):
                groups.append(pair)
                towers = [t for t in towers if t not in pair]
        groups += [[t] for t in towers]
        if len(groups) > len(people) and w.pioneer and not w.data.get("phaseTask") and w.pioneer not in people:
            people.append(w.pioneer)
        groups = groups[:len(people)]
        best = min(permutations(people, len(groups)), key=lambda units: sum(
            (path.length if (path := self.path(w, actor, self.group_stands(group))) else 10000)
            for actor, group in zip(units, groups)))
        return {a["id"]: group for a, group in zip(best, groups)}

    @staticmethod
    def group_stands(group):
        return set.intersection(*(set(around(pos(t))) for t in group)) if group else set()

    def path(self, w, actor, goals):
        blocked = {point for (uid, point), _ in self.bad_steps.items() if uid == actor["id"]}
        return w.path(actor, goals, extra_blocked=blocked)

    def walk(self, w, actor, cells):
        goals = {p for c in cells for p in around(c)} - set(cells)
        path = self.path(w, actor, goals)
        return bool(path and path.length and w.put(actor, command("move", [path.step])))

    def home_path(self, w, actor):
        group = self.duty_groups.get(actor["id"], [])
        if group:
            path = self.path(w, actor, self.group_stands(group))
            if path is not None:
                return path
        goals = {p for c in footprint(w.station) for p in around(c)} - footprint(w.station)
        return self.path(w, actor, goals)

    def return_home(self, w, actor):
        path = self.home_path(w, actor)
        return bool(path and path.length and w.put(actor, command("move", [path.step])))

    def home(self, w, actor):
        tower = self.assignments.get(actor["id"])
        return {pos(tower)} if tower else footprint(w.station)

    def must_return(self, w, actor):
        path = self.home_path(w, actor)
        return w.day_left <= (path.length if path else 12) + self.config.return_margin

    def pioneer(self, w):
        actor = w.pioneer
        if not actor:
            return
        if self.emergency(w):
            if not self.return_home(w, actor):
                self.maintain(w, actor)
            return
        if actor["id"] in self.duty_groups and (not w.daylight or self.must_return(w, actor)):
            if w.daylight:
                self.return_home(w, actor)
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
            path = self.path(w, actor, {p for c in cells for p in around(c)} - set(cells))
            if path is None:
                continue
            if actor["id"] in self.duty_groups:
                duration = task.get("timeoutRounds", 30)
                back = self.home_path(w, actor)
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

    def leave_task(self, w):
        actor = w.pioneer
        points = [pos(z) for z in w.zones if z["neutralType"].startswith(w.team + "TaskPoint")]
        goals = {p for p in around(pos(actor)) if not any(distance(p, t) <= 1 for t in points)}
        if not goals:
            goals = {(x, y) for x in range(w.width) for y in range(w.height)
                     if not any(distance((x, y), t) <= 1 for t in points)}
        path = self.path(w, actor, goals)
        if path and path.length:
            w.put(actor, command("move", [path.step]))
        w.used.add(actor["id"])
        w.reasons[actor["id"]] = "exhausted_task_exit" if path else "task_exit_blocked"

    def target_tower_count(self, w):
        expanding = self.recall or (w.gold >= 25 + self.config.third_tower_reserve and
                                   (w.day >= 2 or any(t.get("level", 1) >= 2 for t in w.towers)))
        return min(3, max(self.config.initial_towers, 3 if expanding else 0))

    def wall_jobs(self, w):
        occupied = {pos(u) for u in w.walls}
        occupied.update(p for p, kind in self.builds.values() if kind == "wall")
        return [p for p in self.wall_sites if p not in occupied and
                self.failed_sites.get((p, "wall"), 0) < w.round]

    def worker(self, w, actor):
        if actor["id"] in w.used:
            return
        if self.maintain(w, actor):
            return
        bag = Counter(actor.get("backpack", []))
        if self.must_return(w, actor):
            if not self.return_home(w, actor):
                self.nearby_work(w, actor)
            return
        target_count = self.target_tower_count(w)
        pending = sum(1 for _, kind in self.builds.values() if kind in TOWERS)
        if len(w.towers) + pending < target_count and w.gold >= 25:
            existing = Counter(t["roleType"] for t in w.towers)
            existing.update(kind for _, kind in self.builds.values() if kind in TOWERS)
            desired = Counter()
            kind = None
            for item in self.config.tower_loadout[:target_count]:
                desired[item] += 1
                if existing[item] < desired[item]:
                    kind = item
                    break
            slots = [p for typ, p in self.tower_slots if typ == kind]
            reserved_slots = {p for typ, p in self.tower_slots if typ != kind}
            sites = slots + [p for p in self.build_sites if p not in slots and p not in reserved_slots]
            if kind and self.build(w, actor, sites, kind):
                return
        if self.deliver_upgrade(w, actor) or self.shopping(w, actor):
            return
        missing = self.wall_jobs(w)
        if bag["stone"] and missing and self.build(w, actor, missing, "wall"):
            return
        need_stone = bool(missing) and actor == w.workers[0]
        if not self.mine_or_sell(w, actor, need_stone):
            self.mine_or_sell(w, actor, False)

    def deliver_upgrade(self, w, actor, adjacent_only=False):
        for building in [w.station] + sorted(w.towers, key=lambda u: (-u.get("level", 1), u["roleType"] != "rocket")) + w.walls:
            level = building.get("level", 1)
            prefix = {"station": "Station", "wall": "Wall"}.get(building["roleType"], "Weapon")
            name = f"{prefix}UpgradeVoucher{level}"
            if level >= 3 or name not in actor.get("backpack", []):
                continue
            if w.at(actor, footprint(building)):
                return w.put(actor, command("use", [pos(building)], name=name))
            if not adjacent_only and self.walk(w, actor, footprint(building)):
                return True
        return False

    def shopping(self, w, actor, adjacent_only=False):
        upgrade = self.upgrade_choice(w)
        if upgrade:
            name, target = upgrade
            price = w.shop.get(name)
            held = any(name in p.get("backpack", []) for p in w.people)
            purchased = name in self.claimed_jobs or any(c.get("action") == "buy" and c.get("name") == name for c in w.commands.values())
            pending = sum(1 for _, kind in self.builds.values() if kind in TOWERS)
            reserve = 25 * max(0, self.target_tower_count(w) - len(w.towers) - pending)
            if price is not None and price > 0 and w.gold >= price + reserve and not held and not purchased:
                shops = w.zone_cells("weaponShop")
                if not self.best_buyer(w, actor, shops):
                    return self.buy_supplies(w, actor, adjacent_only)
                trip = self.path(w, actor, {p for c in shops for p in around(c)} - set(shops))
                capacity = actor.get("backPackCapability", 100)
                if trip and len(actor.get("backpack", [])) < capacity:
                    if w.at(actor, shops):
                        if w.put(actor, command("buy", name=name, num=1)):
                            w.gold -= price
                            self.claimed_jobs.add(name)
                            return True
                    # Do not start a shopping trip too late to bring the voucher home.
                    back = min((distance(p, pos(target)) for p in shops), default=999)
                    if (not adjacent_only and w.daylight and
                            trip.length + back + 4 + self.config.return_margin < w.day_left):
                        if w.put(actor, command("move", [trip.step])):
                            self.claimed_jobs.add(name)
                            return True
        return self.buy_supplies(w, actor, adjacent_only)

    def build(self, w, actor, sites, kind):
        options = []
        for site in sites:
            if not w.inside(site) or site in w.blocked or site in w.reserved:
                continue
            if self.failed_sites.get((site, kind), 0) >= w.round:
                continue
            if kind == "wall" and not self.wall_preserves_routes(w, site):
                continue
            path = self.path(w, actor, around(site))
            if path:
                options.append((sites.index(site), path.length, site, path))
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

    def wall_preserves_routes(self, w, site):
        vendors = w.zone_cells("vendor")
        goals = {p for c in vendors for p in around(c)} - set(vendors)
        for actor in w.people:
            if w.path(actor, goals) is not None and w.path(actor, goals, extra_blocked={site}) is None:
                return False
        for group in self.duty_groups.values():
            stands = self.group_stands(group)
            for actor in w.workers:
                if w.path(actor, stands) is not None and w.path(actor, stands, extra_blocked={site}) is None:
                    return False
        return True

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
        threats = w.home_threats()
        if threats and "Bomb" in bag:
            centre = max((pos(r) for r in threats), key=lambda p: sum(min(r["health"], 100) for r in threats if distance(p, pos(r)) <= 1))
            value = sum(min(r["health"], 100) for r in threats if distance(centre, pos(r)) <= 1)
            if value >= 150 or (self.emergency(w) and value >= 100):
                return w.put(actor, command("use", [centre], name="Bomb"))
        if "DizzyWeapon" in bag:
            urgent = [r for r in threats if r.get("abnormalState") != "dizzy" and w.base_distance(pos(r)) <= 4]
            if urgent:
                target = max(urgent, key=lambda r: r["health"])
                if target["health"] >= 100 or len(urgent) >= 3:
                    return w.put(actor, command("use", [pos(target)], name="DizzyWeapon"))
        return False

    def buy_supplies(self, w, actor, adjacent_only=False):
        shops = w.zone_cells("weaponShop")
        if not shops or len(actor.get("backpack", [])) >= actor.get("backPackCapability", 100):
            return False
        reserve = 25 * max(0, self.target_tower_count(w) - len(w.towers))
        wanted = []
        if actor["health"] < (160 if actor["roleType"] == "worker" else 140):
            wanted.append("Medicine")
        if any(t["health"] < 500 and pos(t) in self.wall_sites for t in w.walls):
            wanted.append("WallFixer")
        if self.emergency(w):
            wanted.append("Bomb")
        if (self.config.enable_raids and w.daylight and w.day >= 3 and w.healthy_base()
                and w.station.get("level", 1) >= 2 and not self.recall
                and any(t["roleType"] == "rocket" and t.get("level", 1) == 3 for t in w.towers)
                and self.summons_used < 10):
            wanted.append("LargeRobotSummonOrder")
        for name in wanted:
            if name in self.claimed_jobs or any(name in a.get("backpack", []) for a in w.people):
                continue
            price = w.shop.get(name, 0)
            buffer = max(reserve, 150) if "SummonOrder" in name else reserve
            if price <= 0 or w.gold < price + buffer:
                continue
            if name != "Medicine" and not self.best_buyer(w, actor, shops):
                continue
            if w.at(actor, shops):
                if w.put(actor, command("buy", name=name, num=1)):
                    w.gold -= price
                    self.claimed_jobs.add(name)
                    return True
            elif not adjacent_only and w.daylight:
                path = self.path(w, actor, {p for c in shops for p in around(c)} - set(shops))
                home_cost = min((w.base_distance(p) for p in shops), default=999)
                if path and path.length + home_cost + 4 + self.config.return_margin < w.day_left:
                    if w.put(actor, command("move", [path.step])):
                        self.claimed_jobs.add(name)
                        return True
        return False

    def best_buyer(self, w, actor, shops):
        goals = {p for c in shops for p in around(c)} - set(shops)
        choices = []
        for person in w.people:
            if person["id"] in w.used or len(person.get("backpack", [])) >= person.get("backPackCapability", 100):
                continue
            route = self.path(w, person, goals)
            if route is None:
                continue
            if route.length and (not w.daylight or self.must_return(w, person)):
                continue
            choices.append((route.length, person["id"]))
        return bool(choices and min(choices)[1] == actor["id"])

    def use_summon(self, w, actor):
        if not self.config.enable_raids or not w.healthy_base() or self.emergency(w) or self.summons_used >= 10:
            return False
        for name in ("BossRobotSummonOrder", "LargeRobotSummonOrder", "MiddleRobotSummonOrder", "SmallRobotSummonOrder"):
            if name in actor.get("backpack", []) and w.put(actor, command("use", name=name)):
                self.summons_used += 1
                return True
        return False

    def mine_or_sell(self, w, actor, need_stone=False, adjacent_only=False):
        bag = Counter(actor.get("backpack", []))
        ores = {k: v for k, v in bag.items() if k in w.prices and v > 0 and w.prices[k] > 0}
        if need_stone:
            ores.pop("stone", None)
        vendors = w.zone_cells("vendor")
        stock = sum(ores.values())
        full = len(actor.get("backpack", [])) >= actor.get("backPackCapability", 100)
        if stock and (stock >= self.config.sale_batch or full or w.at(actor, vendors) or actor["roleType"] == "pioneer"):
            if w.at(actor, vendors):
                ore = max(ores, key=lambda k: ores[k] * w.prices[k])
                return w.put(actor, command("sell", name=ore, num=ores[ore]))
                # Proceeds are usable next turn only: same-turn ordering is unspecified.
            elif not adjacent_only and self.walk(w, actor, vendors):
                return True
        if full or actor["roleType"] != "worker":
            return False
        mines = []
        for zone in w.zones:
            kind = zone["neutralType"]
            if kind not in {"stone", "iron", "copper"} or (need_stone and kind != "stone"):
                continue
            point = pos(zone)
            if self.failed_sites.get((point, "mine"), 0) >= w.round:
                continue
            path = self.path(w, actor, around(point))
            if adjacent_only and (path is None or path.length):
                continue
            if path:
                if w.daylight and path.length + w.base_distance(point) + self.config.return_margin + 2 >= w.day_left:
                    if not adjacent_only:
                        continue
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
                if w.put(actor, command("collect", [point])):
                    self.builds[actor["id"]] = (point, "mine")
                    return True
            else:
                return w.put(actor, command("move", [path.step]))
        elif stock:
            if w.at(actor, vendors):
                ore = max(ores, key=lambda k: ores[k] * w.prices[k])
                return w.put(actor, command("sell", name=ore, num=ores[ore]))
            elif not adjacent_only:
                return self.walk(w, actor, vendors)
        return False

    def fight(self, w):
        projected = {r["id"]: r["health"] for r in w.robots}
        for actor in w.people:
            if actor["id"] in w.used or self.maintain(w, actor):
                continue
            group = self.duty_groups.get(actor["id"], [])
            available = sorted(group, key=lambda t: (t.get("cooldown", 0), -t.get("level", 1), t["id"]))
            if not group:
                available = [t for t in w.towers if distance(pos(actor), pos(t)) <= 1]
            fired = False
            for tower in available:
                if self.fire(w, actor, tower, projected):
                    fired = True
                    break
            if fired:
                continue
            if actor["health"] < 100 and self.evade(w, actor):
                continue
            if group and self.return_home(w, actor):
                continue
            self.nearby_work(w, actor)

    def nearby_work(self, w, actor):
        return (self.maintain(w, actor) or self.deliver_upgrade(w, actor, adjacent_only=True)
                or self.mine_or_sell(w, actor, adjacent_only=True)
                or self.shopping(w, actor, adjacent_only=True) or self.use_summon(w, actor))

    def open_exit(self, w, actor):
        if actor["roleType"] != "worker":
            return False
        # Remove a specific owned wall only if opening it restores a useful route.
        goals = self.group_stands(self.duty_groups.get(actor["id"], []))
        vendors = w.zone_cells("vendor")
        destinations = [goals, {p for c in vendors for p in around(c)} - set(vendors)]
        for wall in sorted(w.walls, key=lambda t: (pos(t) in self.wall_sites, distance(pos(actor), pos(t)))):
            point = pos(wall)
            if self.failed_sites.get((point, "remove"), 0) >= w.round:
                continue
            if not w.daylight and any(distance(point, pos(r)) <= 5 for r in w.home_threats()):
                continue
            restored = any(target and w.path(actor, target) is None and
                           w.path(actor, target, ignore_blocked={point}) is not None for target in destinations)
            if not restored:
                continue
            if w.at(actor, {point}):
                if w.put(actor, command("remove", [point])):
                    self.builds[actor["id"]] = (point, "remove")
                    # Do not rebuild this opening on the very next day.
                    self.failed_sites[(point, "wall")] = w.round + 130
                    return True
            elif self.walk(w, actor, {point}):
                return True
        return False

    def evade(self, w, actor):
        nearby = [r for r in w.robots if distance(pos(actor), pos(r)) <= 4 and r.get("abnormalState") != "dizzy"]
        if not nearby:
            return False
        current = min(distance(pos(actor), pos(r)) for r in nearby)
        options = [p for p in around(pos(actor)) if w.inside(p) and p not in w.blocked and p not in w.reserved]
        options = [p for p in options if min(distance(p, pos(r)) for r in nearby) > current]
        if not options:
            return False
        best = max(options, key=lambda p: (min(distance(p, pos(r)) for r in nearby), -w.base_distance(p)))
        return w.put(actor, command("move", [best]))

    def scout(self, w, actor):
        if (not self.config.enable_raids or actor["roleType"] != "pioneer" or not w.enemy_station
                or actor["health"] < 140 or not w.healthy_base() or self.emergency(w)
                or (self.pvp != "supported" and self.config.pvp_mode != "on")
                or not any(t["roleType"] == "rocket" and t.get("level", 1) == 3 for t in w.towers)):
            return False
        if any(w.base_distance(pos(r)) <= 8 for r in w.home_threats()):
            return False
        forbidden = {p for p in ((x, y) for x in range(w.width) for y in range(w.height))
                     if any(distance(p, pos(r)) <= 4 for r in w.robots) or
                     any(distance(p, pos(t)) <= (t.get("attackRange") or 3) for t in w.enemy if t["roleType"] in TOWERS)}
        goals = {(x, y) for x in range(w.width) for y in range(w.height)
                 if w.base_distance((x, y), enemy=True) in (3, 4) and (x, y) not in forbidden}
        path = w.path(actor, goals, extra_blocked=forbidden)
        if path is None:
            return False
        cooling = [t["coldDownRounds"] for t in w.tasks if t.get("coldDownRounds", 0) > 0]
        if cooling and min(cooling) <= 2 * path.length + 4:
            return False
        if path.length:
            return w.put(actor, command("move", [path.step]))
        w.reasons[actor["id"]] = "scouting_for_rocket"
        return True

    def stage_task(self, w, actor):
        if actor["roleType"] != "pioneer":
            return False
        options = []
        for task in w.tasks:
            if not task.get("coldDownRounds", 0) and not task.get("isValid"):
                continue
            suffix = "2" if "2" in task.get("taskType", "") else "1"
            cells = w.zone_cells(w.team + "TaskPoint" + suffix)
            path = self.path(w, actor, {p for c in cells for p in around(c)} - set(cells))
            if path is not None:
                options.append((max(path.length, task.get("coldDownRounds", 0)), path.length, path))
        if not options:
            return False
        path = min(options, key=lambda x: x[:2])[2]
        if path.length:
            return w.put(actor, command("move", [path.step]))
        w.reasons[actor["id"]] = "task_point_cooldown"
        return True

    def fallback(self, w, actor):
        if self.maintain(w, actor) or self.evade(w, actor):
            return
        if actor["id"] in self.duty_groups and (not w.daylight or self.must_return(w, actor)):
            if self.return_home(w, actor) or self.nearby_work(w, actor) or self.open_exit(w, actor):
                return
            path = self.home_path(w, actor)
            w.reasons[actor["id"]] = "guard_cooldown_or_no_target" if path and not path.length else "return_route_blocked"
            return
        if self.deliver_upgrade(w, actor) or self.shopping(w, actor):
            return
        if self.mine_or_sell(w, actor, False) or self.use_summon(w, actor):
            return
        if actor["roleType"] == "worker":
            if self.open_exit(w, actor):
                return
            if w.daylight and "stone" in actor.get("backpack", []) and self.build(w, actor, self.wall_jobs(w), "wall"):
                return
            if self.return_home(w, actor):
                return
        elif self.scout(w, actor) or self.stage_task(w, actor):
            return
        w.reasons[actor["id"]] = "no_reachable_useful_action"

    def record_utilization(self, w):
        for actor in w.people:
            uid = actor["id"]
            own_command = w.commands.get(str(uid))
            controls = any(c.get("controllerId") == str(uid) for c in w.commands.values())
            stationary_move = own_command and own_command["action"] == "move" and self.last_positions.get(uid) == pos(actor)
            productive = (own_command and not stationary_move) or controls or w.reasons.get(uid) == "task_result_pending"
            self.idle[uid] = 0 if productive else self.idle.get(uid, 0) + 1
            self.last_positions[uid] = pos(actor)
            if not own_command and not controls:
                reason = w.reasons.get(uid, "no_station")
                if self.idle[uid] == 1 or self.idle[uid] % self.config.idle_replan_rounds == 0:
                    LOG.info("round=%s role=%s waiting=%s rounds=%s", w.round, uid, reason, self.idle[uid])
            if self.idle[uid] >= self.config.idle_replan_rounds and stationary_move:
                target = own_command["targetPos"][0]
                self.bad_steps[(uid, (target["x"], target["y"]))] = w.round + 3
                LOG.info("round=%s role=%s blocked move; alternate route next turn", w.round, uid)

    def target_weight(self, w, robot):
        return 3 if w.base_distance(pos(robot)) <= 4 else 1

    def fire(self, w, actor, tower, projected):
        if (actor["id"] in w.used or str(tower["id"]) in w.commands
                or tower.get("cooldown", 0) > 0 or distance(pos(actor), pos(tower)) > 1):
            return False
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
                return True
        targets = [r for r in w.home_threats() if distance(pos(tower), pos(r)) <= reach and projected[r["id"]] > 0]
        if not targets:
            return False
        predicted = projected.copy()
        if kind == "rocket":
            shots = []
            for _ in range(level):
                # Include neighbouring cells to exploit splash without assuming base hit duplication.
                values = {}
                for robot in targets:
                    centre = pos(robot)
                    weight = self.target_weight(w, robot)
                    for point in [centre] + around(centre):
                        if w.inside(point) and distance(pos(tower), point) <= reach:
                            damage = 20 if point == centre else 10
                            values[point] = values.get(point, 0) + min(predicted[robot["id"]], damage) * weight
                best = max(sorted(values), key=values.get)
                shots.append(best)
                for robot in w.robots:
                    d = distance(best, pos(robot))
                    damage = 20 if d == 0 else (10 if d == 1 else 0)
                    predicted[robot["id"]] = max(0, predicted[robot["id"]] - damage)
        else:
            target = min(targets, key=lambda r: (w.base_distance(pos(r)), projected[r["id"]], r["id"]))
            # One repeated direction always satisfies Gatling's 90-degree cone.
            shots = [pos(target)] * (level if kind == "gatling" else 1)
            # Do not predict direct damage: line interception/penetration is engine-defined.
        if w.put(actor, command("attack", shots, controllerId=str(actor["id"])), key=tower["id"]):
            projected.update(predicted)
            return True
        return False
