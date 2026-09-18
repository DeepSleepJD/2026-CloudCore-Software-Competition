"""Pioneer task pipeline and evidence-gated treasure trips."""
from types import SimpleNamespace
from .model import command, distance, neighbours
from .news import News
from .tasks import TaskRunner


class TaskWorld:
    def __init__(self, planner):
        self.planner = planner
        self.data = planner.w.raw
        self.round = planner.w.round
        self.pioneer = {"id": planner.w.pioneer.id, "pos": planner.w.pioneer.p} if planner.w.pioneer else None

    def put(self, actor, value):
        return self.planner.emit(self.planner.w.pioneer, value)


class Missions:
    def __init__(self, identity, state_dir):
        self.news = News()
        self.runner = TaskRunner(SimpleNamespace(state_dir=state_dir), identity)
        self.abandon = False

    def leave(self, planner):
        actor = planner.w.pioneer
        points = [p for p, k in planner.w.zones.items() if k.startswith(planner.w.team + "TaskPoint")]
        distances, first = planner.route(actor)
        goals = [p for p in distances if p != actor.p and all(distance(p, t) > 1 for t in points)]
        if goals:
            target = min(goals, key=lambda p: (distances[p], distance(p, planner.layout.operator), p))
            planner.emit(actor, command("move", first[target]))

    def active(self, planner):
        w = planner.w
        if not w.raw.get("phaseTask"):
            self.runner.step(TaskWorld(planner))
            self.abandon = False
            return False
        if not w.pioneer:
            self.runner.step(TaskWorld(planner))
            return False
        danger = w.pioneer.p in planner.danger
        emergency = planner.emergency() or self.abandon
        # A returned answer can be submitted within the broad route-avoidance area.
        # A robot within two cells can reach/attack the pioneer this turn: save the
        # response first, then prioritize healing or escape instead of standing still.
        imminent = any(distance(w.pioneer.p, r.p) <= max(2, r.reach + 1) for r in w.robots)
        danger = danger or imminent
        result = self.runner.step(TaskWorld(planner), allow_work=not (danger or emergency),
                                  allow_submit=not imminent and not self.abandon)
        self.runner.trace.emit('scheduling', w.round, danger=danger, imminent=imminent,
                               emergency=emergency, already_abandoning=self.abandon,
                               position=w.pioneer.p, health=w.pioneer.health,
                               nearby_robots=[{'id': r.id, 'position': r.p, 'distance': distance(w.pioneer.p, r.p)}
                                              for r in w.robots if distance(w.pioneer.p, r.p) <= 8])
        if result.get("answered"):
            planner.used.add(w.pioneer.id)
            return True
        if danger:
            # Do not hold a task lock while the pioneer is exposed to a wave.
            if "Medicine" in w.pioneer.bag and w.pioneer.health < 160:
                self.runner.trace.emit('survival_action', w.round, reason='heal_then_retry')
                planner.emit(w.pioneer, command("use", name="Medicine"))
            else:
                self.runner.trace.exit_reason = 'robot_danger_retreat'
                self.runner.trace.emit('survival_action', w.round, reason=self.runner.trace.exit_reason)
                self.abandon = True
                planner.retreat(w.pioneer)
            planner.used.add(w.pioneer.id)
            return True
        if emergency:
            self.runner.trace.exit_reason = self.runner.trace.exit_reason or 'base_emergency_leave'
            self.runner.trace.emit('survival_action', w.round, reason=self.runner.trace.exit_reason)
            self.abandon = True
            self.leave(planner)
            planner.used.add(w.pioneer.id)
            return True
        if result.get("exhausted"):
            self.runner.trace.exit_reason = 'task_budget_exhausted'
            self.runner.trace.emit('survival_action', w.round, reason=self.runner.trace.exit_reason)
            self.abandon = True
            self.leave(planner)
        else:
            planner.extra.update({k: v for k, v in result.items() if k in {"prompt", "executeCmd"}})
        planner.used.add(w.pioneer.id)
        return True

    def treasure(self, planner, actor):
        w, news = planner.w, self.news
        t = news.treasure
        if not t or news.treasure_done or news.attempts >= 2 or planner.emergency():
            return False
        if t["day"] < w.day_no or t["day"] > w.day_no + 1:
            return False
        if not w.day and actor == planner.operator:
            return False
        if w.round < news.retry_after:
            return False
        missing = [item for item in t["items"] if item not in actor.bag]
        if missing:
            if not w.day or sum(w.shop.get(i, 10**9) for i in missing) > planner.budget - planner.defence_reserve():
                return False
            return planner.purchase(actor, missing[0], reserve=planner.defence_reserve())
        if not news.due(w):
            return False
        distances, _ = planner.route(actor)
        travel = min((distances[p] for p in neighbours(t["position"]) if p in distances), default=10**6)
        back = distance(t["position"], planner.layout.operator) if actor == planner.operator else 0
        remaining = (70 if w.day else 130) - w.day_tick
        if travel + back + 4 >= remaining:
            return False
        if planner.approach(actor, t["position"], command("summonTreasure", t["position"], item=t["items"])):
            action = planner.commands.get(str(actor.id), {})
            if action.get("action") == "summonTreasure":
                news.attempts += 1
                news.awaiting_treasure = True
            return True
        return False

    def choose(self, planner, actor):
        w = planner.w
        if planner.emergency():
            return False
        if self.treasure(planner, actor):
            return True
        if not w.workers:
            return False
        if not w.day and not planner.quiet_night() and not (planner.operator != actor and planner.operator.p == planner.layout.operator):
            return False
        distances, _ = planner.route(actor)
        choices = []
        for task in w.tasks:
            if not task.get("isValid") or (task.get("coldDownRounds") or 0) > 0:
                continue
            suffix = "2" if "2" in task.get("taskType", "") else "1"
            targets = [p for p, kind in w.zones.items() if kind == w.team + "TaskPoint" + suffix]
            if not targets and task.get("taskPosition"):
                p = task["taskPosition"]
                targets = [(p["x"], p["y"])]
            for target in targets:
                travel = min((distances[p] for p in neighbours(target) if p in distances), default=10**6)
                # Reserve a return path; an unexpectedly long task gets a worker relief.
                work = min(20, int(task.get("timeoutRounds") or 60))
                back = distance(target, planner.layout.operator)
                # During a cleared night the next dangerous boundary is next dusk.
                available = 70 - w.day_tick if w.day else 200 - w.day_tick
                if travel + work + back + 6 >= available:
                    continue
                value = int(task.get("goldReward") or 0) + int(task.get("scoreReward") or 0)
                choices.append((travel, -value, target, int(task.get("timeoutRounds") or 60)))
        for _, _, target, timeout in sorted(choices):
            if planner.approach(actor, target, command("acceptTask")):
                if planner.commands.get(str(actor.id), {}).get("action") == "acceptTask":
                    self.runner.accepted(w.round, timeout)
                return True
        return False
