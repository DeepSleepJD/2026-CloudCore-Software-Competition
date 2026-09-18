"""Independent action checks and a limited economy rollout, NOT the official judger."""
import copy
import random
from collections import Counter


def d(a, b):
    return max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"]))


def point(x, y):
    return {"x": x, "y": y}


def unit(i, kind, x, y, **extra):
    return {"id": i, "roleType": kind, "pos": point(x, y),
            "health": 1500 if kind == "station" else 1000 if kind in ("rocket", "wall") else 220,
            "level": 1, "cooldown": 0, "attackRange": 10 if kind == "rocket" else 0,
            "backpack": [], "backPackCapability": 100 if kind == "worker" else 40, **extra}


def initial(left=False):
    base = 10000 if left else 20000
    roles = ([unit(base+10, "worker", 8, 21), unit(base+11, "pioneer", 8, 23),
              unit(base+12, "worker", 8, 22), unit(base+13, "station", 9, 22)] if left else
             [unit(base+10, "worker", 30, 8), unit(base+11, "pioneer", 32, 8),
              unit(base+12, "worker", 31, 8), unit(base+13, "station", 30, 10)])
    ore_data = [(8,27,"stone"),(37,14,"copper"),(2,7,"stone"),(39,18,"iron"),
                (10,1,"stone"),(32,13,"iron"),(7,25,"copper"),(17,16,"stone"),
                (21,13,"copper"),(4,18,"copper"),(19,20,"iron"),(40,20,"stone"),
                (0,30,"iron"),(33,14,"stone"),(20,16,"vendor"),(25,20,"weaponShop"),
                (14,14,"challengerTaskPoint1"),(16,17,"challengerTaskPoint2"),
                (17,17,"challengerTaskPoint2"),(23,14,"defenderTaskPoint1"),
                (26,17,"defenderTaskPoint2"),(27,17,"defenderTaskPoint2")]
    shop = {"WeaponUpgradeVoucher1":100,"WeaponUpgradeVoucher2":150,
            "StationUpgradeVoucher1":100,"StationUpgradeVoucher2":150,
            "WallUpgradeVoucher1":20,"WallUpgradeVoucher2":30,"WallFixer":10,"Medicine":10}
    return {"roundNo":1,"mapInfo":{"width":41,"height":32,"zones":[
                {"pos":point(x,y),"neutralType":k} for x,y,k in ore_data]},
            "teamOur":{"type":"challenger" if left else "defender", "teamId":"test", "goldNum":75,"roles":roles},
            "teamEnemy":{"roles":[unit(20013 if left else 10013,"station",30 if left else 9,10 if left else 22)]},
            "robot":{"roles":[]},"lastRoundRoleActionResults":{},
            "vendorShopList":[{"name":k,"price":v} for k,v in {"stone":1,"iron":3,"copper":5}.items()],
            "weaponShopList":[{"name":k,"price":v} for k,v in shop.items()]}


def footprint(r):
    x,y=r["pos"].values()
    return {(x,y),(x+1,y),(x,y-1),(x+1,y-1)} if r["roleType"]=="station" else {(x,y)}


