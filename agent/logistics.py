"""Invest confirmed income in firepower, the exposed wall and emergency healing."""
from .model import Layout


def upgrade_goal(world, levels=None):
    # Virtual levels plan shopping only. Commands use authoritative live levels.
    levels = levels or {}
    level = lambda r: levels.get(r.id, r.level)
    station = world.station
    rockets = sorted((r for r in world.towers if r.kind == "rocket"),
                     key=lambda r: (-level(r), r.id))
    front = set(Layout.for_world(world).walls[:6])
    walls = sorted((r for r in world.walls if r.p in front),
                   key=lambda r: (level(r), r.health / max_health(r), r.id))

    def voucher(target):
        prefix = {"station": "Station", "rocket": "Weapon", "wall": "Wall"}[target.kind]
        return f"{prefix}UpgradeVoucher{level(target)}", target

    # A base upgrade restores health; the calendar alone never justifies it.
    if (level(station) == station.level < 3
            and station.health <= 0.35 * max_health(station)):
        return voucher(station)
    if len(rockets) < 3:
        return None
    critical = [r for r in walls if level(r) == r.level < 3
                and r.health < 0.35 * max_health(r)
                and (world.day or any(max(abs(r.p[0] - b.p[0]), abs(r.p[1] - b.p[1])) <= 4
                                      for b in world.robots))]
    if critical:
        return voucher(critical[0])
    if level(rockets[0]) < 3:
        return voucher(rockets[0])
    low = [r for r in rockets if level(r) < 2]
    if low:
        return voucher(low[0])
    # Interleave two front sections with each remaining level-3 launcher.
    full = sum(level(r) == 3 for r in rockets)
    reinforced = sum(level(r) >= 2 for r in walls)
    if reinforced < min(len(walls), 2 * full):
        return voucher(next(r for r in walls if level(r) == 1))
    low = [r for r in rockets if level(r) < 3]
    if low:
        return voucher(low[0])
    if (level(station) == station.level < 3
            and station.health < 0.7 * max_health(station)):
        return voucher(station)
    low = [r for r in walls if level(r) < 3]
    if low:
        return voucher(low[0])
    if level(station) < 3:
        return voucher(station)
    return None


def shopping_goal(world, bag):
    """Account for tickets in this courier's bag before buying the next one."""
    levels, remaining = {}, list(bag)
    for _ in range(24):
        goal = upgrade_goal(world, levels)
        if not goal or goal[0] not in remaining:
            return goal, levels
        name, target = goal
        remaining.remove(name)
        levels[target.id] = levels.get(target.id, target.level) + 1
    return None, levels


def purchase_quantity(world, name, bag):
    """Batch consecutive priorities of the same type, never speculative stock."""
    inventory, count = list(bag), 0
    for _ in range(6):
        goal, _ = shopping_goal(world, inventory)
        if not goal or goal[0] != name:
            break
        inventory.append(name)
        count += 1
    return count


def repair_reserve(world):
    # Existing supplies count; anticipated task rewards never count as money.
    stock = sum(r.bag.count("WallFixer") for r in world.actors)
    return max(0, min(2, repair_stock(world)) - stock) * world.shop.get("WallFixer", 10)


def repair_stock(world):
    day = (world.round - 1) // 130 + 1
    return 0 if day == 1 else 2 if day == 2 else 4 if day < 5 else 6


def max_health(unit):
    return 1500 * unit.level if unit.kind == "station" else 1000 + 500 * (unit.level - 1)
