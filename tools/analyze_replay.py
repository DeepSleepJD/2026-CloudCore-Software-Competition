"""Inspect champion evidence and validate decisions against recorded states.

Replay frames are post-action snapshots, not Request messages. Conversion here
is offline only and must never be used as the platform HTTP protocol.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.strategy import Agent
from tests.support import check

SHOP = {"WeaponUpgradeVoucher1":100,"WeaponUpgradeVoucher2":150,
        "WallUpgradeVoucher1":20,"WallUpgradeVoucher2":30,
        "StationUpgradeVoucher1":100,"StationUpgradeVoucher2":150,
        "WallFixer":10,"Medicine":10}
RESOURCES = {"石头":"stone","铁":"iron","铜":"copper"}
ROBOTS = {"smallRobot","middleRobot","largeRobot","bossRobot"}


def read_frames(path):
    frames=[]
    markers=[]
    for number,line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(),1):
        if not line.strip():continue
        if line.strip()=="valid":
            markers.append(number)
            continue
        frames.append(json.loads(line))
    return frames,markers


def request_from_frame(frame,start,team_index,last_shots):
    kinds={v["mapType"]:k for k,v in start["roles"].items()}
    robots=[]
    teams=[]
    for team in frame["teams"]:
        units=[]
        for role in team["roles"]:
            kind=kinds[role["roleType"]]
            if role["health"]<=0:continue
            level=max(1,role.get("level",1))
            converted={"id":role["id"],"pos":role["pos"],"roleType":kind,"health":role["health"],
                       "level":level,"backpack":role.get("backpacks",[]),
                       "backPackCapability":100 if kind=="worker" else 40,
                       "cooldown":max(0,3-(frame["round"]-last_shots.get(role["id"],-10000))),
                       "attackRange":(10,15,2147483647)[level-1] if kind=="rocket" else 0}
            if kind in ROBOTS:
                converted["targetTeam"]=team["type"];robots.append(converted)
            else:units.append(converted)
        teams.append({"type":team["type"],"teamId":team["teamId"],"teamName":team["teamName"],
                      "goldNum":team["goldNum"],"roles":units})
    zones=[{"pos":r["pos"],"neutralType":RESOURCES.get(r["resName"],r["resName"])} for r in frame["resources"]]
    zones += [{"pos":r["pos"],"neutralType":r["roleName"]} for r in frame["npc"]]
    return {"roundNo":frame["round"]+1,"mapInfo":{"width":start["map"]["width"],"height":start["map"]["height"],"zones":zones},
            "teamOur":teams[team_index],"teamEnemy":{"roles":teams[1-team_index]["roles"]},
            "robot":{"roles":robots},"vendorShopList":frame.get("vendorShopList",[]),
            "weaponShopList":[{"name":k,"price":v} for k,v in SHOP.items()],
            "worldNews":frame.get("news",{}),"lastRoundRoleActionResults":{}}


def analyze(path,validate=False):
    frames,markers=read_frames(path)
    start=frames[0]
    champion=next(i for i,t in enumerate(start["teams"]) if t["teamName"]=="Agentic麻辣烫")
    actor_kinds={start["roles"][k]["mapType"] for k in ("worker","pioneer","rocket","wall","station")}
    counts=Counter();events=[];shots={};validated=0;elapsed=[];generated=Counter()
    for frame in frames:
        if frame.get("type")!="round":continue
        n=frame["round"]
        for team in frame["teams"]:
            for role in team["roles"]:
                for cmd in role.get("commands",[]):
                    if role["roleType"]==start["roles"]["rocket"]["mapType"] and cmd["action"]=="attack" and cmd["valid"]:
                        shots[role["id"]]=n
        for role in frame["teams"][champion]["roles"]:
            if role["roleType"] not in actor_kinds:continue
            for cmd in role.get("commands",[]):
                counts[cmd["action"]+":"+str(cmd.get("targetName"))]+=1
                if n<=70 and cmd["action"] in {"build","buy","use","sell"}:
                    events.append({"round":n,"role":role["id"],"action":cmd["action"],"name":cmd.get("targetName"),"targetPos":cmd.get("targetPos"),"valid":cmd["valid"]})
        if validate:
            for team_index in (0,1):
                request=request_from_frame(frame,start,team_index,shots)
                if not any(r["roleType"]=="station" for r in request["teamOur"]["roles"]):continue
                before=time.perf_counter()
                # A fresh agent prevents champion feedback from being mistaken for ours.
                response=Agent().decide(request)
                elapsed.append(time.perf_counter()-before)
                try:check(request,response)
                except Exception as error:
                    raise AssertionError(f"frame={n} team={team_index} response={response}") from error
                generated.update(c["action"] for c in response["roleCommandMap"].values())
                validated+=1
    return {"team":start["teams"][champion]["teamName"],"teamId":start["teams"][champion]["teamId"],
            "lastRound":max(f.get("round",0) for f in frames),"ignoredTrailerLines":markers,
            "championActions":dict(counts),"firstDayEvents":events,"validatedSnapshots":validated,
            "generatedActions":dict(generated),"maxDecisionSeconds":max(elapsed,default=0),
            "validationScope":"Offline snapshot legality only; not a counterfactual match or survival test. Shop prices/ranges from current task document; cooldown reconstructed from recorded shots."}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay",type=Path)
    parser.add_argument("--validate",action="store_true")
    args=parser.parse_args()
    print(json.dumps(analyze(args.replay,args.validate),ensure_ascii=False,indent=2))
