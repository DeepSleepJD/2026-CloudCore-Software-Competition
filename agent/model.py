"""Wire model. Coordinates have their origin at the bottom left."""
from dataclasses import dataclass

WEAPONS = {"gatling", "railgun", "rocket"}
ACTORS = {"worker", "pioneer"}
ORES = {"stone", "iron", "copper"}


def pos(raw):
    return raw["x"], raw["y"]


def wire(p):
    return {"x": p[0], "y": p[1]}


def distance(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def neighbours(p):
    return [(p[0] + x, p[1] + y) for x in (-1, 0, 1)
            for y in (-1, 0, 1) if x or y]


@dataclass
class Unit:
    id: int
    kind: str
    p: tuple
    health: int
    level: int
    cooldown: int
    reach: int
    bag: list
    capacity: int
    target_team: str = ""

    @classmethod
    def load(cls, raw):
        kind = raw["roleType"]
        level = max(1, int(raw.get("level") or 1))
        fallback = (10, 15, 2147483647)[min(level, 3) - 1] if kind == "rocket" else 0
        return cls(int(raw["id"]), kind, pos(raw["pos"]), int(raw["health"]),
                   level, int(raw.get("cooldown") or 0),
                   int(raw.get("attackRange") or fallback),
                   list(raw.get("backpack") or []),
                   int(raw.get("backPackCapability") or (100 if kind == "worker" else 40)),
                   raw.get("targetTeam", ""))

    def cells(self):
        x, y = self.p
        return {(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)} if self.kind == "station" else {self.p}


class World:
    def __init__(self, request):
        self.raw = request
        self.round = int(request["roundNo"])
        self.day_tick = (self.round - 1) % 130
        self.day = self.day_tick < 70
        info, team = request["mapInfo"], request["teamOur"]
        self.width, self.height = int(info["width"]), int(info["height"])
        self.team = team["type"]
        self.gold = int(team.get("goldNum", 0))
        self.ours = [Unit.load(r) for r in team["roles"] if r["health"] > 0]
        self.enemy = [Unit.load(r) for r in request.get("teamEnemy", {}).get("roles", []) if r["health"] > 0]
        self.robots = [Unit.load(r) for r in request.get("robot", {}).get("roles", []) if r["health"] > 0]
        self.zones = {pos(z["pos"]): z["neutralType"] for z in info.get("zones", [])}
        self.actors = sorted((r for r in self.ours if r.kind in ACTORS), key=lambda r: r.id)
        self.workers = [r for r in self.actors if r.kind == "worker"]
        self.towers = [r for r in self.ours if r.kind in WEAPONS]
        self.walls = [r for r in self.ours if r.kind == "wall"]
        self.station = next((r for r in self.ours if r.kind == "station"), None)
        self.occupied = set().union(*(r.cells() for r in self.ours + self.enemy + self.robots))
        self.blocked = self.occupied | {p for p, k in self.zones.items() if k != "land"}
        self.prices = {r["name"]: r["price"] for r in request.get("vendorShopList", [])}
        self.shop = {r["name"]: r["price"] for r in request.get("weaponShopList", [])}

    def inside(self, p):
        return 0 <= p[0] < self.width and 0 <= p[1] < self.height

    def base_distance(self, p):
        return min(distance(p, c) for c in self.station.cells())


@dataclass
class Layout:
    towers: list
    walls: list
    operator: tuple

    @classmethod
    def for_world(cls, world):
        # Mirror the observed defender layout by 180 degrees around the 2x2 base.
        sx, sy = world.station.p
        left_base = sx < world.width / 2

        def transform(dx, dy):
            return (sx + 1 - dx, sy - 1 - dy) if left_base else (sx + dx, sy + dy)

        towers = [transform(2, -1), transform(1, 1), transform(2, 1)]
        front = [(-2, y) for y in (0, -1, 1, -2, 2, -3)]
        caps = [(x, y) for x in (-1, 0, 1) for y in (2, -3)]
        return cls([p for p in towers if world.inside(p)],
                   [transform(*p) for p in front + caps if world.inside(transform(*p))],
                   transform(2, 0))


def command(action, target=None, **kwargs):
    result = {"action": action, **kwargs}
    if target is not None:
        result["targetPos"] = [wire(target)]
    return result
