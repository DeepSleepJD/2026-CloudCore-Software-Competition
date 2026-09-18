"""Small observable-state memory; no learned world dynamics."""
from collections import deque
from .model import distance


class Memory:
    def __init__(self):
        self.previous = {}
        self.damage = {}
        self.pressure = {}
        self.robot_tracks = {}
        self.mines = {}
        self.selling = set()
        self.orders = {}
        self.night_guard = None
        self.deliveries = {}
        self.positions = {}
        self.stalled = set()

    def observe(self, world, consecutive=True):
        alive = {u.id for u in world.actors}
        self.mines = {k: v for k, v in self.mines.items() if k in alive}
        self.selling.intersection_update(alive)
        self.deliveries = {k: v for k, v in self.deliveries.items() if k in alive}
        self.stalled.clear()
        for actor in world.actors:
            history = self.positions.setdefault(actor.id, deque(maxlen=6))
            if not consecutive:
                history.clear()
            history.append(actor.p)
            if len(history) == 6 and len(set(history)) <= 2 and len(set(history)) > 1:
                self.stalled.add(actor.id)
                self.mines.pop(actor.id, None)
        for u in world.ours:
            old = self.previous.get(u.id)
            loss = max(0, old.health - u.health) if old and old.level == u.level and consecutive else 0
            history = self.damage.setdefault(u.id, deque(maxlen=3))
            history.append(loss)
            self.pressure[u.id] = max(self.pressure.get(u.id, 0), loss)
        threatened_people = [u for u in world.actors if self.damage.get(u.id) and self.damage[u.id][-1] > 0]
        enemy_base = next((u for u in world.enemy if u.kind == "station"), None)
        selected, tracks = [], {}
        for r in world.robots:
            previous = self.robot_tracks.get(r.id)
            toward = previous[1] + 1 if consecutive and previous and world.base_distance(r.p) < world.base_distance(previous[0]) else 0
            tracks[r.id] = (r.p, toward)
            # Explicit ownership is authoritative. A robot hitting our base/people
            # is an exception for immediate self-defence, not score farming.
            immediate = world.base_distance(r.p) <= 3 or any(distance(r.p, u.p) <= 3 for u in threatened_people)
            unknown = r.target_team not in {"challenger", "defender"}
            inferred = unknown and (world.base_distance(r.p) <= 5 or
                         (toward >= 2 and enemy_base and world.base_distance(r.p) <
                          min(distance(r.p, p) for p in enemy_base.cells())))
            if r.target_team == world.team or immediate or inferred:
                selected.append(r)
        world.threats = selected
        self.robot_tracks = tracks
        self.previous = {u.id: u for u in world.ours}

    def rate(self, unit):
        recent = self.damage.get(unit.id, ())
        return max(recent[-1], sum(recent) / len(recent)) if recent else 0
