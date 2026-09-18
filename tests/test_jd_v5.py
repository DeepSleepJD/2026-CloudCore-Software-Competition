"""Multi-turn regressions from the replay audit, without hardcoded replay answers."""
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path

from test_jd_v4 import scene, robot
from test_agent import unit
from agent.strategy import Agent, Planner
from agent.model import World
from agent.state import Memory


class DeliveryAndTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent = Agent(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_repair_600_hp_wall_is_used_not_alternating_two_positions(self):
        d = scene(90)
        worker = d['teamOur']['roles'][1]
        worker.update(pos={'x':29,'y':12}, backpack=['WallFixer'])
        wall = unit(41000, 'wall', (28,9), hp=600)
        d['teamOur']['roles'].append(wall)
        d['robot']['roles'] = [robot(point=(27,9))]
        used = False
        for n in range(90,96):
            d['roundNo'] = n
            cmd = self.agent.decide(d)['roleCommandMap'].get('20010', {})
            if cmd.get('action') == 'move':
                worker['pos'] = cmd['targetPos'][0]
            elif cmd.get('action') == 'use':
                self.assertEqual(cmd['name'], 'WallFixer')
                self.assertEqual(cmd['targetPos'], [wall['pos']])
                used = True
                break
        self.assertTrue(used, 'courier must finish delivery, not just issue moves')

    def test_adjacent_base_voucher_used_before_fatal_damage(self):
        d = scene(1284)
        d['teamOur']['roles'][0].update(level=2, health=275)
        d['teamOur']['roles'][1].update(pos={'x':30,'y':11}, backpack=['StationUpgradeVoucher2'])
        d['robot']['roles'] = [robot(point=(27,9))]
        c = self.agent.decide(d)['roleCommandMap']['20010']
        self.assertEqual((c['action'], c['name']), ('use','StationUpgradeVoucher2'))

    def test_night_safe_task_while_foreign_robots_are_far_away(self):
        d = scene(110)
        d['teamOur']['roles'][3]['pos'] = {'x':24,'y':14}
        d['teamOur']['playerTasks'] = [{'taskType':'自进化类1','isValid':True,'timeoutRounds':30}]
        d['robot']['roles'] = [robot(point=(5,25), team='challenger')]
        self.assertEqual(self.agent.decide(d)['roleCommandMap']['20011']['action'], 'acceptTask')

    def test_task_escape_when_pioneer_is_threatened(self):
        d = scene(90)
        d['teamOur']['roles'][3]['pos'] = {'x':24,'y':14}
        d['phaseTask'] = 'Read task_current.md'
        d['robot']['roles'] = [robot(point=(22,14))]
        r = self.agent.decide(d)
        self.assertEqual(r['roleCommandMap']['20011']['action'], 'move')
        self.assertFalse(r['executeCmd'])

    def test_safe_night_mining_continues_with_active_wave_elsewhere(self):
        d = scene(90)
        d['teamOur']['roles'][2]['pos'] = {'x':36,'y':14}
        d['robot']['roles'] = [robot(point=(20,8))]
        c = self.agent.decide(d)['roleCommandMap']['20012']
        self.assertEqual(c['action'], 'collect')
        self.assertEqual(c['targetPos'], [{'x':37,'y':14}])

    def test_dusk_miner_leaves_distant_mine_before_spawn(self):
        d = scene(68)
        d['teamOur']['roles'][2]['pos'] = {'x':36,'y':14}
        c = self.agent.decide(d)['roleCommandMap']['20012']
        self.assertEqual(c['action'], 'move')

    def test_economy_worker_collects_copper_while_builder_has_missing_walls(self):
        d = scene(20)
        d['teamOur']['roles'][2]['pos'] = {'x':36,'y':14}
        c = self.agent.decide(d)['roleCommandMap']['20012']
        self.assertEqual((c['action'], c['targetPos']), ('collect',[{'x':37,'y':14}]))

    def test_task_bootstrap_then_model_and_answer_for_two_task_families(self):
        for filename, answer in [('task_8_unknown_city.md', {'count': 7}), ('task_8_new_token.md', {'token':'fixture-token-unique'})]:
            with self.subTest(filename=filename):
                a = Agent(self.temp.name)
                d = scene(10)
                d['phaseTask'] = '请阅读' + filename + '，获取任务信息'
                d['teamOur']['roles'][3]['pos'] = {'x':24,'y':14}
                r = a.decide(d)
                self.assertEqual('cat -- ' + filename, r['executeCmd'])
                self.assertFalse(r['prompt'])
                d.update(roundNo=11, lastCmdResult='[exitCode:0]\nReturn exactly ' + json.dumps(answer))
                self.assertIn('Return exactly', a.decide(d)['prompt'])
                d.update(roundNo=12, llmResp=json.dumps({'action':'answer','answer':answer}))
                self.assertEqual(json.loads(a.decide(d)['roleCommandMap']['20011']['taskAnswer']), answer)

    def test_invalid_model_response_retries_with_original_task_file(self):
        d = scene(10)
        d['phaseTask'] = 'Read task_example.md'
        self.agent.decide(d)
        d.update(roundNo=11, lastCmdResult='[exitCode:0]\nRequired fields are city and count')
        self.agent.decide(d)
        d.update(roundNo=12, lastCmdResult='', llmResp='not JSON')
        r = self.agent.decide(d)
        self.assertIn('Required fields are city and count', r['prompt'])
        self.assertIn('format_error', r['prompt'])
        trace = next(Path(self.temp.name).glob('*_trace.jsonl'))
        entries = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(entries[-1]['llm_response'], 'not JSON')

    def test_investment_uses_spare_cash_for_endangered_base(self):
        d = scene(20,245)
        d['teamOur']['roles'][0].update(level=2,health=870)
        d['teamOur']['roles'][3]['pos'] = {'x':24,'y':20}
        c = self.agent.decide(d)['roleCommandMap']['20011']
        self.assertEqual((c['action'],c['name']),('buy','StationUpgradeVoucher2'))
        self.assertEqual(self.agent.memory.deliveries[20011], ('StationUpgradeVoucher2',20013))

    def test_assigned_wall_voucher_is_delivered_before_unrelated_gun_voucher(self):
        d = scene(20)
        d['teamOur']['roles'][1].update(pos={'x':29,'y':9},backpack=['WallUpgradeVoucher1','WeaponUpgradeVoucher2'])
        d['teamOur']['roles'].append(unit(41000,'wall',(28,9),hp=900))
        w = World(d); m = Memory(); m.deliveries[20010] = ('WallUpgradeVoucher1',41000)
        p = Planner(w,{},m)
        self.assertTrue(p.use_supplies(w.workers[0]))
        self.assertEqual(p.commands['20010']['name'], 'WallUpgradeVoucher1')

    def test_active_wave_does_not_switch_to_worker_before_gunner_handover(self):
        d = scene(71)
        d['teamOur']['playerTasks'] = [{'isValid':True,'taskType':'自进化类1'}]
        d['teamOur']['roles'][1]['pos'] = {'x':29,'y':13}
        d['robot']['roles'] = [robot(point=(20,8))]
        r = self.agent.decide(d)
        self.assertTrue(any(c.get('controllerId') == '20011' for c in r['roleCommandMap'].values()))


if __name__ == '__main__':
    unittest.main()
