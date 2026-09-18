"""Daytime construction/economy and one-operator nighttime defence."""
import re
import traceback

from .model import World, Layout, ORES, command, distance, neighbours, wire
from .navigation import paths
from .combat import rocket_targets, rocket_wall_targets
from .tasklog import rlog


class Planner:
    def __init__(self, world, avoided, task=None):
        self.w = world
        self.layout = Layout.for_world(world)
        self.front = set(self.layout.front)
        self.avoided = avoided
        self.task = task if task is not None else {"state": "idle"}
        self.commands = {}
        self.reserved = set()
        self.sites = set()
        self.fired = set()
        self.budget = world.gold
        self.build_count = len(world.towers)
        self.operator = min(world.actors, key=lambda r: (r.kind != "pioneer", distance(r.p, self.layout.operator), r.id)) if world.actors else None
        self.wall_missing = [p for p in self.layout.walls if not any(r.p == p for r in world.walls)]
        self.danger = {p for r in world.robots for x in range(-4, 5) for y in range(-4, 5)
                       for p in [(r.p[0] + x, r.p[1] + y)] if world.inside(p)}

    def emit(self, actor, cmd):
        self.commands[str(actor.id)] = cmd
        if cmd["action"] in ("move", "build"):
            p = cmd["targetPos"][0]
            self.reserved.add((p["x"], p["y"]))
        return True

    def route(self, actor, avoid_danger=True):
        forbidden = set(self.layout.towers)
        if actor != self.operator:
            forbidden.add(self.layout.operator)
        if avoid_danger:
            forbidden |= self.danger
        return paths(self.w, actor, self.reserved, forbidden)

    def approach(self, actor, target, action=None, exact=False, safe=True):
        distances, first = self.route(actor, safe)
        goals = [target] if exact else neighbours(target)
        goals = [p for p in goals if p in distances and (not safe or p not in self.danger)]
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
        if not force and not adjacent and sum(amounts.values()) < 12 and len(actor.bag) < actor.capacity:
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
                    # Corridor-centre first: position rank dominates urgency (spread < 10); caps rank last.
                    rank = self.layout.front.index(unit.p) if unit.p in self.front else len(self.layout.front)
                    priority = rank * 10 + (0.5 if unit.health < 350 else 2) + unit.health / (1000 + 500 * (unit.level - 1))
                options.append((priority, distance(actor.p, unit.p), unit.p, name))
            max_health = 1000 + 500 * (unit.level - 1)
            if unit.kind == "wall" and unit.health < max_health * 0.65 and "WallFixer" in actor.bag:
                options.append((0 if unit.health < 350 else 3, distance(actor.p, unit.p), unit.p, "WallFixer"))
        for _, _, target, name in sorted(options):
            if self.approach(actor, target, command("use", target, name=name)):
                self.sites.add(target)
                return True
        return False

    def purchase(self, actor, name, reserve=0):
        if name not in self.w.shop or len(actor.bag) >= actor.capacity:
            return False
        price = self.w.shop[name]
        if self.budget - reserve < price:
            return False
        # Do not send a second courier for an item already being delivered.
        if any(name in r.bag for r in self.w.actors):
            return False
        shops = sorted((p for p, k in self.w.zones.items() if k == "weaponShop"), key=lambda p: distance(actor.p, p))
        for target in shops:
            if actor == self.operator:
                distances, _ = self.route(actor)
                travel = min((distances[q] for q in neighbours(target) if q in distances), default=10**6)
                # A conservative return allowance keeps the gunner home at dusk.
                if not self.w.day or travel + distance(target, self.layout.operator) + 8 >= 70 - self.w.day_tick:
                    continue
            if self.approach(actor, target, command("buy", name=name, num=1)):
                if self.commands[str(actor.id)]["action"] == "buy":
                    self.budget -= price
                return True
        return False

    def upgrade_order(self):
        rockets = sorted((r for r in self.w.towers if r.kind == "rocket"), key=lambda r: (-r.level, r.id))
        station = self.w.station
        if station.level < 3 and station.health < 1500 * station.level * 0.6:
            return f"StationUpgradeVoucher{station.level}"
        if rockets:
            main = rockets[0]
            if main.level < 3:
                return f"WeaponUpgradeVoucher{main.level}"
            other = min(rockets, key=lambda r: (r.level, r.id))
            if other.level < 3:
                return f"WeaponUpgradeVoucher{other.level}"
        if station.level < 3:
            return f"StationUpgradeVoucher{station.level}"
        return None

    def operate(self, actor):
        if not self.w.day:
            choices = []
            for tower in self.w.towers:
                if tower.kind != "rocket" or tower.cooldown > 0 or distance(actor.p, tower.p) != 1:
                    continue
                if tower.id in self.fired:
                    continue
                # Strict handover: once no robot targets us, bombard the enemy walls
                # (lowest HP first); robots targeting the other side never block this.
                if any(r.target_team == self.w.team for r in self.w.robots):
                    targets, score = rocket_targets(self.w, tower)
                else:
                    targets, score = rocket_wall_targets(self.w, tower)
                if targets:
                    choices.append((score, -tower.id, tower, targets))
            if choices:
                _, _, tower, targets = max(choices, key=lambda c: c[:2])
                self.fired.add(tower.id)
                self.commands[str(tower.id)] = {"action": "attack", "controllerId": str(actor.id),
                                                 "targetPos": [wire(p) for p in targets]}
                return True
        return self.approach(actor, self.layout.operator, exact=True, safe=False)

    def task_day(self, actor):
        """Idle -> go -> accepted transitions; the accepted phase is TaskSolver's."""
        task = self.task
        state = task.get("state", "idle")
        if state == "go":
            point = task.get("point")
            if self.w.phase_task:
                task["state"] = "active"
                task.setdefault("accepted_round", self.w.round)
                rlog(self.w.round, f"go→active: 已接受任务 类型={task.get('type')} "
                                   f"任务点={point} 任务文本前200字: {self.w.phase_task[:200]!r}")
                return False
            info = next((t for t in self.w.tasks if t["p"] == point), None)
            if point is None or info is None or not (info["valid"] and info["cooldown"] == 0):
                rlog(self.w.round, f"go→idle: 任务点失效(点={point} 信息={info!r}),任务放弃")
                task["state"] = "idle"
                return False
            if task.get("accepts", 0) >= 3:
                rlog(self.w.round, "go→idle: acceptTask已尝试3次仍未见phaseTask,放弃本轮任务")
                task["state"] = "idle"
                return False
            if self.approach(actor, point, command("acceptTask")):
                cmd = self.commands.get(str(actor.id)) or {}
                if cmd.get("action") == "acceptTask":
                    task["accepts"] = task.get("accepts", 0) + 1
                    rlog(self.w.round, f"go: 发送acceptTask(第{task['accepts']}次), "
                                       f"开拓者距任务点{distance(actor.p, point)}")
                else:
                    rlog(self.w.round, f"go: 向任务点{point}移动中,距离={distance(actor.p, point)}")
                return True
            task["go_fails"] = task.get("go_fails", 0) + 1
            rlog(self.w.round, f"go: 无法接近任务点{point}(第{task['go_fails']}次),"
                               f"可能被阻挡/禁行")
            if task["go_fails"] > 5:
                rlog(self.w.round, "go→idle: 连续寻路失败,放弃本轮任务")
                task["state"] = "idle"
            return False
        if state != "idle" or self.w.phase_task:
            return False
        ready = [t for t in self.w.tasks if t["valid"] and t["cooldown"] == 0]
        if not ready:
            return False
        target = min(ready, key=lambda t: (distance(actor.p, t["p"]), t["type"]))
        budget = 6 if target["type"] in task.get("known", []) else 20
        remaining = 70 - self.w.day_tick
        d_go = distance(actor.p, target["p"])
        d_back = distance(target["p"], self.layout.operator)
        if remaining <= d_go + d_back + budget:
            rlog(self.w.round, f"idle: 有可接任务{target['p']}但时间Guard拒绝: "
                               f"剩余白天={remaining} < 去{d_go}+回{d_back}+预算{budget}")
            return False
        # Keep retry counters when re-picking the same point across idle resets.
        task.update({"state": "go", "type": target["type"], "point": target["p"],
                     "timeout": target["timeout"],
                     "accepts": task.get("accepts", 0) if task.get("point") == target["p"] else 0,
                     "go_fails": task.get("go_fails", 0) if task.get("point") == target["p"] else 0})
        rlog(self.w.round, f"idle→go: 选定任务点{target['p']} 类型={target['type']} "
                           f"超时={target['timeout']} 剩余白天={remaining} "
                           f"去{d_go}+回{d_back}+预算{budget},开拓者位置={actor.p}")
        return self.task_day(actor)

    def retreat(self, actor):
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
        if self.retreat(actor) or self.use_supplies(actor):
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
        # One worker handles repairs; the other continues funding the defence.
        if self.w.workers and actor.id == self.w.workers[0].id:
            if self.w.station.level < 3 and self.w.station.health < 800:
                if self.purchase(actor, f"StationUpgradeVoucher{self.w.station.level}", reserve=reserve):
                    return
            damaged = sorted((r for r in self.w.walls if r.health < (1000 + 500 * (r.level - 1)) * 0.6),
                             key=lambda r: (self.layout.front.index(r.p) if r.p in self.front else len(self.layout.front),
                                            r.health, r.id))
            if damaged:
                wall = damaged[0]
                # Upgrading both repairs and strengthens the exposed section.
                if wall.level < 3 and self.purchase(actor, f"WallUpgradeVoucher{wall.level}", reserve=reserve):
                    return
                if self.purchase(actor, "WallFixer", reserve=reserve):
                    return
            # Preventive upgrades: once rockets are paid for and the base is safe,
            # spend spare gold pushing the exposed front walls toward level 3.
            ready = [r for r in self.w.walls if r.p in self.front and r.level < 3]
            if self.build_count >= 3 and (self.w.station.level >= 3 or self.w.station.health >= 800) and ready:
                wall = min(ready, key=lambda r: (self.layout.front.index(r.p), r.level, r.health))
                if self.purchase(actor, f"WallUpgradeVoucher{wall.level}", reserve=reserve):
                    return
        if not self.mine(actor):
            self.sell(actor, True)

    def run(self):
        # Night control is assigned first so a fallback worker never also mines/moves.
        if not self.w.day and self.operator:
            self.operate(self.operator)
        for worker in self.w.workers:
            if not self.w.day and worker == self.operator:
                continue
            self.worker(worker)
        for actor in self.w.actors:
            if actor.kind != "pioneer":
                continue
            if not self.w.day:
                # The operator already fired via the run() prologue; extra pioneers
                # man another ready tower only when they have nothing to heal/use.
                if actor != self.operator and not self.use_supplies(actor):
                    self.operate(actor)
                continue
            return_margin = distance(actor.p, self.layout.operator) + 8
            if 70 - self.w.day_tick <= return_margin:
                if self.task.get("state") in ("go", "active"):
                    rlog(self.w.round, f"黄昏Guard触发(剩{70 - self.w.day_tick}回合,"
                                       f"margin={return_margin}): 弃任务返岗,离开任务点自动结束任务")
                self.operate(actor)
                continue
            if self.task_day(actor):
                continue
            if self.w.phase_task and not self.task.get("abandoned"):
                continue
            if self.use_supplies(actor):
                continue
            upgrade = self.upgrade_order()
            if upgrade and self.purchase(actor, upgrade, reserve=25 * max(0, 3 - self.build_count)):
                continue
            self.operate(actor)
        return {"roleCommandMap": self.commands, "prompt": "", "executeCmd": ""}


