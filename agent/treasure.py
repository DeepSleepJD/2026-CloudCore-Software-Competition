"""Evidence-backed treasure hypotheses with bounded daily LLM use and spending."""
from collections import Counter
import hashlib
import json
import logging

from .logistics import repair_stock, upgrade_goal
from .model import command, distance, neighbours
from .navigation import paths
from .tasks import envelope, INF

LOG = logging.getLogger(__name__)
SUPPLIES = {"Medicine", "WallFixer", "DizzyWeapon", "Bomb", "stone", "iron", "copper"}


class TreasureScheduler:
    def __init__(self, planner):
        self.p, self.w = planner, planner.w
        self.m = planner.memory.setdefault("treasure", {"news": [], "calls": {}, "tried": [],
                                                        "history": [], "plan": None})
        self.day = (self.w.round - 1) // 130 + 1
        self.actor = next((r for r in self.w.actors if r.kind == "pioneer"), None)
        news = self.w.raw.get("worldNews") or {}
        if any(news.get(k) for k in ("officialNews", "folkLegends")):
            entry = {"day": self.day, "officialNews": str(news.get("officialNews", ""))[:16000],
                     "folkLegends": str(news.get("folkLegends", ""))[:16000]}
            if entry not in self.m["news"]:
                self.m["news"] = (self.m["news"] + [entry])[-20:]
        # A new day or a repeated news payload is not new evidence for sacrifice.
        clues = sorted({n["folkLegends"] for n in self.m["news"] if n["folkLegends"]})
        self.version = hashlib.sha256(json.dumps(clues, ensure_ascii=False).encode()).hexdigest()
        self.items = [name for name in self.w.shop if name not in SUPPLIES
                      and "UpgradeVoucher" not in name and not name.endswith("RobotSummonOrder")]
        pending = self.m.pop("summon_pending", None)
        if pending:
            result = self.w.raw.get("lastSummonTreasureResult", 0) if pending["round"] + 1 == self.w.round else 0
            record = {"round": self.w.round, "result": result, "fingerprint": pending["fingerprint"]}
            self.m["history"].append(record)
            LOG.info("treasure_result=%s", record)
            if result in (1, 4):
                self.m["done"] = True
            # Even a failed legal summon consumes items. Never repeat this plan.
            self.m["plan"] = None

    def validate(self, value):
        try:
            p = value["targetPos"]
            assert isinstance(p, dict) and set(p) == {"x", "y"}
            assert all(type(p[k]) is int for k in p)
            assert self.w.inside((p["x"], p["y"]))
            start, end = value["openRound"], value["closeRound"]
            assert type(start) is int and type(end) is int and 1 <= start <= end <= 1300
            assert end >= self.w.round
            items = value["item"]
            assert isinstance(items, list) and 1 <= len(items) <= 6
            assert all(isinstance(item, str) and item in self.items for item in items)
            assert value["confidence"] == "high"
            for field in ("position", "window", "items"):
                citations = value["evidence"][field]
                assert isinstance(citations, list) and citations
                for citation in citations:
                    quote = citation["quote"]
                    assert isinstance(quote, str) and len(quote) >= 4
                    assert any(n["day"] == citation["day"] and quote in n["folkLegends"] for n in self.m["news"])
            plan = {"targetPos": p, "openRound": start, "closeRound": end, "item": sorted(items)}
            plan["fingerprint"] = json.dumps(plan, sort_keys=True, ensure_ascii=False)
            assert plan["fingerprint"] not in self.m["tried"]
            plan["evidence"] = value["evidence"]
            plan["version"] = self.version
            return plan
        except (AssertionError, KeyError, TypeError, ValueError):
            return None

    def infer(self, tasks_busy):
        """One response channel owner per round; task calls are quota-exempt."""
        if self.m.get("plan") and self.m["plan"]["version"] != self.version:
            self.m["plan"] = None
        pending = self.m.pop("llm_pending", None)
        if pending:
            if self.w.round == pending["round"] + 1:
                codes = {e.get("errorCode") for e in self.w.raw.get("errors", [])}
                if 5 in codes:
                    self.m["calls"][self.day] = 3
                elif not tasks_busy and pending["version"] == self.version:
                    self.m["plan"] = self.validate(envelope(self.w.raw.get("llmResp") or ""))
        if (tasks_busy or self.m.get("done") or self.m.get("plan") or not self.actor
                or not self.items or self.m.get("attempted_version") == self.version
                or self.m["calls"].get(self.day, 0) >= 3
                or not any(n["folkLegends"] for n in self.m["news"])):
            return ""
        self.m["calls"][self.day] = self.m["calls"].get(self.day, 0) + 1
        self.m["attempted_version"] = self.version
        self.m["llm_pending"] = {"round": self.w.round, "version": self.version}
        return (
            '根据跨日民间传闻推断宝藏，只依据提供的线索，不猜测。不足则返回{}。'
            '有充分证据时返回JSON：{"targetPos":{"x":整数,"y":整数},"item":[精确商品名],'
            '"openRound":首个可开回合,"closeRound":最后可开回合,"confidence":"high",'
            '"evidence":{"position":[{"day":天数,"quote":"原文引用"}],'
            '"window":[{"day":天数,"quote":"原文引用"}],"items":[{"day":天数,"quote":"原文引用"}]}}。'
            '每一天130回合，前70回合白天。不得多给或漏给献祭物品；商品名拼写必须与列表一致。'
            '坐标、完整物品组合和开启时间分别需要原文证据；不确定不要购买或献祭。\n'
            + json.dumps({"news": self.m["news"], "items": self.items,
                          "round": self.w.round, "previousAttempts": self.m["history"]}, ensure_ascii=False))

    def reserve(self):
        goal = upgrade_goal(self.w)
        upgrades = self.w.shop.get(goal[0], 0) if goal and not any(goal[0] in r.bag for r in self.w.actors) else 0
        repair = sum(max(0, repair_stock(self.w) - r.bag.count("WallFixer")) for r in self.w.workers)
        return max(upgrades, self.p.upgrade_reserve) + repair * self.w.shop.get("WallFixer", 10) + 25 * max(0, 3 - self.p.build_count) + 30

    def act(self, actor):
        plan = self.m.get("plan")
        if not plan or self.m.get("done") or not self.w.day or self.w.raw.get("phaseTask"):
            return False
        if self.w.round > plan["closeRound"]:
            self.m["plan"] = None
            return False
        if actor.health < 100 or actor.p in self.p.danger:
            return False
        target = (plan["targetPos"]["x"], plan["targetPos"]["y"])
        dist, _ = self.p.route(actor)
        home, _ = paths(self.w, actor, start=self.p.layout.operator, ignore_actors=True)
        goals = [q for q in neighbours(target) if q in dist and q not in self.p.danger]
        if not goals:
            return False
        goal = min(goals, key=lambda q: (dist[q] + home.get(q, INF), q))
        wait = max(0, plan["openRound"] - (self.w.round + dist[goal]))
        if dist[goal] + wait + home.get(goal, INF) + 6 >= 70 - self.w.day_tick:
            return False
        if self.w.round + dist[goal] > plan["closeRound"]:
            return False
        missing = Counter(plan["item"]) - Counter(actor.bag)
        if missing:
            if any(name not in self.w.shop for name in missing):
                return False
            cost = sum(self.w.shop[name] * n for name, n in missing.items())
            if self.p.budget - cost < self.reserve() or len(actor.bag) + sum(missing.values()) > actor.capacity:
                return False
            # Include the shop detour and every separate buy action before dispatch.
            for shop in sorted(p for p, k in self.w.zones.items() if k == "weaponShop"):
                shop_cells = [q for q in neighbours(shop) if q in dist]
                for cell in sorted(shop_cells, key=lambda q: dist[q]):
                    after, _ = paths(self.w, actor, start=cell, ignore_actors=True)
                    onward = min((after.get(q, INF) + home.get(q, INF) for q in goals), default=INF)
                    if dist[cell] + len(missing) + onward + 6 >= 70 - self.w.day_tick:
                        continue
                    name = sorted(missing)[0]
                    if self.p.approach(actor, shop, command("buy", name=name, num=missing[name])):
                        if self.p.commands[str(actor.id)]["action"] == "buy":
                            self.p.budget -= self.w.shop[name] * missing[name]
                        return True
            return False
        if distance(actor.p, target) == 1:
            if self.w.round < plan["openRound"]:
                return True
            self.p.emit(actor, command("summonTreasure", target, item=plan["item"]))
            self.m["tried"].append(plan["fingerprint"])
            self.m["summon_pending"] = {"round": self.w.round, "fingerprint": plan["fingerprint"]}
            return True
        return self.p.approach(actor, goal, exact=True)
