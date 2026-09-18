"""Defense investment policy. Deadlines are priorities, not guaranteed completion days."""


def upgrade_goal(world):
    station = world.station
    day = (world.round - 1) // 130 + 1
    rockets = sorted((r for r in world.towers if r.kind == "rocket"),
                     key=lambda r: (-r.level, r.id))
    main_level = rockets[0].level if rockets else 0
    base_due = (
        station.level == 1 and (day >= 3 or (day >= 2 and main_level >= 2)
                               or station.health < 1125)
        or station.level == 2 and (day >= 4 or (day >= 3 and main_level == 3)
                                  or station.health < 1950)
    )
    if base_due:
        return f"StationUpgradeVoucher{station.level}", station
    if rockets:
        target = rockets[0] if main_level < 3 else min(rockets, key=lambda r: (r.level, r.id))
        if target.level < 3:
            return f"WeaponUpgradeVoucher{target.level}", target
    if station.level < 3 and len(rockets) == 3:
        return f"StationUpgradeVoucher{station.level}", station
    return None


def repair_stock(world):
    day = (world.round - 1) // 130 + 1
    return 0 if day == 1 else 2 if day == 2 else 4 if day < 5 else 6


def max_health(unit):
    return 1500 * unit.level if unit.kind == "station" else 1000 + 500 * (unit.level - 1)
