"""Eight-way shortest paths with per-turn destination reservations."""
from collections import deque
from .model import neighbours


def paths(world, actor, reserved=(), forbidden=(), start=None, ignore_actors=False):
    start = actor.p if start is None else start
    blocked = (world.blocked | set(reserved) | set(forbidden)) - {actor.p, start}
    if ignore_actors:
        # Long-range trip estimates may assume allies eventually yield. Never
        # enable this for a move command's actual next step.
        blocked -= {r.p for r in world.actors}
    queue = deque([start])
    distances, first = {start: 0}, {start: None}
    while queue:
        current = queue.popleft()
        for cell in neighbours(current):
            if cell in distances or cell in blocked or not world.inside(cell):
                continue
            distances[cell] = distances[current] + 1
            first[cell] = cell if current == start else first[current]
            queue.append(cell)
    return distances, first
