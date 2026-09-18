from dataclasses import dataclass, field
import json
from pathlib import Path


@dataclass
class Config:
    profile: str = "legacy"
    pvp_mode: str = "auto"  # off / auto (probe first) / on (explicit experiment)
    tower_loadout: list[str] = field(default_factory=lambda: ["rocket", "gatling", "rocket"])
    initial_towers: int = 2
    wall_limit: int = 6
    return_margin: int = 6
    sale_batch: int = 8
    third_tower_reserve: int = 25
    enable_raids: bool = True
    idle_replan_rounds: int = 3
    task_llm_budget: int = 12
    task_cmd_budget: int = 16
    state_dir: str = ".zk_state"
    # Offsets from the upper-left station coordinate. Empty uses Demo-style rings.
    tower_offsets: list[list[int]] = field(default_factory=list)
    wall_offsets: list[list[int]] = field(default_factory=list)

    @classmethod
    def baseline(cls, **overrides):
        values = dict(profile="baseline", pvp_mode="off", enable_raids=False,
                      tower_loadout=["rocket", "rocket", "rocket"],
                      initial_towers=3, wall_limit=9, sale_batch=10)
        values.update(overrides)
        return cls(**values)

    @classmethod
    def load(cls, path: str | None):
        values = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        obj = cls.baseline(**values) if values.get("profile", "baseline") == "baseline" else cls(**values)
        if obj.profile not in {"legacy", "baseline"}:
            raise ValueError("profile must be legacy or baseline")
        if obj.pvp_mode not in {"off", "auto", "on"}:
            raise ValueError("pvp_mode must be off, auto or on")
        if not 1 <= obj.initial_towers <= 3 or len(obj.tower_loadout) != 3:
            raise ValueError("initial_towers must be 1..3; provide three tower types")
        if any(t not in {"rocket", "gatling", "railgun"} for t in obj.tower_loadout):
            raise ValueError("unknown tower type")
        if min(obj.wall_limit, obj.return_margin, obj.sale_batch,
               obj.task_llm_budget, obj.task_cmd_budget, obj.third_tower_reserve) < 0:
            raise ValueError("budgets must be nonnegative")
        if obj.idle_replan_rounds < 1:
            raise ValueError("idle_replan_rounds must be positive")
        for pair in obj.tower_offsets + obj.wall_offsets:
            if len(pair) != 2 or any(type(n) is not int for n in pair):
                raise ValueError("layout offsets must be integer pairs")
        return obj
