from collections import deque
from dataclasses import dataclass

TOWERS = {"rocket", "gatling", "railgun"}
PEOPLE = {"worker", "pioneer"}
STEPS = [(x, y) for x in (-1, 0, 1) for y in (-1, 0, 1) if x or y]


def pos(unit):
    value = unit["pos"]
    return (int(value["x"]), int(value["y"]))


def distance(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def around(p):
    return [(p[0] + dx, p[1] + dy) for dx, dy in STEPS]


def footprint(unit):
    x, y = pos(unit)
    return {(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)} if unit["roleType"] == "station" else {(x, y)}


def command(action, targets=None, **kwargs):
    result = {"action": action, **kwargs}
    if targets is not None:
        result["targetPos"] = [{"x": p[0], "y": p[1]} for p in targets]
    return result


@dataclass
class Path:
    step: tuple[int, int]
    length: int


class World:
    def __init__(self, data):
        self.data = data
        self.round = int(data["roundNo"])
        if self.round < 1:
            raise ValueError("roundNo must be 1-based, matching the supplied Demo")
        self.day = (self.round - 1) // 130 + 1
        self.tick = (self.round - 1) % 130
        self.daylight = self.tick < 70
        self.day_left = max(0, 70 - self.tick)
        self.width = int(data["mapInfo"]["width"])
        self.height = int(data["mapInfo"]["height"])
        if not (1 <= self.width <= 128 and 1 <= self.height <= 128):
            raise ValueError("unsupported map dimensions")
        self.team = data["teamOur"].get("type", "challenger")
        self.gold = int(data["teamOur"].get("goldNum", 0))
        self.ours = [u for u in data["teamOur"].get("roles", []) if u.get("health", 0) > 0]
        self.enemy = [u for u in data.get("teamEnemy", {}).get("roles", []) if u.get("health", 0) > 0]
        self.robots = [u for u in data.get("robot", {}).get("roles", []) if u.get("health", 0) > 0]
        self.zones = data["mapInfo"].get("zones", [])
        self.station = next((u for u in self.ours if u["roleType"] == "station"), None)
        self.enemy_station = next((u for u in self.enemy if u["roleType"] == "station"), None)
        self.workers = sorted([u for u in self.ours if u["roleType"] == "worker"], key=lambda u: u["id"])
        self.pioneer = next((u for u in self.ours if u["roleType"] == "pioneer"), None)
        self.towers = sorted([u for u in self.ours if u["roleType"] in TOWERS], key=lambda u: u["id"])
        self.walls = [u for u in self.ours if u["roleType"] == "wall"]
        self.people = self.workers + ([self.pioneer] if self.pioneer else [])
        self.blocked = set()
        for unit in self.ours + self.enemy + self.robots:
            self.blocked.update(footprint(unit))
        self.blocked.update(pos(z) for z in self.zones if z["neutralType"] != "land")
        self.prices = {s["name"]: int(s["price"]) for s in data.get("vendorShopList", [])}
        self.shop = {s["name"]: int(s["price"]) for s in data.get("weaponShopList", [])}
        self.tasks = data["teamOur"].get("playerTasks", [])
        self.reserved = set()
        self.commands = {}
        self.used = set()

    def inside(self, p):
        return 0 <= p[0] < self.width and 0 <= p[1] < self.height

    def base_distance(self, p, enemy=False):
        station = self.enemy_station if enemy else self.station
        return min(distance(p, q) for q in footprint(station)) if station else 999

    def home_threats(self):
        return [r for r in self.robots if r.get("targetTeam", self.team) == self.team]

    def path(self, actor, goals, extra_blocked=()):
        start = pos(actor)
        blocked = self.blocked | self.reserved | set(extra_blocked)
        blocked.discard(start)
        goals = {g for g in goals if self.inside(g) and g not in blocked}
        if not goals:
            return None
        queue = deque([(start, start, 0)])
        seen = {start}
        while queue:
            p, first, cost = queue.popleft()
            if p in goals:
                return Path(first, cost)
            for q in around(p):
                if q not in seen and q not in blocked and self.inside(q):
                    seen.add(q)
                    queue.append((q, q if cost == 0 else first, cost + 1))
        return None

    def adjacent_path(self, actor, cells):
        return self.path(actor, {p for c in cells for p in around(c)} - set(cells))

    def put(self, actor, value, key=None):
        uid = actor["id"]
        target_key = uid if key is None else key
        if uid in self.used or str(target_key) in self.commands:
            return False
        if value["action"] == "move":
            p = value["targetPos"][0]
            dest = (p["x"], p["y"])
            if dest in self.blocked or dest in self.reserved or distance(pos(actor), dest) != 1:
                return False
            self.reserved.add(dest)
        self.commands[str(target_key)] = value
        self.used.add(uid)
        return True

    def walk(self, actor, cells):
        path = self.adjacent_path(actor, cells)
        return bool(path and path.length and self.put(actor, command("move", [path.step])))

    def at(self, actor, cells):
        return any(distance(pos(actor), p) <= 1 for p in cells)

    def zone_cells(self, kind):
        return [pos(z) for z in self.zones if z["neutralType"] == kind]

    def healthy_base(self):
        return bool(self.station and self.station["health"] >= 0.7 * 1500 * self.station.get("level", 1))
