"""Real HTTP failures and turn-by-turn correction, with fixed model fixtures."""
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import tempfile
from threading import Thread
import unittest
from urllib.parse import parse_qs, urlsplit

from agent.strategy import Agent
from agent.task_sandbox import run
from task_fixtures import document_reply
from test_jd_v4 import scene
from test_task_quality import execute_helper, page


@contextmanager
def service(respond):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            args = parse_qs(urlsplit(self.path).query)
            calls.append(args)
            status, data, headers = respond(args, self.headers)
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    plan = {'url': 'http://127.0.0.1:%s/data' % server.server_port,
            'records_path': 'data.records', 'id_field': 'id',
            'success': {'path': 'code', 'equals': 200},
            'pagination': {'mode': 'offset', 'param': 'offset', 'start': 0,
                           'total_path': 'data.pagination.total'},
            'aggregations': {'count': {'op': 'count'}}}
    try:
        yield plan, calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def report(plan):
    output = StringIO()
    with redirect_stdout(output):
        run({'kind': 'query', 'request': 'fixture', 'plan': plan})
    return json.loads(output.getvalue().split('=', 1)[1])


class QueryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent = Agent(self.temp.name)
        self.data = scene(9)
        self.data['teamOur']['roles'][3]['pos'] = {'x': 24, 'y': 14}
        self.data['teamOur']['playerTasks'] = [
            {'taskType': '自进化类1', 'isValid': True, 'timeoutRounds': 15}]

    def tearDown(self):
        self.temp.cleanup()

    def step(self, **changes):
        self.data.update(llmResp='', lastCmdResult='', errors=[], lastRoundRoleActionResults={})
        self.data.update(changes)
        result = self.agent.decide(self.data)
        self.data['roundNo'] += 1
        return result

    def begin(self, url, fields=None):
        self.assertEqual(self.step()['roleCommandMap']['20011']['action'], 'acceptTask')
        cmd = self.step(phaseTask='Read task_recovery.md')['executeCmd']
        body = ('查询接口 ' + url + '\n凭据值为 fixture-key，旧文档可能过时。\n'
                '## 提交形式\n```json\n' + json.dumps(fields or {'count': 0}) + '\n```')
        return self.step(lastCmdResult=document_reply(cmd, body))

    def execute(self, plan):
        result = self.step(llmResp=json.dumps({'action': 'query', 'plan': plan}))
        return self.step(lastCmdResult=execute_helper(result['executeCmd']))

    def test_http_status_body_and_authentication_header_survive(self):
        with service(lambda *_: (401, {'message': 'Use Bearer with the current task key'},
                                  {'WWW-Authenticate': 'Bearer realm="fixture"'})) as (plan, _):
            result = report(plan)
        self.assertFalse(result['ok'])
        self.assertNotIn('answer', result)
        self.assertEqual(result['diagnostics']['http_status'], 401)
        self.assertIn('Use Bearer', result['diagnostics']['response_preview'])
        self.assertIn('Bearer', result['diagnostics']['authentication_hint'])

    def test_business_error_preserves_parameter_correction(self):
        with service(lambda *_: (200, {'code': 400, 'message': 'Missing parameter location'}, {})) as (plan, _):
            result = report(plan)
        self.assertFalse(result['ok'])
        self.assertEqual(result['diagnostics']['stage'], 'business_status')
        self.assertIn('location', result['diagnostics']['response_preview'])

    def test_non_json_error_excerpt_is_bounded(self):
        with service(lambda *_: (400, b'Missing location: ' + b'x' * 20000, {})) as (plan, _):
            result = report(plan)
        self.assertIn('Missing location', result['diagnostics']['response_preview'])
        self.assertLessEqual(len(result['diagnostics']['response_preview']), 3000)
        self.assertLess(len(json.dumps(result)), 10000)

    def test_changed_record_path_exposes_actual_structure(self):
        with service(lambda *_: (200, {'code': 200, 'data': {'items': [{'id': 1}], 'total': 1}}, {})) as (plan, _):
            result = report(plan)
        self.assertFalse(result['ok'])
        self.assertEqual(result['diagnostics']['stage'], 'records')
        self.assertIn('items', result['diagnostics']['response_preview'])

    def test_later_page_failure_never_returns_partial_statistics(self):
        def respond(args, _):
            return (200, page([{'id': 1}], 2), {}) if args['offset'] == ['0'] else (400, {'message': 'bad cursor'}, {})
        with service(respond) as (plan, _):
            result = report(plan)
        self.assertFalse(result['ok'])
        self.assertFalse(result['complete'])
        self.assertNotIn('answer', result)
        self.assertEqual(result['diagnostics']['page'], 2)
        self.assertIn('bad cursor', result['diagnostics']['response_preview'])

    def test_one_failed_field_preserves_other_results_and_real_null_sample(self):
        with service(lambda *_: (200, page([{'id': 1, 'name': 'fixture', 'rank': None}], 1), {})) as (plan, _):
            plan['aggregations']['oldest'] = {'op': 'first_by', 'field': 'name', 'sort_field': 'rank'}
            result = report(plan)
        self.assertTrue(result['ok'])
        self.assertTrue(result['complete'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['answer'], {'count': 1})
        self.assertIn('oldest', result['field_errors'])
        self.assertIn('null', result['diagnostics']['response_preview'])

    def test_nonfinite_field_does_not_discard_valid_count(self):
        with service(lambda *_: (200, page([{'id': 1, 'amount': float('nan')}], 1), {})) as (plan, _):
            plan['aggregations']['sum'] = {'op': 'sum', 'field': 'amount'}
            result = report(plan)
        self.assertTrue(result['ok'])
        self.assertEqual(result['answer'], {'count': 1})
        self.assertIn('sum', result['field_errors'])

    def test_auth_then_parameter_then_pages_then_submit_within_deadline(self):
        def respond(args, headers):
            if headers.get('Authorization') != 'Bearer fixture-key':
                return 401, {'message': 'Use Authorization: Bearer with the current task key'}, {'WWW-Authenticate': 'Bearer'}
            if args.get('location') != ['fixture-city']:
                return 400, {'message': 'Missing required parameter location'}, {}
            start = int(args['offset'][0])
            return 200, page([{'id': start + 1}], 2), {}
        with service(respond) as (plan, calls):
            self.begin(plan['url'])
            response = self.execute(plan)
            self.assertIn('Use Authorization: Bearer', response['prompt'])
            self.assertNotIn('20011', response['roleCommandMap'])
            plan['headers'] = {'Authorization': 'Bearer fixture-key'}
            response = self.execute(plan)
            self.assertIn('Missing required parameter location', response['prompt'])
            self.assertNotIn('20011', response['roleCommandMap'])
            plan['params'] = {'location': 'fixture-city'}
            response = self.execute(plan)
            submitted = response['roleCommandMap']['20011']
            self.assertEqual(submitted['action'], 'submitAnswer')
            self.assertEqual(json.loads(submitted['taskAnswer']), {'count': 2})
            self.assertLess(self.data['roundNo'] - 9, 15)
            self.assertEqual(len(calls), 4)

    def test_partial_answer_is_submitted_then_failed_field_can_be_corrected(self):
        rows = [{'id': 1, 'name': 'A', 'rank': None, 'year': 20},
                {'id': 2, 'name': 'B', 'rank': None, 'year': 10}]
        with service(lambda *_: (200, page(rows, 2), {})) as (plan, _):
            self.begin(plan['url'], {'count': 0, 'oldest': 'name'})
            plan['aggregations']['oldest'] = {'op': 'first_by', 'field': 'name', 'sort_field': 'rank'}
            response = self.execute(plan)
            self.assertEqual(json.loads(response['roleCommandMap']['20011']['taskAnswer']), {'count': 2})
            response = self.step(errors=[{'errorCode': 2, 'description': 'oldest missing'}])
            payload = json.loads(response['prompt'].splitlines()[-1])
            self.assertEqual(payload['confirmed_fields'], {'count': 2})
            self.assertIn('oldest', payload['field_errors'])
            plan['aggregations']['oldest']['sort_field'] = 'year'
            response = self.execute(plan)
            self.assertEqual(json.loads(response['roleCommandMap']['20011']['taskAnswer']), {'count': 2, 'oldest': 'B'})

    def test_all_fields_failed_do_not_submit_empty_object_and_new_task_resets_feedback(self):
        with service(lambda *_: (200, page([{'id': 1}], 1), {})) as (plan, _):
            self.begin(plan['url'])
            plan['aggregations'] = {'count': {'op': 'sum', 'field': 'missing'}}
            response = self.execute(plan)
            self.assertNotIn('20011', response['roleCommandMap'])
            self.assertIn('field_errors', response['prompt'])
            self.step(phaseTask='')
            runner = self.agent.missions.runner
            self.assertEqual(runner.query_feedback, {})
            self.assertEqual(runner.confirmed_fields, {})
            self.assertEqual(runner.field_errors, {})


if __name__ == '__main__':
    unittest.main()
