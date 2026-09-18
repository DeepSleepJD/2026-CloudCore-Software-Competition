"""Observed-match regressions and changed-parameter end-to-end task completion.

Model responses are controlled fixtures. Only our helper and reviewed test programs
execute locally; recorded model shell commands are never executed.
"""
import base64
import copy
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest

from agent.task_checks import grounded_url
from agent.task_evidence import PageEvidence, ProtocolMemory
from agent.task_sandbox import check, command_check, document
from agent.tasks import TaskRunner
from task_fixtures import helper_payload
from test_task_environment import run_owned_helper
from test_query_recovery import service

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/task_match_20260918.json').read_text(encoding='utf-8'))


class Flow:
    def __init__(self, directory):
        self.runner = TaskRunner(SimpleNamespace(state_dir=directory), 'changed-parameter-match')
        self.world = SimpleNamespace(round=0, data={}, pioneer={'id': 7, 'pos': (1, 1)}, put=self.put)
        self.submissions = []

    def put(self, _, command):
        self.submissions.append((self.world.round, json.loads(command['taskAnswer'])))
        return True

    def step(self, **changes):
        self.world.data.update(llmResp='', lastCmdResult='', errors=[], lastRoundRoleActionResults={})
        self.world.data.update(changes)
        value = self.runner.step(self.world)
        self.world.round += 1
        return value

    def start(self, task, accepted=10, timeout=10):
        self.runner.accepted(accepted, timeout)
        self.world.round = accepted + 1
        return self.step(phaseTask='Read ' + task)


def document_result(command, docs):
    payload = helper_payload(command)
    return '[exitCode:0]\n__ZK_TASK_RESULT__=' + json.dumps({
        'kind': 'document', 'request': payload['request'], 'ok': True, 'complete': True,
        'documents': [{'path': '/sandbox/' + d['name'], 'text': d['text'], 'truncated': False} for d in docs]})


def command_for(url, location='alpha', offset=0):
    return 'curl -s -H "Authorization: Bearer fixture" "%s?location=%s&offset=%s&limit=10"' % (url, location, offset)


