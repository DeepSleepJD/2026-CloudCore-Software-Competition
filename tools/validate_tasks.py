"""Repeatable task protocol rollout; deliberately uses a synthetic LLM/judger."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_tasks import TaskRollout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rounds", type=int, default=260)
    args = parser.parse_args()
    report = {"validation": "synthetic; no official platform, real LLM or task sandbox", "sides": []}
    for left in (True, False):
        sim = TaskRollout(left)
        for _ in range(args.rounds):
            sim.step()
        report["sides"].append({"side": sim.p["teamOur"]["type"], "rounds": args.rounds,
                                "events": sim.events, "history": sim.agent.memory["tasks"]["history"],
                                "maxDecisionSeconds": max(sim.times)})
        assert any(h["reason"] == "completed_inferred" for h in sim.agent.memory["tasks"]["history"])
        assert max(sim.times) < 5
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"validation": report["validation"], "sides": [
        {k: v for k, v in s.items() if k != "events"} for s in report["sides"]]}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
