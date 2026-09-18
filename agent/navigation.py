"""Eight-way shortest paths with per-turn destination reservations."""
from collections import deque
from .model import neighbours


def paths(world, actor, reserved=(), forbidden=()):
    blocked = (world.blocked | set(reserved) | set(forbidden)) - {actor.p}
    queue = deque([actor.p])
    distances, first = {actor.p: 0}, {actor.p: None}
    while queue:
        current = queue.popleft()
        for cell in neighbours(current):
            if cell in distances or cell in blocked or not world.inside(cell):
                continue
            distances[cell] = distances[current] + 1
            first[cell] = cell if current == actor.p else first[current]
            queue.append(cell)
    return distances, first