class TaskSolver:
    """One accepted self-evolution task: sandbox exploration plus platform-LLM round-trips.

    Drives only the top-level `prompt`/`executeCmd` fields and the pioneer's
    submitAnswer; role movement stays with Planner. All state lives in the
    Agent-owned task dict so nothing survives a World rebuild unintentionally.
    """
    LADDER = ("pwd && ls -la && find . -maxdepth 3 -type f 2>/dev/null | head -50",
              "find / -maxdepth 2 2>/dev/null | head -40")

    def __init__(self, sop, task):
        self.sop = sop
        self.task = task

    def step(self, world, response):
        task = self.task
        if task.get("abandoned"):
            rlog(world.round, "active: 任务已标记弃坑,等待开拓者走离任务点")
            return response
        if not world.phase_task:
            if task.get("state") == "active":
                self._record(world.round)
            else:
                rlog(world.round, f"phaseTask为空且状态={task.get('state')},无活动任务")
            # Keep type/known so the next task of the same kind can replay the recipe.
            task.update({"state": "idle", "outputs": [], "cmd": None, "stuck": 0,
                         "submits": 0, "guess": "", "abandoned": False})
            task.pop("accepted_round", None)
            task.pop("submitted_round", None)
            return response
        if task.get("state") != "active":
            task.update({"state": "active", "phase": world.phase_task,
                         "accepted_round": task.get("accepted_round", world.round),
                         "outputs": [], "cmd": None, "stuck": 0, "submits": 0})
            rlog(world.round, f"任务激活: 类型={task.get('type')} 接取回合={task['accepted_round']} "
                              f"超时={task.get('timeout')} 全文:\n---------- phaseTask ----------\n"
                              f"{world.phase_task}\n------------------------------")
        pioneer = next((r for r in world.actors if r.kind == "pioneer"), None)
        if pioneer is None:
            rlog(world.round, "开拓者不存在(可能阵亡),任务记忆清空")
            task.clear()
            task["state"] = "idle"
            return response
        if str(pioneer.id) in response["roleCommandMap"]:
            action = response["roleCommandMap"][str(pioneer.id)].get("action")
            rlog(world.round, f"active: 开拓者本回合已被Planner调度(action={action}),"
                              f"任务动作让位(黄昏返岗/撤退)")
            return response
        if task.get("submitted_round") == world.round - 1:
            rlog(world.round, f"active: 上回合已提交答案 {task.get('guess', '')[:80]!r},等待平台判定")
            return response
        spent = world.round - task["accepted_round"]
        budget = 0.6 * task.get("timeout") if task.get("timeout") else 35
        parsed = self._parse(world.llm_resp)
        rlog(world.round, f"active: 耗时={spent} 预算={budget:.0f} 提交次数={task.get('submits', 0)} "
                          f"stuck={task.get('stuck', 0)} llmResp前300字: {world.llm_resp[:300]!r} "
                          f"lastCmd退出码={world.last_cmd.get('code')!r} "
                          f"输出前200字: {world.last_cmd.get('out', '')[:200]!r}")
        if spent > budget:
            guess = task.get("guess") or (parsed[1] if parsed and parsed[0] == "ANSWER" else "")
            rlog(world.round, f"超预算({spent}>{budget:.0f}),强制提交最佳猜测: {guess[:100]!r}")
            return self._submit(world, response, pioneer, guess or world.phase_task)
        if parsed and parsed[0] == "ANSWER" and (parsed[1] != task.get("guess") or not task.get("submits")):
            return self._submit(world, response, pioneer, parsed[1])
        if parsed is None:
            rlog(world.round, "llmResp未解析出CMD/ANSWER,回落探索梯子")
        if task.get("submits", 0) >= 2:
            # Wrong twice: walk away (ends the task, partial credit) instead of squatting.
            rlog(world.round, f"已提交{task['submits']}次仍失败,弃坑离开任务点")
            task["abandoned"] = True
            task["state"] = "idle"
            return response
        outputs = task["outputs"]
        out = world.last_cmd.get("out", "")
        if task.get("cmd") and out:
            entry = [task["cmd"], out[:800]]
            if outputs and outputs[-1] == entry:
                task["stuck"] += 1
                rlog(world.round, f"命令输出与上回合相同,stuck={task['stuck']},准备升级探索")
            else:
                outputs.append(entry)
                del outputs[:-6]
                rlog(world.round, f"命令输出已记录(现{len(outputs)}条)")
        if parsed and parsed[0] == "CMD" and len(parsed[1]) <= 300:
            cmd = parsed[1]
            source = "LLM"
        elif outputs or not self.sop.get(task.get("type")):
            rung = min(task.get("stuck", 0), 2)
            cmd = self.LADDER[1] if rung >= 2 else self._doc_cmd(outputs) if rung == 1 else self.LADDER[0]
            source = f"梯子第{rung + 1}级" + ("(文档)" if rung == 1 and cmd else "")
        else:
            # Recipe replay: hand the recorded context straight to the LLM.
            cmd = None
            source = "配方重放(仅prompt)"
        if cmd:
            task["cmd"] = cmd
            response["executeCmd"] = cmd
            rlog(world.round, f"executeCmd({source}): {cmd[:200]}")
        else:
            rlog(world.round, f"本回合无executeCmd({source})")
        response["prompt"] = self._prompt(world, task, outputs)
        rlog(world.round, f"prompt已发送,长度={len(response['prompt'])}")
        return response

    @staticmethod
    def _parse(text):
        for line in str(text or "").splitlines():
            match = re.match(r"\s*(CMD|ANSWER)\s*[:：]\s*(.+)", line, re.I)
            if match:
                return match.group(1).upper(), match.group(2).strip()
        return None

    @staticmethod
    def _doc_cmd(outputs):
        for _, out in outputs:
            names = re.findall(r"\S+\.(?:md|txt|py|json|sh|log)\b", out)
            if names:
                return f"head -120 {names[0]}"
        return None

    def _prompt(self, world, task, outputs):
        recipe = self.sop.get(task.get("type")) or {}
        parts = [f"任务描述:\n{world.phase_task}"]
        if recipe.get("context"):
            parts.append("同类型任务的历史资料(以往任务沙盒中的关键内容):\n" + recipe["context"][:1500])
        if outputs:
            parts.append("沙盒探索记录(命令与输出):\n" +
                         "\n".join(f"$ {c}\n{o}" for c, o in outputs)[:4000])
        parts.append("只输出一行,格式二选一,不要有任何其他文字:\n"
                     "CMD: <一条15秒内可完成的shell命令,沙盒不能访问外网>\n"
                     "ANSWER: <任务的最终答案文本>")
        return "\n\n".join(parts)

    def _submit(self, world, response, pioneer, answer):
        task = self.task
        task["submits"] = task.get("submits", 0) + 1
        task["guess"] = answer
        task["submitted_round"] = world.round
        response["roleCommandMap"][str(pioneer.id)] = {"action": "submitAnswer",
                                                       "taskAnswer": str(answer)[:2000]}
        rlog(world.round, f"submitAnswer(第{task['submits']}次): {str(answer)[:200]!r}")
        return response

    def _record(self, round_no):
        task = self.task
        kind = task.get("type")
        if not kind or task.get("submits", 0) < 1:
            rlog(round_no, f"任务结束但未记录SOP: 类型={kind!r} 提交次数={task.get('submits', 0)}")
            return
        context = "\n".join(f"$ {c}\n{o}" for c, o in task.get("outputs", []))
        self.sop[kind] = {"cmds": [c for c, _ in task.get("outputs", [])],
                          "context": context[:3000]}
        rlog(round_no, f"SOP已记录: 类型={kind} 命令序列={[c for c, _ in task.get('outputs', [])]} "
                       f"资料长度={len(context)}")


