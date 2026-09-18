"""Decision-level tests: wall voucher priority and preventive front upgrades."""
import unittest

from agent.model import Layout, World
from agent.strategy import Agent, Planner, TaskSolver
from agent.combat import rocket_wall_targets

FRONT = [(8, 10), (8, 9), (8, 11), (8, 8), (8, 12), (8, 7)]
CAPS = [(9, 12), (10, 12), (11, 12), (9, 7), (10, 7), (11, 7)]


def role(id, kind, x, y, health=1000, level=1, bag=()):
    return {"id": id, "roleType": kind, "pos": {"x": x, "y": y}, "health": health,
            "level": level, "backpack": list(bag), "backPackCapability": 100}


def request(roles=(), zones=(), shop=(), enemy=(), robots=(), tasks=(), round_no=10,
            phase_task="", llm_resp="", last_cmd_result=""):
    return {"roundNo": round_no,
            "mapInfo": {"width": 20, "height": 20, "zones": list(zones)},
            "teamOur": {"type": "teamA", "goldNum": 500, "roles": list(roles),
                        "playerTasks": list(tasks)},
            "teamEnemy": {"roles": list(enemy)},
            "robot": {"roles": list(robots)},
            "phaseTask": phase_task, "llmResp": llm_resp, "lastCmdResult": last_cmd_result,
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


class WallPositionPriorityTest(unittest.TestCase):
    """Req 1: the corridor-centre front wall always wins, health only breaks ties."""

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

    def test_pristine_middle_outranks_damaged_outer_front(self):
        walls = [((8, 10), 1000, 1), ((8, 9), 100, 1)]
        self.assertEqual(self.first_use_target(walls, ["WallUpgradeVoucher1"]), (8, 10))


class WallPurchasePreferenceTest(unittest.TestCase):
    """Req 1: repair-driven and proactive voucher purchases also go middle-first."""

    def defence(self, front_levels, front_healths, shop):
        levels = {p: 1 for p in FRONT + CAPS}
        healths = {p: 1000 for p in FRONT + CAPS}
        for p, lvl in zip(FRONT, front_levels):
            levels[p] = lvl
        for p, hp in zip(FRONT, front_healths):
            healths[p] = hp
        roles = [role(100, "station", 10, 10, health=3000),
                 role(1, "worker", 14, 15, health=200),
                 role(300, "rocket", 12, 9, health=500),
                 role(301, "rocket", 11, 11, health=500),
                 role(302, "rocket", 12, 11, health=500)]
        roles += [role(400 + i, "wall", *p, health=healths[p], level=levels[p])
                  for i, p in enumerate(FRONT + CAPS)]
        zones = [{"pos": {"x": 15, "y": 15}, "neutralType": "weaponShop"}]
        return Planner(World(request(roles=roles, zones=zones, shop=shop)), {})

    def bought(self, planner):
        planner.worker(planner.w.workers[0])
        command = planner.commands.get("1")
        self.assertEqual(command["action"], "buy")
        return command["name"]

    def test_damaged_purchase_prefers_middle(self):
        # Centre wall lvl2 slightly damaged vs outer wall lvl1 nearly dead.
        planner = self.defence([2, 1, 1, 1, 1, 1], [1000, 100, 1000, 1000, 1000, 1000],
                               [{"name": "WallUpgradeVoucher2", "price": 60}])
        self.assertEqual(self.bought(planner), "WallUpgradeVoucher2")

    def test_proactive_prefers_middle(self):
        # All front walls healthy; centre is already lvl2 so it finishes first.
        planner = self.defence([2, 1, 1, 1, 1, 1], [1900] * 6,
                               [{"name": "WallUpgradeVoucher2", "price": 60}])
        self.assertEqual(self.bought(planner), "WallUpgradeVoucher2")


class NightWallTargetTest(unittest.TestCase):
    """Req 2: strict handover — no robot targeting us means bombard enemy walls."""

    def night(self, robots=(), enemy=(), pioneers=(5,)):
        roles = [role(100, "station", 10, 10, health=3000),
                 role(300, "rocket", 12, 9, health=500),
                 role(301, "rocket", 11, 11, health=500),
                 role(302, "rocket", 12, 11, health=500)]
        for pid in pioneers:
            x, y = {5: (12, 10), 6: (13, 10)}[pid]
            roles.append(role(pid, "pioneer", x, y))
        return Planner(World(request(roles=roles, enemy=enemy, robots=robots, round_no=71)), {})

    def test_walls_when_no_robots(self):
        planner = self.night(enemy=[role(900, "wall", 14, 10, health=1200)])
        planner.run()
        command = planner.commands.get("300")
        self.assertEqual(command["action"], "attack")
        self.assertEqual(command["controllerId"], "5")
        self.assertEqual(command["targetPos"], [{"x": 14, "y": 10}])

    def test_neutral_robots_do_not_block_walls(self):
        planner = self.night(enemy=[role(900, "wall", 14, 10, health=1200)],
                             robots=[role(901, "smallRobot", 15, 10)])
        planner.run()
        self.assertEqual(planner.commands.get("300")["targetPos"], [{"x": 14, "y": 10}])

    def test_targeted_robots_win(self):
        robot = role(901, "smallRobot", 15, 10)
        robot["targetTeam"] = "teamA"
        planner = self.night(enemy=[role(900, "wall", 14, 10, health=1200)], robots=[robot])
        planner.run()
        command = planner.commands.get("300")
        self.assertEqual(command["action"], "attack")
        hit = command["targetPos"][0]
        self.assertLessEqual(max(abs(hit["x"] - 15), abs(hit["y"] - 10)), 1)

    def test_two_pioneers_fire_distinct_towers(self):
        planner = self.night(enemy=[role(900, "wall", 14, 10, health=1200)], pioneers=(5, 6))
        planner.run()
        fired = [key for key, cmd in planner.commands.items()
                 if cmd.get("action") == "attack"]
        self.assertEqual(len(fired), 2)
        self.assertEqual(len(set(fired)), 2)

    def test_lowest_hp_order_and_padding(self):
        enemy = [role(900, "wall", 0, 0, health=1800, level=2),
                 role(901, "wall", 2, 2, health=1200, level=1),
                 role(902, "wall", 14, 10, health=100, level=1)]
        roles = [role(100, "station", 10, 10, health=3000),
                 role(300, "rocket", 12, 9, health=500, level=3)]
        world = World(request(roles=roles, enemy=enemy))
        targets, _ = rocket_wall_targets(world, world.towers[0])
        self.assertEqual(targets, [(14, 10), (2, 2), (0, 0)])
        roles[1]["level"] = 1
        world = World(request(roles=roles, enemy=enemy))
        targets, _ = rocket_wall_targets(world, world.towers[0])
        self.assertEqual(targets, [(14, 10)])
        roles[1]["level"] = 3
        world = World(request(roles=roles, enemy=[enemy[1]]))
        targets, _ = rocket_wall_targets(world, world.towers[0])
        self.assertEqual(targets, [(2, 2), (2, 2), (2, 2)])


class TaskFlowTest(unittest.TestCase):
    """Req 3: pioneer daytime self-evolution tasks end to end."""

    TASK = "任务:查询城市天气,把气温填入答案"

    def roles(self, px=12, py=12):
        return [role(100, "station", 10, 10, health=3000),
                role(1, "worker", 16, 16, health=200),
                role(5, "pioneer", px, py)]

    def task(self):
        return {"taskType": "自进化类1", "taskPosition": {"x": 14, "y": 14},
                "coldDownRounds": 0, "scoreReward": 50, "goldReward": 30, "isValid": True}

    def decide(self, agent, roles, **kw):
        return agent.decide(request(roles=roles, tasks=[self.task()], **kw))

    def test_walk_then_accept(self):
        agent = Agent()
        move = self.decide(agent, self.roles(12, 12))["roleCommandMap"]["5"]
        self.assertEqual(move["action"], "move")
        accept = self.decide(agent, self.roles(14, 13), round_no=11)["roleCommandMap"]["5"]
        self.assertEqual(accept["action"], "acceptTask")

    def test_active_holds_and_explores(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        response = self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        self.assertNotIn("5", response["roleCommandMap"])
        self.assertEqual(response["executeCmd"], TaskSolver.LADDER[0])
        self.assertIn(self.TASK, response["prompt"])

    def test_llm_cmd_executed_then_answer_submitted(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        response = self.decide(agent, self.roles(14, 13), round_no=12, phase_task=self.TASK,
                               llm_resp="CMD: cat api.md",
                               last_cmd_result="[exitCode:0]\nls output")
        self.assertEqual(response["executeCmd"], "cat api.md")
        response = self.decide(agent, self.roles(14, 13), round_no=13, phase_task=self.TASK,
                               llm_resp="ANSWER: 25C",
                               last_cmd_result="[exitCode:0]\n25")
        self.assertEqual(response["roleCommandMap"]["5"],
                         {"action": "submitAnswer", "taskAnswer": "25C"})

    def test_sop_recipe_replay_skips_exploration(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        self.decide(agent, self.roles(14, 13), round_no=12, phase_task=self.TASK,
                    llm_resp="CMD: cat api.md", last_cmd_result="[exitCode:0]\nls output")
        self.decide(agent, self.roles(14, 13), round_no=13, phase_task=self.TASK,
                    llm_resp="ANSWER: 25C",
                    last_cmd_result="[exitCode:0]\napi doc: http://localhost:8899/api/v1/x")
        self.decide(agent, self.roles(14, 13), round_no=14)
        self.assertIn("自进化类1", agent.sop)
        self.assertIn("URL: http://localhost:8899/api/v1/x", agent.sop["自进化类1"]["facts"])
        response = self.decide(agent, self.roles(14, 13), round_no=15,
                               phase_task="任务:查询上海天气,把气温填入答案")
        self.assertEqual(response["executeCmd"], TaskSolver.LADDER[0])
        self.assertIn("历史经验", response["prompt"])
        self.assertIn("http://localhost:8899/api/v1/x", response["prompt"])

    def test_multiline_cmd_preserved(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        heredoc = "CMD: cat > f.conf << 'EOF'\nport 8080\nname app\nEOF"
        response = self.decide(agent, self.roles(14, 13), round_no=12, phase_task=self.TASK,
                               llm_resp=heredoc)
        self.assertEqual(response["executeCmd"],
                         "cat > f.conf << 'EOF'\nport 8080\nname app\nEOF")

    def test_placeholder_answer_not_submitted(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        response = self.decide(agent, self.roles(14, 13), round_no=12, phase_task=self.TASK,
                               llm_resp="ANSWER: 未知")
        self.assertNotIn("5", response["roleCommandMap"])
        self.assertEqual(response["executeCmd"], TaskSolver.LADDER[0])

    def test_budget_exhausted_without_answer_never_submits_garbage(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        for round_no in range(11, 26):
            response = self.decide(agent, self.roles(14, 13), round_no=round_no,
                                   phase_task=self.TASK)
            command = response["roleCommandMap"].get("5")
            self.assertTrue(command is None or command.get("action") != "submitAnswer",
                            f"R{round_no} 提交了垃圾答案: {command}")

    def test_time_guard_rejects_late_task(self):
        agent = Agent()
        response = self.decide(agent, self.roles(12, 12), round_no=63)
        command = response["roleCommandMap"]["5"]
        self.assertNotEqual(command["action"], "acceptTask")

    def test_dusk_walk_outranks_task(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        response = self.decide(agent, self.roles(14, 13), round_no=66, phase_task=self.TASK)
        self.assertEqual(response["roleCommandMap"]["5"]["action"], "move")

    def test_pioneer_death_resets_task(self):
        agent = Agent()
        self.decide(agent, self.roles(14, 13))
        self.decide(agent, self.roles(14, 13), round_no=11, phase_task=self.TASK)
        self.decide(agent, [role(100, "station", 10, 10, health=3000),
                            role(1, "worker", 16, 16)], round_no=12, phase_task=self.TASK)
        accept = self.decide(agent, self.roles(14, 13), round_no=13)["roleCommandMap"]["5"]
        self.assertEqual(accept["action"], "acceptTask")

    def test_malformed_inputs_stay_three_keys(self):
        agent = Agent()
        bad_tasks = [{"taskType": "自进化类1"}]
        response = agent.decide(request(roles=self.roles(), tasks=bad_tasks, llm_resp=None))
        self.assertEqual(set(response), {"roleCommandMap", "prompt", "executeCmd"})


if __name__ == "__main__":
    unittest.main()
