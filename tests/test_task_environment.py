"""Task document/environment regressions missing from the v7 synthetic fixtures."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from agent.strategy import Agent
from agent.task_checks import describe, grounded_url
from agent.task_sandbox import aggregate
from test_jd_v4 import scene


def run_owned_helper(command, cwd):
    args = shlex.split(command)
    assert args[:2] == ['python3', '-c'] and 'ns["run"]' in args[2]
    result = subprocess.run([sys.executable, *args[1:]], cwd=cwd, capture_output=True,
                            text=True, encoding='utf-8', timeout=15)
    return '[exitCode:%s]\n%s' % (result.returncode, result.stdout)


class TaskEnvironmentTests(unittest.TestCase):
    def test_real_submission_heading_and_indented_fence(self):
        text = ('查询接口 http://localhost:8899\n## 提交形式\n'
                '- 以字符串形式提交\n\n  ```json\n'
                '  {"city":"北京","total_count":0}\n  ```\n')
        self.assertEqual(describe(text)['example'], {'city': '北京', 'total_count': 0})

    def test_query_supports_required_literal_fields(self):
        answer = aggregate([{'id': 1}], {'city': {'op': 'constant', 'value': '北京'},
                                        'total_count': {'op': 'count'}})
        self.assertEqual(answer, {'city': '北京', 'total_count': 1})

    def test_documented_origin_and_route_can_be_combined(self):
        text = '服务 http://localhost:8899，接口 `GET /api/heritage`。'
        self.assertTrue(grounded_url('http://localhost:8899/api/heritage', text))
        self.assertFalse(grounded_url('http://localhost:8899/api/guessed', text))
        self.assertFalse(grounded_url('http://other.invalid/api/heritage', text))

    def test_real_documents_paginated_query_and_complete_answer_within_15_rounds(self):
        from test_task_quality import api, page, execute_helper
        with tempfile.TemporaryDirectory() as temp, api([
                page([{'id': 1}], 2), page([{'id': 2}], 2)]) as (plan, calls):
            root = Path(temp)
            (root / 'task_flow.md').write_text(
                '查询城市为海城。参考 `API_DOCS.md`。\n## 提交形式\n'
                '  ```json\n  {"city":"海城","total_count":0}\n  ```', encoding='utf-8')
            (root / 'API_DOCS.md').write_text('查询接口 ' + plan['url'], encoding='utf-8')
            agent = Agent(str(root / 'state'))
            data = scene(10)
            data['teamOur']['roles'][3]['pos'] = {'x': 24, 'y': 14}
            data['teamOur']['playerTasks'] = [{'taskType': '自进化类1', 'isValid': True, 'timeoutRounds': 15}]
            self.assertEqual(agent.decide(data)['roleCommandMap']['20011']['action'], 'acceptTask')
            data.update(roundNo=11, phaseTask='请阅读 task_flow.md')
            cmd = agent.decide(data)['executeCmd']
            data.update(roundNo=12, lastCmdResult=run_owned_helper(cmd, temp))
            prompt = agent.decide(data)['prompt']
            self.assertIn(plan['url'], prompt)
            plan['aggregations'] = {'city': {'op': 'constant', 'value': '海城'},
                                    'total_count': {'op': 'count'}}
            data.update(roundNo=13, lastCmdResult='', llmResp=json.dumps({'action': 'query', 'plan': plan}))
            cmd = agent.decide(data)['executeCmd']
            data.update(roundNo=14, llmResp='', lastCmdResult=execute_helper(cmd))
            submitted = agent.decide(data)['roleCommandMap']['20011']
            self.assertEqual(submitted['action'], 'submitAnswer')
            self.assertEqual(json.loads(submitted['taskAnswer']), {'city': '海城', 'total_count': 2})
            self.assertEqual(len(calls), 2)

    def test_relative_checker_is_bound_to_task_document_not_process_directory(self):
        text = ('修复部署，`cd ws`，运行 `python3 verify.py` 验收，输出 TOKEN。\n'
                '## 提交规则\n```json\n{"token":"xxx"}\n```')
        contract = describe(text, '/tmp/rotating/tasks/task.md')
        self.assertEqual(contract['checker']['cwd'], '/tmp/rotating/tasks/ws')

    def test_document_discovery_reads_sibling_spec_and_binds_later_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'rotating' / 'current'
            folder.mkdir(parents=True)
            (folder / 'task_real.md').write_text('阅读 `API_DOCS.md` 后计算。', encoding='utf-8')
            (folder / 'API_DOCS.md').write_text('本题接口资料，参数必须取自这里。', encoding='utf-8')
            agent = Agent(str(root / 'state'))
            data = scene(10)
            data['teamOur']['roles'][3]['pos'] = {'x': 24, 'y': 14}
            data.update(phaseTask='请阅读 task_real.md 获取任务', errors=[])
            first = agent.decide(data)
            result = run_owned_helper(first['executeCmd'], temp)
            data.update(roundNo=11, lastCmdResult=result)
            second = agent.decide(data)
            self.assertIn('本题接口资料', second['prompt'])
            self.assertIn(folder.resolve().as_posix(), second['prompt'])
            data.update(roundNo=12, lastCmdResult='', llmResp=json.dumps(
                {'action': 'command', 'command': 'cat API_DOCS.md'}))
            third = agent.decide(data)
            self.assertEqual(third['executeCmd'], 'cd ' + shlex.quote(folder.resolve().as_posix()) + ' && cat API_DOCS.md')

    def test_ambiguous_documents_are_not_arbitrarily_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for child in ('a', 'b'):
                (root / child).mkdir()
                (root / child / 'task_ambiguous_zk.md').write_text(child)
            agent = Agent(str(root / 'state'))
            data = scene(10)
            data['teamOur']['roles'][3]['pos'] = {'x': 24, 'y': 14}
            data.update(phaseTask='请阅读 task_ambiguous_zk.md 获取任务', errors=[])
            first = agent.decide(data)
            result = run_owned_helper(first['executeCmd'], temp)
            self.assertIn('ambiguous', result)
            data.update(roundNo=11, lastCmdResult=result)
            self.assertIn('tool_error', agent.decide(data)['prompt'])


if __name__ == '__main__':
    unittest.main()
