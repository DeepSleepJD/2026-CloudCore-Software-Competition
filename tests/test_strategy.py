import copy
import json
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from agent.model import World, Layout, distance
from agent.strategy import Agent
from agent.server import AgentServer
from tests.support import initial, unit, point, check, EconomyRollout


class StrategyTests(unittest.TestCase):
    def armed(self,left=False,level=1):
        p=initial(left)
        layout=Layout.for_world(World(p))
        prefix=10000 if left else 20000
        for i,(x,y) in enumerate(layout.towers):
            p["teamOur"]["roles"].append(unit(prefix+40+i,"rocket",x,y,level=level,attackRange=(10,15,2147483647)[level-1]))
        pioneer=next(r for r in p["teamOur"]["roles"] if r["roleType"]=="pioneer")
        pioneer["pos"]=point(*layout.operator)
        p["roundNo"]=71
        station=next(r for r in p["teamOur"]["roles"] if r["roleType"]=="station")
        x=station["pos"]["x"]+(5 if left else -5)
        p["robot"]["roles"]=[unit(30000,"smallRobot",x,station["pos"]["y"],health=40,targetTeam=p["teamOur"]["type"])]
        return p

    def test_mirror_and_common_operator(self):
        for left in (False,True):
            w=World(initial(left));layout=Layout.for_world(w)
            self.assertEqual(3,len(layout.towers));self.assertEqual(12,len(set(layout.walls)))
            self.assertTrue(all(distance(p,layout.operator)==1 for p in layout.towers))
            self.assertTrue(all(w.base_distance(p)==1 for p in layout.towers))
            self.assertTrue(all(w.base_distance(p)==2 for p in layout.walls))
            check(w.raw,Agent().decide(w.raw))

    def test_one_controller_one_weapon_and_full_target_array(self):
        for left in (False,True):
            for level in (1,2,3):
                p=self.armed(left,level);r=Agent().decide(p);check(p,r)
                attacks=[v for v in r["roleCommandMap"].values() if v["action"]=="attack"]
                self.assertEqual(1,len(attacks));self.assertEqual(level,len(attacks[0]["targetPos"]))

    def test_cooldown_rotates_and_never_attacks_by_day(self):
        p=self.armed();agent=Agent();ids=[]
        for _ in range(3):
            r=agent.decide(p);check(p,r)
            shot=next(i for i,c in r["roleCommandMap"].items() if c["action"]=="attack")
            ids.append(shot)
            next(v for v in p["teamOur"]["roles"] if str(v["id"])==shot)["cooldown"]=3
            p["roundNo"]+=1
        self.assertEqual(3,len(set(ids)))
        r=agent.decide(p);self.assertFalse(any(c["action"]=="attack" for c in r["roleCommandMap"].values()))
        for tick in (1,70,131,200):
            p["roundNo"]=tick;r=Agent().decide(p);check(p,r)
            self.assertFalse(any(c["action"]=="attack" for c in r["roleCommandMap"].values()))

    def test_shared_gold_and_destroyed_base(self):
        p=initial();p["teamOur"]["goldNum"]=25
        p["teamOur"]["roles"][0]["pos"]=point(30,11)
        p["teamOur"]["roles"][2]["pos"]=point(32,10)
        check(p,Agent().decide(p))
        p["teamOur"]["roles"]=[r for r in p["teamOur"]["roles"] if r["roleType"]!="station"]
        self.assertEqual({},Agent().decide(p)["roleCommandMap"])

    def test_enemy_obstacles_and_destination_reservations(self):
        p=initial()
        p["teamEnemy"]["roles"].extend([unit(50000,"wall",29,9),unit(50001,"worker",30,7)])
        check(p,Agent().decide(p))

    def test_operator_death_falls_back_to_one_worker(self):
        p=self.armed();roles=p["teamOur"]["roles"]
        roles[1]["health"]=0
        roles[0]["pos"]=point(*Layout.for_world(World(p)).operator)
        r=Agent().decide(p);check(p,r)
        attack=next(c for c in r["roleCommandMap"].values() if c["action"]=="attack")
        self.assertEqual(str(roles[0]["id"]),attack["controllerId"])

    def test_repair_upgrade_and_destroyed_tower_reconstruction(self):
        p=self.armed();p["roundNo"]=132;p["robot"]["roles"]=[]
        roles=p["teamOur"]["roles"];roles[0]["pos"]=point(29,10)
        roles[0]["backpack"]=["WallUpgradeVoucher1"]
        roles.append(unit(41000,"wall",28,10,health=100))
        r=Agent().decide(p);check(p,r)
        self.assertEqual("use",r["roleCommandMap"][str(roles[0]["id"] )]["action"])
        dead=next(r for r in roles if r["roleType"]=="rocket")
        dead["health"]=0;roles[2]["pos"]=point(33,9)
        r=Agent().decide(p);check(p,r)
        self.assertTrue(any(c["action"]=="build" and c["name"]=="rocket" for c in r["roleCommandMap"].values()))

    def test_splash_prefers_group_over_isolated_robot(self):
        p=self.armed(level=2)
        p["robot"]["roles"]=[unit(30000+j,"smallRobot",x,y,health=40,targetTeam="defender")
                                for j,(x,y) in enumerate(((25,7),(25,8),(26,7),(25,12)))]
        r=Agent().decide(p);check(p,r)
        attack=next(c for c in r["roleCommandMap"].values() if c["action"]=="attack")
        self.assertLessEqual(attack["targetPos"][0]["y"],9)

    def test_failed_mine_backoff(self):
        p=initial();worker=p["teamOur"]["roles"][0];worker["pos"]=point(33,13)
        p["teamOur"]["goldNum"]=0
        agent=Agent();r=agent.decide(p)
        self.assertEqual("collect",r["roleCommandMap"][str(worker["id"])]["action"])
        p["roundNo"]+=1;p["lastRoundRoleActionResults"]={str(worker["id"]):False}
        agent.decide(p);self.assertIn((33,14),agent.avoided)
        p["roundNo"]=1;agent.decide(p);self.assertEqual({},agent.avoided)

    def test_official_request_example(self):
        source=Path(__file__).resolve().parents[2]/"Competition-main"/"docs"/"request.txt"
        if not source.exists():self.skipTest("external official fixture not present")
        p=json.loads(source.read_text(encoding="utf-8"));r=Agent().decide(p)
        check(p,r)

    def test_opening_both_sides_with_depleting_mines(self):
        for left in (False,True):
            sim=EconomyRollout(left);agent=Agent()
            for _ in range(70):sim.step(agent.decide(sim.request))
            roles=sim.request["teamOur"]["roles"]
            self.assertEqual(3,sum(r["roleType"]=="rocket" for r in roles))
            self.assertEqual(12,sum(r["roleType"]=="wall" for r in roles))
            operator=next(r for r in roles if r["roleType"]=="pioneer")
            towers=[r for r in roles if r["roleType"]=="rocket"]
            self.assertTrue(all(max(abs(operator["pos"]["x"]-r["pos"]["x"]),abs(operator["pos"]["y"]-r["pos"]["y"]))==1 for r in towers))

    def test_worker_buys_and_delivers_critical_front_wall_upgrade(self):
        sim=EconomyRollout();agent=Agent()
        for _ in range(70):sim.step(agent.decide(sim.request))
        p=sim.request;p["roundNo"]=131;p["robot"]["roles"]=[]
        worker=p["teamOur"]["roles"][0]
        worker["pos"]=point(24,19);worker["backpack"]=[]
        p["teamOur"]["goldNum"]=25
        front=set(Layout.for_world(World(p)).walls[:6])
        wall=next(r for r in p["teamOur"]["roles"] if r["roleType"]=="wall" and tuple(r["pos"].values()) in front)
        wall["health"]=100
        response=agent.decide(p);check(p,response)
        self.assertEqual("WallUpgradeVoucher1",response["roleCommandMap"][str(worker["id"])]["name"])
        sim.step(response)
        for _ in range(25):
            sim.step(agent.decide(p))
            if wall["level"]==2:break
        self.assertEqual(2,wall["level"])
        self.assertEqual(1500,wall["health"])

    def test_http_transport_and_malformed_request_recovery(self):
        server=AgentServer(("127.0.0.1",0));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f"http://127.0.0.1:{server.server_port}/any-platform-path"
            for body in (b"{",json.dumps(initial()).encode()):
                req=Request(url,data=body,headers={"Content-Type":"application/json"})
                if body==b"{":
                    with self.assertLogs("agent.server",level="ERROR"):
                        with urlopen(req,timeout=5) as result:
                            response=json.load(result);self.assertEqual(200,result.status)
                else:
                    with urlopen(req,timeout=5) as result:
                        raw=result.read();self.assertEqual(len(raw),int(result.headers["Content-Length"]))
                        response=json.loads(raw);self.assertEqual(200,result.status)
                if body==b"{":self.assertEqual({},response["roleCommandMap"])
                else:check(initial(),response)
        finally:server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=="__main__":unittest.main()