class RecordedMatchTests(unittest.TestCase):
    def test_beijing_two_observed_pages_submit_at_round_35(self):
        with tempfile.TemporaryDirectory() as temp:
            flow = Flow(temp)
            result = flow.start('task_1_beijing.md', accepted=24, timeout=15)
            flow.step(lastCmdResult=document_result(result['executeCmd'], FIXTURE['tasks'][1]['documents']))
            url = 'http://localhost:8899/api/v1/heritage/search'
            # The actual two authentication/parameter corrections consumed four rounds.
            for code in (401, 400):
                flow.step(llmResp=json.dumps({'action': 'command', 'command': command_for(url)}))
                flow.step(lastCmdResult='[exitCode:0]\n' + json.dumps({'code': code, 'message': 'fixture rejection'}))
            for offset, page in zip((0, 10), FIXTURE['beijing_pages']):
                flow.step(llmResp=json.dumps({'action': 'command', 'command': command_for(url, '北京', offset)}))
                flow.step(lastCmdResult='[exitCode:0]\n' + json.dumps(page))
            result = flow.step(llmResp=json.dumps({'action': 'answer', 'answer': FIXTURE['beijing_answer']}))
            self.assertTrue(result.get('answered'))
            self.assertEqual(flow.submissions, [(35, FIXTURE['beijing_answer'])])

    def test_document_url_query_can_be_split_without_allowing_other_endpoint(self):
        for origin, path in [('http://localhost:8899', '/api/v1/heritage/search'),
                             ('https://example.invalid:8443', '/changed/report')]:
            text = origin + path + '?region=alpha'
            self.assertTrue(grounded_url(origin + path, text))
            self.assertTrue(grounded_url(origin + path + '?region=beta', text))
            self.assertFalse(grounded_url(origin + path + '/invented', text))
            self.assertFalse(grounded_url('https://different.invalid' + path, text))

    def test_page_groups_reject_gaps_overlap_changed_query_auth_and_total(self):
        a, b = FIXTURE['beijing_pages']
        output = lambda value: '[exitCode:0]\n' + json.dumps(value)
        for case in ('missing', 'overlap', 'location', 'auth', 'total', 'truncated'):
            with self.subTest(case=case):
                tracker = PageEvidence()
                self.assertFalse(tracker.observe(command_for('http://fixture/data'), output(a)))
                second = copy.deepcopy(b)
                cmd = command_for('http://fixture/data', offset=10)
                if case == 'missing':
                    second['data']['pagination']['offset'] = 11
                    second['data']['records'].pop()
                    cmd = command_for('http://fixture/data', offset=11)
                elif case == 'overlap':
                    second['data']['records'][0]['id'] = a['data']['records'][0]['id']
                elif case == 'location':
                    cmd = command_for('http://fixture/data', 'beta', 10)
                elif case == 'auth':
                    cmd = cmd.replace('Bearer fixture', 'Bearer other')
                elif case == 'total':
                    second['data']['pagination']['total_count'] = 16
                reply = output(second) + ('\n[TRUNCATED]' if case == 'truncated' else '')
                self.assertFalse(tracker.observe(cmd, reply))

    def test_verified_protocol_survives_new_task_without_old_business_values(self):
        memory = ProtocolMemory()
        url = 'http://fixture/changed/path'
        memory.remember(command_for(url, 'old-region'), '[exitCode:0]\n' + json.dumps(FIXTURE['beijing_pages'][0]), '/one')
        relevant = memory.relevant('API http://fixture', '/one')
        self.assertEqual(relevant[0]['url'], url)
        self.assertIn('location', relevant[0]['parameter_names'])
        self.assertNotIn('old-region', json.dumps(relevant))
        self.assertNotIn('故宫', json.dumps(relevant, ensure_ascii=False))
        self.assertEqual(memory.relevant('API http://different', '/one'), [])
        self.assertEqual(memory.relevant('API http://fixture', '/two'), [])
        memory.remember(command_for(url), '[exitCode:0]\n{"code":401}', '/one')
        self.assertEqual(memory.relevant('API http://fixture', '/one'), [])

    def test_listing_cannot_clear_known_query_error(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Flow(temp)
            cmd = f.start('task_2_nanjing.md')['executeCmd']
            f.step(lastCmdResult=document_result(cmd, FIXTURE['tasks'][2]['documents']))
            f.step(llmResp=json.dumps({'action': 'command', 'command': 'curl -s http://fixture/wrong'}))
            f.step(lastCmdResult='[exitCode:0]\n{"code":404}')
            f.step(llmResp=json.dumps({'action': 'command', 'command': 'ls'}))
            f.step(lastCmdResult='[exitCode:0]\nreference.md')
            self.assertTrue(f.runner.query_issue)
            self.assertFalse(f.step(llmResp='{"action":"answer","answer":{"city":"南京"}}').get('answered'))


class VariableTaskTests(unittest.TestCase):
    def test_actual_followup_books_find_reference_without_naming_it(self):
        for index in (2, 4):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                task = FIXTURE['tasks'][index]['documents'][0]
                taskfile = Path(temp) / task['name']
                taskfile.write_text(task['text'], encoding='utf-8')
                reference = FIXTURE['tasks'][1]['documents'][1]['text']
                Path(temp, 'renamed_service_reference.md').write_text(reference, encoding='utf-8')
                result = document({'path': taskfile.as_posix()})
                self.assertEqual(len(result['documents']), 2)
                self.assertIn('/api/v1/heritage/search', result['documents'][1]['text'])

    def test_short_followups_discover_renamed_docs_and_bind_new_parameters(self):
        # Same algorithm, changed endpoint, credential, business parameter, city,
        # dataset, answer fields, document name and task directory.
        for variant in range(3):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp:
                location = 'region-' + str(variant)
                parameter = 'area_' + str(variant)
                credential = 'fresh-' + str(variant)
                rows = [{'id': n, 'amount': n + variant} for n in range(variant + 2)]
                def respond(args, headers):
                    if headers.get('Authorization') != 'Bearer ' + credential:
                        return 401, {'message': 'Use the current credential'}, {}
                    if args.get(parameter) != [location]:
                        return 400, {'message': 'Missing required parameter ' + parameter}, {}
                    offset = int(args.get('offset', ['0'])[0])
                    return 200, {'code': 200, 'data': {'records': rows[offset:offset+1],
                                                     'pagination': {'total': len(rows)}}}, {}
                with service(respond) as (plan, calls):
                    root = Path(temp)
                    task = root / ('case_%s.md' % variant)
                    task.write_text('查询统计，API与前题相同，服务 ' + plan['url'].rsplit('/', 1)[0] +
                        '\n## 提交形式\n```json\n' + json.dumps({'region': location, 'items': 0, 'sum': 0}) + '\n```', encoding='utf-8')
                    (root / ('reference_%s.md' % variant)).write_text('API 认证说明 curl ' + plan['url'] + '?' + parameter + '=' + location,
                                                                 encoding='utf-8')
                    flow = Flow(str(root / 'state'))
                    read = flow.start(task.name)['executeCmd']
                    prompt = flow.step(lastCmdResult=run_owned_helper(read, temp))['prompt']
                    self.assertIn(plan['url'] + '?' + parameter, prompt)
                    plan['headers'] = {'Authorization': 'Bearer ' + credential}
                    plan['params'] = {parameter: location}
                    plan['aggregations'] = {'region': {'op': 'constant', 'value': location},
                                            'items': {'op': 'count'}, 'sum': {'op': 'sum', 'field': 'amount'}}
                    cmd = flow.step(llmResp=json.dumps({'action': 'query', 'plan': plan}))['executeCmd']
                    self.assertTrue(flow.step(lastCmdResult=run_owned_helper(cmd, temp)).get('answered'))
                    round_no, answer = flow.submissions[-1]
                    self.assertEqual(answer, {'region': location, 'items': len(rows), 'sum': sum(r['amount'] for r in rows)})
                    self.assertEqual(round_no - 10, 4)
                    self.assertEqual(len(calls), len(rows))
                    flow.step(phaseTask='', lastRoundRoleActionResults={'7': True})
                    read = flow.start(task.name, accepted=30)['executeCmd']
                    prompt = flow.step(lastCmdResult=run_owned_helper(read, temp))['prompt']
                    known = json.loads(prompt.splitlines()[-1])['verified_interfaces']
                    self.assertEqual(known[0]['parameter_names'], [parameter])
                    self.assertNotIn(location, json.dumps(known))

    def test_three_recorded_repair_tasks_read_current_spec_and_finish_in_four_rounds(self):
        for index in (0, 3, 5):
            task = FIXTURE['tasks'][index]
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spec = task['spec']
                folder = re.search(r'logs/[^/]+/', spec).group()
                config = re.search(r'config/[^\s]+\.conf', spec).group()
                edits = {int(n): text for n, text in re.findall(r'第 (\d+) 行：`([^`]+)`', spec)}
                text = task['documents'][0]['text'].replace('${TASK_ROOT}', root.resolve().as_posix())
                spec_name = 'current_requirements_%s.md' % index
                text = text.replace('spec.md', spec_name)
                workspace = Path(re.search(r'`cd ([^`]+)`', text).group(1))
                workspace.mkdir()
                (workspace / spec_name).write_bytes(spec.encode('utf-8'))
                (workspace / 'config').mkdir()
                (workspace / config).write_text('header\nlog_level info\nwrong port\nmax_connections 100\ntimeout 30\nwrong name\n')
                (workspace / 'bin').mkdir()
                (workspace / 'bin/start.sh').write_text('fixture')
                # A separately written verifier checks task-specific values. The
                # returned token differs across cases, and is never in the prompt.
                token = 'verified-' + str(index)
                checker = ('from pathlib import Path\nimport os\n'
                           'lines=Path(' + repr(config) + ').read_text().splitlines()\n'
                           'assert Path(' + repr(folder) + ').is_dir()\n' +
                           ''.join('assert lines[%d]==%r\n' % (n-1, value) for n, value in edits.items()) +
                           'assert lines[3]=="max_connections 100" and lines[4]=="timeout 30"\n'
                           'assert os.name=="nt" or Path(' + repr(folder) + ').stat().st_mode & 0o777 == 0o755\n'
                           'assert os.name=="nt" or Path("bin/start.sh").stat().st_mode & 0o777 == 0o755\n' +
                           'print("TOKEN: ' + token + '")\n')
                (workspace / 'verify.py').write_text(checker, encoding='utf-8')
                text = text.replace('./check', 'python3 verify.py')
                taskfile = root / task['documents'][0]['name']
                taskfile.write_text(text, encoding='utf-8')
                flow = Flow(str(root / 'state'))
                read = flow.start(taskfile.name, timeout=task['timeout'])['executeCmd']
                prompt = flow.step(lastCmdResult=run_owned_helper(read, temp))['prompt']
                self.assertIn(spec.replace('\r\n', '\n').strip(), json.loads(prompt.splitlines()[-1])['task_file'])
                # Reviewed model fixture derives all changes from this spec.
                repair = ('from pathlib import Path; p=Path(' + repr(config) + '); '
                          'lines=p.read_text().splitlines(); ' +
                          '; '.join('lines[%d]=%r' % (n-1, value) for n, value in edits.items()) +
                          '; p.write_text(chr(10).join(lines)+chr(10)); Path(' + repr(folder) + ').mkdir(parents=True,exist_ok=True); '
                          'Path(' + repr(folder) + ').chmod(0o755); Path("bin/start.sh").chmod(0o755)')
                # Double-quoted executable and base64 program are portable across test OSes.
                encoded = base64.b64encode(repair.encode()).decode()
                cmd = '"' + sys.executable + '" -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
                output = flow.step(llmResp=json.dumps({'action': 'command', 'command': cmd,
                                                        'workspace': workspace.as_posix(), 'verify': True}))
                # Competition uses python3; use the test interpreter for this known fixture.
                payload = helper_payload(output['executeCmd'])
                self.assertEqual(payload['kind'], 'command_check')
                payload['plan']['checker']['argv'][0] = sys.executable
                from agent.task_sandbox import run
                from contextlib import redirect_stdout
                from io import StringIO
                stream = StringIO()
                with redirect_stdout(stream): run(payload)
                self.assertTrue(json.loads(stream.getvalue().split('=', 1)[1])['ok'], stream.getvalue())
                self.assertTrue(flow.step(lastCmdResult='[exitCode:0]\n' + stream.getvalue()).get('answered'))
                self.assertEqual(flow.submissions, [(14, {'token': token})])
                self.assertEqual((workspace / 'verify.py').read_text(encoding='utf-8'), checker)

    @unittest.skipUnless(os.path.exists('/bin/sh'), 'POSIX checker is verified in Linux CI')
    def test_crlf_checker_runs_without_changing_verifier_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'check'
            raw = b'#!/bin/sh\r\n[ -f repaired ] || exit 1\r\nprintf "TOKEN: runtime-value\\n"\r\n'
            p.write_bytes(raw)
            p.chmod(0o755)
            with self.assertRaisesRegex(ValueError, 'checker failed'):
                check({'argv': ['./check'], 'cwd': temp})
            result = command_check({'command': 'touch repaired', 'cwd': temp,
                                    'checker': {'argv': ['./check'], 'cwd': temp}})
            self.assertEqual(result['answer'], {'token': 'runtime-value'})
            self.assertEqual(result['checker_adapter'], 'crlf_in_memory')
            self.assertEqual(p.read_bytes(), raw)

    def test_failed_repair_never_reaches_token_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'verify.py').write_text('print("TOKEN: unsafe")\n')
            with self.assertRaisesRegex(ValueError, 'repair command failed'):
                command_check({'command': 'exit 1', 'cwd': temp,
                               'checker': {'argv': [sys.executable, 'verify.py'], 'cwd': temp}})


if __name__ == '__main__':
    unittest.main()
