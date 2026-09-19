"""Real HTTP offset/limit pages, current prompt only, and a second shorter task."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from agent.task_sop import learn_sop, solve_sop, validate_pages
from tests.test_task_sandbox import execute_fixture_command
from tests.test_tasks import TaskRollout


class SopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = []
        self.mode = ''
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                q = parse_qs(urlsplit(self.path).query)
                city = q.get('location', [''])[0]
                offset = int(q.get('offset', ['0'])[0])
                owner.requests.append((city, offset, self.headers.get('Authorization')))
                if self.headers.get('Authorization') != 'Bearer fixture-key':
                    self.send_data(401, {'status': 'error', 'message': 'Expected Authorization: Bearer <key>'})
                elif not city:
                    self.send_data(400, {'status': 'error', 'message': 'Missing required parameter: location'})
                elif owner.mode == 'business':
                    self.send_data(200, {'success': False, 'message': 'unavailable'})
                else:
                    total = 15 if city == '北京' else 12
                    actual = 0 if owner.mode == 'repeat' else offset
                    rows = [{'id': str(i), 'name': city + str(i), 'type': '类别' + str(i % 9),
                             'era': '旧石器时代' if i == 0 else '清',
                             'protected_level': '世界遗产' if i % 3 == 0 else '全国重点'}
                            for i in range(actual, min(actual + 10, total))]
                    if owner.mode == 'duplicate' and offset:
                        rows[0]['id'] = '0'
                    self.send_data(200, {'code': 200, 'data': {'records': rows, 'pagination': {
                        'total_count': total, 'offset': actual, 'limit': 10}}})

            def send_data(self, status, data):
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = 'http://127.0.0.1:%d/query' % self.server.server_port
        (self.root / 'API_DOCS.md').write_text(self.url + '\nX-API-Key: fixture-key', encoding='utf-8')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def start(self, sim, city, timeout):
        for t in sim.p['teamOur']['playerTasks']:
            t['timeoutRounds'] = timeout
        sim.reach('accept')
        state = sim.agent.memory['tasks']['active']
        accepted = state['accepted']
        sim.agent.memory['tasks']['discoveries'].setdefault(state['type'], {})['directory'] = self.root.as_posix()
        name = 'task_' + str(timeout) + '.md'
        (self.root / name).write_text('查询' + city + '全部文化遗产: city,total_count,world_heritage_count,types,oldest_era', encoding='utf-8')
        response = sim.step(False, phaseTask='Read ' + name)
        self.assertLess(len(response['executeCmd']), 16000)
        output = execute_fixture_command(response['executeCmd'])
        response = sim.step(False, lastCmdResult=output)
        context = json.loads(response['prompt'].split('\n', 1)[1])
        # Use only the public prompt, never read internal bootstrap to build commands.
        helper = Path(context['bootstrap']['httpHelper'])
        self.addCleanup(helper.parent.rmdir)
        self.addCleanup(helper.unlink)
        self.assertTrue(helper.is_file())
        return accepted, context

    def test_first_fifteen_then_ten_round_task_reuses_sop_and_direct_submits(self):
        sim = TaskRollout()
        sim.p['teamOur']['playerTasks'] = sim.p['teamOur']['playerTasks'][:1]
        accepted, context = self.start(sim, '北京', 15)
        response = sim.step(False, llmResp=json.dumps({'httpRequest': {
            'url': self.url + '?city=北京&page=1&size=100', 'apiKey': 'fixture-key'}}))
        response = sim.step(False, lastCmdResult=execute_fixture_command(response['executeCmd']))
        self.assertIn('Missing required parameter: location', response['prompt'])
        response = sim.step(False, llmResp=json.dumps({'httpRequest': {
            'url': self.url + '?location=北京&page=1&size=100', 'apiKey': 'fixture-key'}}))
        response = sim.step(False, lastCmdResult=execute_fixture_command(response['executeCmd']))
        context = json.loads(response['prompt'].split('\n', 1)[1])
        self.assertEqual('location', context['discoveries']['sop']['cityParam'])
        response = sim.step(False, llmResp=json.dumps({'runSop': {'city': '北京', 'apiKey': 'fixture-key'}}))
        response = sim.step(False, lastCmdResult=execute_fixture_command(response['executeCmd']))
        answer = json.loads(response['roleCommandMap'][sim.rid]['taskAnswer'])
        self.assertEqual(15, answer['total_count'])
        self.assertEqual(9, len(answer['types']))
        self.assertFalse(response['prompt'])
        self.assertEqual(8, sim.events[-1][0] - accepted)
        sim.step(False, phaseTask='', lastRoundRoleActionResults={sim.rid: True})
        self.assertEqual('completed_inferred', sim.agent.memory['tasks']['history'][-1]['reason'])
        sim.p['roundNo'] = 131
        for t in sim.p['teamOur']['playerTasks']:
            t.update(coldDownRounds=0, isValid=True)
        accepted, context = self.start(sim, '南京', 10)
        self.assertIn('sop', context['discoveries'])
        self.assertNotIn('北京', json.dumps(context['discoveries']['sop'], ensure_ascii=False))
        response = sim.step(False, llmResp=json.dumps({'runSop': {'city': '南京', 'apiKey': 'fixture-key'}}))
        response = sim.step(False, lastCmdResult=execute_fixture_command(response['executeCmd']))
        answer = json.loads(response['roleCommandMap'][sim.rid]['taskAnswer'])
        self.assertEqual(12, answer['total_count'])
        self.assertEqual('南京', answer['city'])
        self.assertEqual(4, sim.events[-1][0] - accepted)
        self.assertFalse(response['prompt'])
        sim.step(False, phaseTask='', lastRoundRoleActionResults={sim.rid: True})
        self.assertEqual('completed_inferred', sim.agent.memory['tasks']['history'][-1]['reason'])

    def helper_result(self):
        # Real HTTP helper with a fresh per-command deadline.
        import runpy
        helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'agent/sandbox_http.py'))
        request = helper['request_json']
        first = request(self.url + '?location=北京', 'fixture-key')
        return request, learn_sop(first)

    def test_repeat_duplicate_and_business_failure_never_complete(self):
        for mode in ('repeat', 'duplicate', 'business'):
            with self.subTest(mode=mode):
                self.mode = ''
                request, config = self.helper_result()
                self.mode = mode
                with self.assertRaises(ValueError):
                    solve_sop(config, '北京', 'fixture-key', request)

    def test_missing_pages_wrong_city_and_forged_counts_rejected(self):
        request, config = self.helper_result()
        result = solve_sop(config, '北京', 'fixture-key', request)
        for pages, city in ((result['pages'][:1], '北京'), (result['pages'], '南京')):
            with self.assertRaises(ValueError):
                validate_pages(config, city, pages)
        answer, evidence = validate_pages(config, '北京', result['pages'])
        self.assertEqual(15, evidence['uniqueCount'])
        self.assertEqual(5, answer['world_heritage_count'])
        from tests.test_task_integrity import IntegrityTests
        obj = IntegrityTests().scheduler()
        obj.m['active']['sopRun'] = {'config': config, 'city': '北京'}
        bad = copy.deepcopy(result)
        bad['answer']['total_count'] = 999
        obj.observe_output('[exitCode:0]\n' + json.dumps(bad))
        self.assertFalse(obj.answer_evidence(bad['answer'])[0])

    def test_previous_city_mentioned_in_task_is_not_current_city(self):
        sim = TaskRollout()
        _, context = self.start(sim, '南京', 10)
        state = sim.agent.memory['tasks']['active']
        state['bootstrap']['files'][0]['text'] += '，API与北京题相同。'
        _, config = self.helper_result()
        sim.agent.memory['tasks']['discoveries'][state['type']]['sop'] = config
        response = sim.step(False, llmResp=json.dumps({'runSop': {'city': '北京', 'apiKey': 'fixture-key'}}))
        self.assertFalse(response['executeCmd'])
        self.assertIn('城市或统计类型未在本题任务文件中确认', response['prompt'])

    def test_sop_with_three_rounds_remaining_can_submit_and_receive_feedback(self):
        sim = TaskRollout()
        self.start(sim, '北京', 15)
        state = sim.agent.memory['tasks']['active']
        _, config = self.helper_result()
        sim.agent.memory['tasks']['discoveries'][state['type']]['sop'] = config
        state['deadline'] = sim.p['roundNo'] + 3
        response = sim.step(False, llmResp=json.dumps({'runSop': {'city': '北京', 'apiKey': 'fixture-key'}}))
        self.assertTrue(response['executeCmd'])
        response = sim.step(False, lastCmdResult=execute_fixture_command(response['executeCmd']))
        self.assertEqual('submitAnswer', response['roleCommandMap'][sim.rid]['action'])
        sim.step(False, phaseTask='', lastRoundRoleActionResults={sim.rid: True})
        self.assertEqual('completed_inferred', sim.agent.memory['tasks']['history'][-1]['reason'])


if __name__ == '__main__':
    unittest.main()
