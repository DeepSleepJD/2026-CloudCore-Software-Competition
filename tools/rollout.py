"""Reproducible limited rollout; deliberately does not claim match survival."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent.strategy import Agent
from tests.support import EconomyRollout


def run(rounds,seed):
    reports=[]
    for left in (True,False):
        sim=EconomyRollout(left,seed);agent=Agent();slowest=0;opening={}
        for index in range(rounds):
            before=time.perf_counter();response=agent.decide(sim.request)
            slowest=max(slowest,time.perf_counter()-before)
            sim.step(response)
            if index==69:
                opening={k:sum(r["roleType"]==k for r in sim.request["teamOur"]["roles"]) for k in ("rocket","wall")}
        reports.append({"side":"left" if left else "right","rounds":rounds,"seed":seed,
                        "opening":opening,"actions":dict(sim.counts),"maxDecisionSeconds":slowest,
                        "gold":sim.request["teamOur"]["goldNum"],
                        "buildingLevels":[{"kind":r["roleType"],"level":r["level"]} for r in sim.request["teamOur"]["roles"] if r["roleType"] in ("station","rocket")]})
    return {"scope":"Economy/legality rollout with stationary synthetic targets; no robot AI or survival scoring.","results":reports}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds",type=int,default=1300)
    parser.add_argument("--seed",type=int,default=42)
    args=parser.parse_args()
    print(json.dumps(run(args.rounds,args.seed),indent=2))
