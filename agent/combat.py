"""Rocket splash scoring, using pre-movement robot positions."""
from .model import distance, neighbours


def rocket_targets(world, tower):
    robots = world.threats
    if not robots:
        return [], 0
    candidates = sorted({p for r in robots for p in [r.p] + neighbours(r.p)
                         if world.inside(p) and distance(p, tower.p) <= tower.reach})
    if not candidates:
        return [], 0
    health = [r.health for r in robots]
    weights = [20 + r.attack if world.base_distance(r.p) <= 3 else
               (3 + r.attack / 10 if any(w.health < 650 and distance(w.p, r.p) <= 3
                                        for w in world.walls) else
                1 + 3 / max(1, world.base_distance(r.p) - 2)) for r in robots]
    hits = {p: [(i, 20 if p == r.p else 10) for i, r in enumerate(robots)
                if distance(p, r.p) <= 1] for p in candidates}
    selected, total = [], 0.0
    for _ in range(min(3, max(1, tower.level))):
        def score(p):
            return sum((min(health[i], damage) + (8 if 0 < health[i] <= damage else 0))
                       * weights[i] for i, damage in hits[p])
        target = max(candidates, key=lambda p: (score(p), -world.base_distance(p), p))
        value = score(target)
        if value <= 0:
            # The wire contract requires exactly level target entries.
            if selected:
                selected.append(selected[-1])
                continue
            return [], 0
        selected.append(target)
        total += value
        for i, damage in hits[target]:
            health[i] = max(0, health[i] - damage)
    return selected, total