class Agent:
    """Small match-local memory; authoritative positions/resources always come from requests."""
    def __init__(self):
        self.key = None
        self.last_round = 0
        self.previous = {}
        self.avoided = {}
        self.task = {"state": "idle"}
        self.sop = {}

    def decide(self, request):
        world = World(request)
        if not world.station or not world.actors:
            return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
        key = (request["teamOur"].get("teamId"), world.team, world.station.p)
        if key != self.key or world.round <= self.last_round:
            self.previous, self.avoided = {}, {}
            self.task = {"state": "idle"}
            rlog(world.round, f"任务记忆重置(新对局或回合回退), key={key!r}")
        if world.round == self.last_round + 1:
            results = request.get("lastRoundRoleActionResults", {})
            for role, cmd in self.previous.items():
                if results.get(role) is False and cmd["action"] == "collect":
                    target = cmd["targetPos"][0]
                    self.avoided[(target["x"], target["y"])] = world.round + 10
        self.avoided = {p: expiry for p, expiry in self.avoided.items() if expiry > world.round}
        self.task["known"] = sorted(self.sop)
        rlog(world.round, f"回合头: day_tick={world.day_tick} {'白天' if world.day else '夜晚'} "
                          f"金币={world.gold} 任务状态={self.task.get('state')} "
                          f"phaseTask={world.phase_task[:120]!r} 已知SOP={self.task['known']}")
        response = Planner(world, self.avoided, task=self.task).run()
        try:
            response = TaskSolver(self.sop, self.task).step(world, response)
        except Exception:
            # The task layer must never take down the round's defence commands.
            rlog(world.round, "TaskSolver异常(任务层已隔离,防御指令不受影响):\n"
                              + traceback.format_exc()[-1200:])
            self.task = {"state": "idle"}
        self.key, self.last_round = key, world.round
        self.previous = response["roleCommandMap"]
        return response
