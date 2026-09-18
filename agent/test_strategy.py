"""Decision-level tests: wall voucher priority and preventive front upgrades."""
import unittest

from agent.model import Layout, World
from agent.strategy import Planner

FRONT = [(8, 10), (8, 9), (8, 11), (8, 8), (8, 12), (8, 7)]
CAPS = [(9, 12), (10, 12), (11, 12), (9, 7), (10, 7), (11, 7)]


def role(id, kind, x, y, health=1000, level=1, bag=()):
    return {"id": id, "roleType": kind, "pos": {"x": x, "y": y}, "health": health,
            "level": level, "backpack": list(bag), "backPackCapability": 100}


def request(roles=(), zones=(), shop=()):
    return {"roundNo": 10,
            "mapInfo": {"width": 20, "height": 20, "zones": list(zones)},
            "teamOur": {"type": "teamA", "goldNum": 500, "roles": list(roles)},
            "vendorShopList": [], "weaponShopList": list(shop)}


class LayoutFrontTest(unittest.TestCase):
    def test_front_is_six_walls_without_caps(self):
        for station_pos in ((10, 10), (5, 10)):
            world = World(request(roles=[role(100, "station", *station_pos)]))
            layout = Layout.for_world(world)
            self.assertEqual(len(layout.front), 6)
            self.assertEqual(len(layout.walls), 12)
            self.assertTrue(set(layout.front) <= set(layout.walls))
            self.assertEqual(len({x for x, _ in layout.front}), 1)


class VoucherPriorityTest(unittest.TestCase):
    def first_use_target(self, walls, bag):
        roles = [role(100, "station", 10, 10, health=3000),
                 role(1, "worker", 10, 11, health=200, bag=bag)]
        roles += [role(200 + i, "wall", *p, health=h, level=l)
                  for i, (p, h, l) in enumerate(walls)]
        planner = Planner(World(request(roles=roles)), {})
        seen = []
        planner.approach = lambda a, target, action=None, **kw: seen.append(target) or True
        planner.use_supplies(planner.w.actors[0])
        return seen[0] if seen else None

    def test_damaged_front_outranks_cap_and_healthy_front(self):
        walls = [((8, 10), 400, 2), ((8, 9), 1000, 1), ((9, 12), 300, 2)]
        bag = ["WallUpgradeVoucher1", "WallUpgradeVoucher2"]
        self.assertEqual(self.first_use_target(walls, bag), (8, 10))

    def test_critical_fixer_outranks_all_upgrades(self):
        walls = [((8, 10), 400, 2), ((8, 9), 1000, 1), ((9, 12), 300, 2)]
        bag = ["WallFixer", "WallUpgradeVoucher2"]
        self.assertEqual(self.first_use_target(walls, bag), (9, 12))

    def test_cap_waits_until_front_walls_maxed(self):
        walls = [((8, 10), 1500, 3), ((8, 9), 1500, 3), ((9, 12), 300, 2)]
        self.assertEqual(self.first_use_target(walls, ["WallUpgradeVoucher2"]), (9, 12))


class ProactiveUpgradeTest(unittest.TestCase):
    def full_defence(self, station_health, shop):
        roles = [role(100, "station", 10, 10, health=station_health),
                 role(1, "worker", 14, 15, health=200),
                 role(300, "rocket", 12, 9, health=500),
                 role(301, "rocket", 11, 11, health=500),
                 role(302, "rocket", 12, 11, health=500)]
        roles += [role(400 + i, "wall", *p, health=1000) for i, p in enumerate(FRONT + CAPS)]
        zones = [{"pos": {"x": 15, "y": 15}, "neutralType": "weaponShop"}]
        return Planner(World(request(roles=roles, zones=zones, shop=shop)), {})

    def test_spare_gold_buys_front_voucher(self):
        planner = self.full_defence(3000, [{"name": "WallUpgradeVoucher1", "price": 60}])
        planner.worker(planner.w.workers[0])
        command = planner.commands.get("1")
        self.assertEqual(command["action"], "buy")
        self.assertEqual(command["name"], "WallUpgradeVoucher1")

    def test_damaged_base_freezes_wall_upgrades(self):
        planner = self.full_defence(700, [{"name": "WallUpgradeVoucher1", "price": 60}])
        planner.worker(planner.w.workers[0])
        self.assertEqual(planner.commands, {})


if __name__ == "__main__":
    unittest.main()