def check(request, response):
    """Raise on illegal commands before applying any simultaneous effects."""
    assert set(response)=={"roleCommandMap","prompt","executeCmd"}
    assert response["prompt"] == response["executeCmd"] == ""
    commands=response["roleCommandMap"]
    assert isinstance(commands,dict)
    roles={str(r["id"]):r for r in request["teamOur"]["roles"] if r["health"]>0}
    station=next(r for r in roles.values() if r["roleType"]=="station")
    blocks=set().union(*(footprint(r) for r in list(roles.values())+request["teamEnemy"]["roles"]+request["robot"]["roles"] if r["health"]>0))
    zones={tuple(z["pos"].values()):z["neutralType"] for z in request["mapInfo"]["zones"]}
    blocks |= {p for p,k in zones.items() if k!="land"}
    day=(request["roundNo"]-1)%130<70
    targets=set();controllers=set();cost=0
    towers=sum(r["roleType"] in ("rocket","railgun","gatling") for r in roles.values())
    for i,c in commands.items():
        assert isinstance(i,str) and i in roles,("unknown actor",i)
        r=roles[i];a=c["action"]
        assert a in {"move","build","collect","attack","sell","buy","use"},a
        assert r["roleType"] in ({"rocket"} if a=="attack" else {"worker","pioneer"})
        if a in {"move","build","collect","attack"} or (a=="use" and c["name"]!="Medicine"):
            ps=c["targetPos"]
            assert isinstance(ps,list) and len(ps)==(r["level"] if a=="attack" else 1)
            for p in ps:
                assert set(p)=={"x","y"} and all(type(v) is int for v in p.values())
                assert 0<=p["x"]<request["mapInfo"]["width"] and 0<=p["y"]<request["mapInfo"]["height"]
            p=ps[0];xy=tuple(p.values())
        if a in {"move","build","collect"}:
            assert d(r["pos"],p)==1,(a,i,r["pos"],p)
        if a in {"move","build"}:
            assert xy not in blocks and xy not in targets,("blocked/reserved",a,xy)
            targets.add(xy)
        if a=="build":
            assert day and r["roleType"]=="worker"
            ring=min(max(abs(p["x"]-x),abs(p["y"]-y)) for x,y in footprint(station))
            assert c["name"] in {"wall","rocket"}
            assert ring==(2 if c["name"]=="wall" else 1)
            if c["name"]=="wall":assert "stone" in r["backpack"]
            else: cost+=25;towers+=1
        if a=="collect":
            assert r["roleType"]=="worker" and zones.get(xy) in {"stone","iron","copper"}
            assert len(r["backpack"])<r["backPackCapability"]
        if a=="attack":
            controller=c["controllerId"]
            assert not day and r.get("cooldown",0)==0
            assert isinstance(controller,str) and controller in roles
            assert roles[controller]["roleType"] in {"worker","pioneer"}
            assert d(r["pos"],roles[controller]["pos"])==1
            assert controller not in commands and controller not in controllers
            controllers.add(controller)
            assert all(d(r["pos"],p)<=r["attackRange"] for p in ps)
        if a in {"buy","sell"}:
            assert type(c["num"]) is int and c["num"]>0
            kind="vendor" if a=="sell" else "weaponShop"
            assert any(d(r["pos"],z["pos"])==1 and z["neutralType"]==kind for z in request["mapInfo"]["zones"])
            if a=="sell": assert r["backpack"].count(c["name"])>=c["num"]
            else:
                prices={q["name"]:q["price"] for q in request["weaponShopList"]}
                cost+=prices[c["name"]]*c["num"]
                assert len(r["backpack"])+c["num"]<=r["backPackCapability"]
        if a=="use":
            assert c["name"] in r["backpack"]
            if c["name"]!="Medicine":
                assert d(r["pos"],p)<=1
                target=next(v for v in roles.values() if v["pos"]==p)
                if "UpgradeVoucher" in c["name"]:
                    prefix={"Weapon":"rocket","Station":"station","Wall":"wall"}[c["name"].split("Upgrade")[0]]
                    assert target["roleType"]==prefix and target["level"]==int(c["name"][-1])
                else: assert c["name"]=="WallFixer" and target["roleType"]=="wall"
    assert cost<=request["teamOur"]["goldNum"],("overspend",cost)
    assert towers<=3


