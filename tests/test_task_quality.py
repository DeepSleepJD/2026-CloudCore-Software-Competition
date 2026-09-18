"""v7: actual local HTTP pages/checker processes plus simulated judge turn routing."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

from agent.task_checks import describe, shape_error, query_observation
from agent.task_sandbox import query, check
from agent.strategy import Agent
from test_jd_v4 import scene
from task_fixtures import document_reply


@contextmanager
def api(pages, mode='offset'):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit
            values = parse_qs(urlsplit(self.path).query)
            calls.append(values)
            payload = pages(values) if callable(pages) else pages[min(len(calls)-1, len(pages)-1)]
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:%s/data' % server.server_port
    plan = {'url': url, 'records_path': 'data.records', 'id_field': 'id',
            'success': {'path': 'code', 'equals': 200},
            'pagination': {'mode': mode, 'param': mode, 'start': 0 if mode != 'page' else 1,
                           'total_path': 'data.pagination.total'},
            'aggregations': {'count': {'op': 'count'}}}
    try:
        yield plan, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def page(records, total):
    return {'code': 200, 'data': {'records': records, 'pagination': {'total': total}}}


def execute_helper(command):
    """Execute only our own generated query/check helper; no live model code."""
    argv = shlex.split(command)
    assert argv[:2] == ['python3', '-c'] and 'ns["run"]' in argv[2]
    result = subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return '[exitCode:0]\n' + result.stdout


class QueryTests(unittest.TestCase):
    def test_offset_pagination_deduplicates_and_advances_raw_offset(self):
        records = [{'id': i, 'amount': i, 'kind': 'a' if i % 2 else 'b'} for i in range(1, 5)]
        with api([page(records[:2], 4), page(records[1:3], 4), page(records[3:], 4)]) as (plan, calls):
            plan['aggregations'].update(total={'op': 'sum', 'field': 'amount'},
                                        kinds={'op': 'distinct', 'field': 'kind'},
                                        a_count={'op': 'count', 'where': {'kind': 'a'}})
            result = query(plan)
        self.assertEqual(result['answer'], {'count': 4, 'total': 10, 'kinds': ['a', 'b'], 'a_count': 2})
        self.assertEqual([c['offset'] for c in calls], [['0'], ['2'], ['4']])

    def test_page_and_cursor_pagination(self):
        with api([page([{'id': 1}], 2), page([{'id': 2}], 2)], 'page') as (plan, calls):
            self.assertEqual(query(plan)['answer'], {'count': 2})
            self.assertEqual([c['page'] for c in calls], [['1'], ['2']])
        values = [{'items': [{'id': 1}], 'next': 'b'}, {'items': [{'id': 2}], 'next': None}]
        with api(values, 'cursor') as (plan, calls):
            plan.pop('success')
            plan['records_path'] = 'items'
            plan['pagination'] = {'mode': 'cursor', 'param': 'cursor', 'start': '', 'next_path': 'next'}
            self.assertEqual(query(plan)['answer'], {'count': 2})
            self.assertEqual(calls[-1]['cursor'], ['b'])

    def test_auth_failure_is_not_empty_success(self):
        with api([{'code': 401, 'data': {'records': []}}]) as (plan, _):
            with self.assertRaisesRegex(ValueError, 'business success'):
                query(plan)

    def test_repeated_page_and_missing_records_and_changing_total_fail(self):
        cases = [([page([{'id': 1}], 2)] * 2, 'repeated page'),
                 ([{'code': 200, 'data': {}}], 'missing field'),
                 ([page([{'id': 1}], 2), page([{'id': 2}], 3)], 'changing total'),
                 ([page([{'id': 1}], 2), page([], 2)], 'no progress')]
        for pages, error in cases:
            with self.subTest(error=error), api(pages) as (plan, _):
                with self.assertRaisesRegex(ValueError, error):
                    query(plan)

    def test_empty_confirmed_dataset_is_valid(self):
        with api([page([], 0)]) as (plan, _):
            self.assertEqual(query(plan)['answer'], {'count': 0})

    def test_chronology_uses_explicit_order_not_alphabetical_minimum(self):
        with api([page([{'id': 1, 'era': '清'}, {'id': 2, 'era': '六朝'}], 2)]) as (plan, _):
            plan['aggregations'] = {'oldest': {'op': 'first_by', 'sort_field': 'era', 'field': 'era',
                                                'order': ['六朝', '明', '清']}}
            self.assertEqual(query(plan)['answer'], {'oldest': '六朝'})


class QualityFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.agent = Agent(self.temp.name)
        self.data = scene(10)
        self.data['teamOur']['roles'][3]['pos'] = {'x': 24, 'y': 14}

    def tearDown(self):
        self.temp.cleanup()

    def step(self, **changes):
        self.data.update(llmResp='', lastCmdResult='', errors=[], lastRoundRoleActionResults={})
        self.data.update(changes)
        output = self.agent.decide(self.data)
        self.data['roundNo'] += 1
        return output

    def begin(self, text):
        command = self.step(phaseTask='Read task_quality.md')['executeCmd']
        return self.step(lastCmdResult=document_reply(command, text))

    def query_task(self, url):
        return '查询接口 ' + url + '\n分页统计数据。\n## 答案格式\n```json\n{"count":0}\n```'

    def assert_answer(self, result, answer):
        cmd = result['roleCommandMap']['20011']
        self.assertEqual(cmd['action'], 'submitAnswer')
        self.assertEqual(json.loads(cmd['taskAnswer']), answer)

    def test_real_query_helper_runs_all_pages_and_submits_without_extra_llm(self):
        with api([page([{'id': 1}], 2), page([{'id': 2}], 2)]) as (plan, calls):
            prompt = self.begin(self.query_task(plan['url']))['prompt']
            self.assertIn('query', prompt)
            r = self.step(llmResp=json.dumps({'action': 'query', 'plan': plan}))
            self.assertTrue(r['executeCmd'])
            r = self.step(lastCmdResult=execute_helper(r['executeCmd']))
            self.assert_answer(r, {'count': 2})
            self.assertFalse(r['prompt'])
            self.assertEqual(len(calls), 2)

    def test_failed_query_cannot_submit_zero_and_can_recover(self):
        pages = [page([{'id': 1}], 2)] * 2
        with api(pages) as (plan, _):
            self.begin(self.query_task(plan['url']))
            r = self.step(llmResp=json.dumps({'action': 'query', 'plan': plan}))
            r = self.step(lastCmdResult=execute_helper(r['executeCmd']))
            self.assertIn('repeated page', r['prompt'])
            r = self.step(llmResp='{"action":"answer","answer":{"count":0}}')
            self.assertNotIn('20011', r['roleCommandMap'])
            pages[:] = [page([{'id': 1}, {'id': 2}], 2)]
            r = self.step(llmResp=json.dumps({'action': 'query', 'plan': plan}))
            self.assert_answer(self.step(lastCmdResult=execute_helper(r['executeCmd'])), {'count': 2})

    def test_partial_page_generic_output_does_not_pass_as_complete(self):
        self.begin(self.query_task('http://example.invalid/data'))
        self.step(llmResp='{"action":"command","command":"python3 collect.py"}')
        self.step(lastCmdResult='[exitCode:0]\n' + json.dumps(page([{'id': 1}], 10)))
        r = self.step(llmResp='{"action":"answer","answer":{"count":1}}')
        self.assertNotIn('20011', r['roleCommandMap'])
        self.assertIn('总量', r['prompt'])

    def test_format_gate_rejects_wrong_type_but_accepts_partial_fields(self):
        self.begin('## 答案格式\n```json\n{"count":0,"names":[]}\n```')
        r = self.step(llmResp='{"action":"answer","answer":{"count":"7"}}')
        self.assertNotIn('20011', r['roleCommandMap'])
        self.assert_answer(self.step(llmResp='{"action":"answer","answer":{"count":7}}'), {'count': 7})

    def test_wrong_answer_cannot_be_repeated_by_reordering_keys(self):
        self.begin('Return an object')
        self.step(llmResp='{"action":"answer","answer":{"x":1,"y":2}}')
        self.step(errors=[{'errorCode': 2, 'description': 'wrong'}])
        r = self.step(llmResp='{"action":"answer","answer":{"y":2,"x":1}}')
        self.assertNotIn('20011', r['roleCommandMap'])
        self.assertIn('已经被裁判判错', r['prompt'])
        self.assert_answer(self.step(llmResp='{"action":"answer","answer":{"x":3,"y":2}}'), {'x': 3, 'y': 2})

    def test_repeated_identical_failure_blocks_third_command_but_not_correction(self):
        self.begin('Inspect a file')
        for _ in range(2):
            self.assertTrue(self.step(llmResp='{"action":"command","command":"cat absent"}')['executeCmd'])
            self.step(lastCmdResult='[exitCode:1]\nNo such file')
        r = self.step(llmResp='{"action":"command","command":"cat absent"}')
        self.assertFalse(r['executeCmd'])
        self.assertIn('repeat_error', r['prompt'])
        self.assertTrue(self.step(llmResp='{"action":"command","command":"cat actual"}')['executeCmd'])

    def test_identical_command_with_new_output_is_progress(self):
        self.begin('Inspect changing data')
        for n in range(3):
            self.assertTrue(self.step(llmResp='{"action":"command","command":"cat data"}')['executeCmd'])
            self.step(lastCmdResult='[exitCode:0]\n' + str(n))

    def engineering_task(self):
        work = Path(self.temp.name).as_posix()
        return ('修复当前工程。工作目录 `cd "' + work + '"`。验收 `python3 verify.py` 输出TOKEN。\n'
                '## 答案格式\n```json\n{"token":"example"}\n```')

    def test_model_token_is_replaced_by_real_checker_token(self):
        Path(self.temp.name, 'verify.py').write_text('print("TOKEN: actual-fixture")\n')
        self.begin(self.engineering_task())
        r = self.step(llmResp='{"action":"answer","answer":{"token":"made-up"}}')
        self.assertNotIn('20011', r['roleCommandMap'])
        # The fixture uses the current interpreter so Windows/Linux both run it.
        self.assert_answer(self.step(lastCmdResult=self.run_checker(r['executeCmd'])), {'token': 'actual-fixture'})

    def run_checker(self, command):
        # Generated command embeds python3 for the competition. Make that fixed
        # trusted fixture executable on Windows without altering production code.
        import base64
        import re
        argv = shlex.split(command)
        encoded = re.findall(r"b64decode\('([A-Za-z0-9+/=]+)'\)", argv[2])[-1]
        payload = json.loads(base64.b64decode(encoded))
        self.assertEqual(payload['kind'], 'check')
        self.assertEqual(payload['plan']['argv'], ['python3', 'verify.py'])
        payload['plan']['argv'][0] = sys.executable
        replacement = base64.b64encode(json.dumps(payload).encode()).decode()
        return execute_helper('python3 -c ' + shlex.quote(argv[2].replace(encoded, replacement)))

    def test_repair_can_trigger_check_without_model_roundtrip(self):
        Path(self.temp.name, 'verify.py').write_text('print("TOKEN: checked")\n')
        self.begin(self.engineering_task())
        self.step(llmResp='{"action":"command","command":"repair_current_workspace","verify":true}')
        r = self.step(lastCmdResult='[exitCode:0]\nrepaired')
        self.assertTrue(r['executeCmd'])
        self.assertFalse(r['prompt'])
        self.assert_answer(self.step(lastCmdResult=self.run_checker(r['executeCmd'])), {'token': 'checked'})

    def test_failed_checker_never_submits_printed_token(self):
        Path(self.temp.name, 'verify.py').write_text('print("TOKEN: fake")\nraise SystemExit(1)\n')
        self.begin(self.engineering_task())
        r = self.step(llmResp='{"action":"answer","answer":{"token":"fake"}}')
        r = self.step(lastCmdResult=self.run_checker(r['executeCmd']))
        self.assertNotIn('20011', r['roleCommandMap'])
        self.assertIn('checker failed', r['prompt'])
        Path(self.temp.name, 'verify.py').write_text('print("FAIL: invalid configuration")\nprint("TOKEN: fake")\n')
        with self.assertRaisesRegex(ValueError, 'reported failure'):
            check({'argv': [sys.executable, 'verify.py'], 'cwd': self.temp.name})

    def test_tool_report_from_different_request_is_rejected(self):
        with api([page([], 0)]) as (plan, _):
            self.begin(self.query_task(plan['url']))
            self.step(llmResp=json.dumps({'action': 'query', 'plan': plan}))
            r = self.step(lastCmdResult='[exitCode:0]\n__ZK_TASK_RESULT__=' + json.dumps(
                {'request': 'stale', 'kind': 'query', 'ok': True, 'complete': True, 'answer': {'count': 0}}))
            self.assertNotIn('20011', r['roleCommandMap'])

    def test_unknown_task_and_ambiguous_examples_remain_generic(self):
        self.assertEqual(describe('API说明，不包含查询地址')['family'], 'unknown')
        doc = '## API返回示例\n```json\n{"data":[]}\n```'
        self.assertIsNone(describe(doc)['example'])
        self.assertIsNone(describe(self.engineering_task() + '\n另一目录 `cd different_workspace`')['checker'])
        self.begin(doc)
        self.assert_answer(self.step(llmResp='{"action":"answer","answer":{"custom":42}}'), {'custom': 42})

    def test_query_requires_grounded_url(self):
        self.begin(self.query_task('http://example.invalid/data'))
        r = self.step(llmResp=json.dumps({'action': 'query', 'plan': {'url': 'http://guessed.invalid/data'}}))
        self.assertFalse(r['executeCmd'])
        self.assertIn('query_error', r['prompt'])
        r = self.step(llmResp=json.dumps({'action': 'query', 'plan': {'url': ['malformed']}}))
        self.assertFalse(r['executeCmd'])
        self.assertIn('query_error', r['prompt'])


if __name__ == '__main__':
    unittest.main()