class EconomyRollout:
    """Resource depletion, construction, trading, upgrades and cooldowns.

    Synthetic stationary targets exercise combat legality. No robot AI, kills,
    damage to buildings or survival scoring is simulated here.
    """
    def __init__(self,left=False,seed=42):
        self.request=initial(left)
        self.rng=random.Random(seed)
        self.remaining={}
        self.counts=Counter()
        self.next_wall=40000 if left else 41000
        self.next_tower=10040 if left else 20040

    def step(self,response):
        p=self.request
        check(p,response)
        roles={str(r["id"]):r for r in p["teamOur"]["roles"]}
        for r in roles.values():r["cooldown"]=max(0,r.get("cooldown",0)-1)
        depleted=[]
        for i,c in response["roleCommandMap"].items():
            r=roles[i];a=c["action"];name=c.get("name");self.counts[a]+=1
            if a=="move":r["pos"]=copy.deepcopy(c["targetPos"][0])
            elif a=="collect":
                target=c["targetPos"][0];xy=tuple(target.values())
                zone=next(z for z in p["mapInfo"]["zones"] if z["pos"]==target)
                r["backpack"].append(zone["neutralType"])
                self.remaining[xy]=self.remaining.get(xy,10)-1
                if self.remaining[xy]<=0 and zone not in depleted:depleted.append(zone)
            elif a=="build":
                target=c["targetPos"][0]
                if name=="rocket":
                    p["teamOur"]["goldNum"]-=25;uid=self.next_tower;self.next_tower+=1
                else:r["backpack"].remove("stone");uid=self.next_wall;self.next_wall+=1
                p["teamOur"]["roles"].append(unit(uid,name,target["x"],target["y"]))
            elif a=="sell":
                price=next(v["price"] for v in p["vendorShopList"] if v["name"]==name)
                p["teamOur"]["goldNum"]+=c["num"]*price
                for _ in range(c["num"]):r["backpack"].remove(name)
            elif a=="buy":
                price=next(v["price"] for v in p["weaponShopList"] if v["name"]==name)
                p["teamOur"]["goldNum"]-=c["num"]*price;r["backpack"] += [name]*c["num"]
            elif a=="use":
                r["backpack"].remove(name)
                if name=="Medicine":r["health"]=220 if r["roleType"]=="worker" else 200
                else:
                    target=next(v for v in roles.values() if v["pos"]==c["targetPos"][0])
                    if "UpgradeVoucher" in name:target["level"]+=1
                    target["health"]=(1500*target["level"] if target["roleType"]=="station" else 1000+500*(target["level"]-1))
                    if target["roleType"]=="rocket":target["attackRange"]=(10,15,2147483647)[target["level"]-1]
            elif a=="attack":r["cooldown"]=3
        for zone in depleted:
            p["mapInfo"]["zones"].remove(zone)
            self.remaining.pop(tuple(zone["pos"].values()),None)
            bases=[r for r in p["teamOur"]["roles"]+p["teamEnemy"]["roles"] if r["roleType"]=="station"]
            occupied=set().union(*(footprint(r) for r in p["teamOur"]["roles"]+p["teamEnemy"]["roles"]+p["robot"]["roles"]))
            occupied|={tuple(z["pos"].values()) for z in p["mapInfo"]["zones"]}
            choices=[point(x,y) for x in range(41) for y in range(32) if (x,y) not in occupied
                     and all(min(max(abs(x-a),abs(y-b)) for a,b in footprint(base))>2 for base in bases)]
            zone["pos"]=self.rng.choice(choices);p["mapInfo"]["zones"].append(zone)
        p["lastRoundRoleActionResults"]={i:True for i in response["roleCommandMap"]}
        p["roundNo"]+=1
        if (p["roundNo"]-1)%130==70:
            base=next(r for r in p["teamOur"]["roles"] if r["roleType"]=="station")
            x=base["pos"]["x"]+(8 if base["pos"]["x"]<20 else -8)
            p["robot"]["roles"]=[unit(30000+j,"smallRobot",x,base["pos"]["y"]+j-1,health=40,
                                         targetTeam=p["teamOur"]["type"]) for j in range(3)]
        elif (p["roundNo"]-1)%130==0:p["robot"]["roles"]=[]
