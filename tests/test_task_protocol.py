"""Real wrapper execution with trusted fixtures; judge/model replies are simulated.

Assertions check submitted answers and transitions, not just whether a prompt exists.
No returned live model code is executed on the developer's machine.
"""
import base64
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from agent.strategy import Agent
from agent.tasks import TaskRunner, parse_object
from test_jd_v4 import scene, robot


SOLVER = '''import re
def solve(task):
    return {"count": int(re.search(r"count=(\\d+)", task).group(1))}
'''
SKILL = {"action": "skill", "match": ["城市历史建筑统计"], "python": SOLVER}


def run_fixture_solver(command):
    """Only run the fixed, reviewed fixture source inside our actual command wrapper."""
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    encoded = re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", argv[2]).group(1)
    assert json.loads(base64.b64decode(encoded))["source"] == SOLVER
    result = subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    return "[exitCode:0]\n" + result.stdout


class TaskProtocolTests(unittest.TestCase):
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
        result = self.agent.decide(self.data)
        self.data['roundNo'] += 1
        return result

    def start(self, task='请阅读task_1_beijing.md，获取任务信息', timeout=60):
        self.data['teamOur']['playerTasks'] = [
            {'taskType': '自进化类1', 'isValid': True, 'timeoutRounds': timeout}]
        self.assertEqual(self.step()['roleCommandMap']['20011']['action'], 'acceptTask')
        return self.step(phaseTask=task)

    def body(self, value=7):
        text = '城市历史建筑统计：计算以下数据并返回count字段。count=' + str(value)
        result = self.step(lastCmdResult='[exitCode:0]\n' + text)
        if result['prompt']:
            payload = json.loads(result['prompt'].splitlines()[-1])
            self.assertIn(text, payload['task'])
        return result

    def assert_answer(self, result, value):
        cmd = result['roleCommandMap']['20011']
        self.assertEqual(cmd['action'], 'submitAnswer')
        self.assertIsInstance(cmd['taskAnswer'], str)
        self.assertEqual(json.loads(cmd['taskAnswer']), value)
        self.assertFalse(result['prompt'])
        self.assertFalse(result['executeCmd'])

    def skill_answer(self):
        self.start()
        self.body()
        command = self.step(llmResp=json.dumps(SKILL))['executeCmd']
        self.assertTrue(command, 'a body-matched solver must reach the sandbox')
        self.assert_answer(self.step(lastCmdResult=run_fixture_solver(command)), {'count': 7})

    def test_body_matched_solver_executes_and_submits_computed_answer(self):
        self.skill_answer()

    def test_confirmed_cache_reads_new_file_and_recomputes_new_parameters(self):
        self.skill_answer()
        self.step(phaseTask='', lastRoundRoleActionResults={'20011': True})
        self.assertEqual(len(self.agent.missions.runner.skills), 1)
        # Restart to prove persistence, then a different task instance with different data.
        self.agent = Agent(self.temp.name)
        self.assertEqual(self.start(task='请阅读task_2_chengdu.md，获取任务信息')['executeCmd'],
                         'cat -- task_2_chengdu.md')
        r = self.body(42)
        self.assertFalse(r['prompt'], 'cached solver should run only after current body is read')
        self.assert_answer(self.step(lastCmdResult=run_fixture_solver(r['executeCmd'])), {'count': 42})

    def test_match_failure_has_real_feedback_not_json_format_error(self):
        self.start()
        self.body()
        r = self.step(llmResp=json.dumps({**SKILL, 'match': ['不存在的题型']}))
        payload = json.loads(r['prompt'].splitlines()[-1])
        self.assertIn('skill_error', payload['recent_history'][-1])
        self.assertNotIn('format_error', payload['recent_history'][-1])
        self.assert_answer(self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'count': 7}})), {'count': 7})

    def test_returned_answer_is_submitted_before_broad_danger_retreat(self):
        self.start(task='Return a token')
        self.data['robot']['roles'] = [robot(point=(20, 14), team='challenger')]
        r = self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'fresh'}}))
        self.assert_answer(r, {'token': 'fresh'})
        self.assertFalse(self.agent.missions.abandon)

    def test_returned_solver_result_is_submitted_before_broad_danger_retreat(self):
        self.start()
        self.body()
        command = self.step(llmResp=json.dumps(SKILL))['executeCmd']
        self.data['robot']['roles'] = [robot(point=(20, 14), team='challenger')]
        self.assert_answer(self.step(lastCmdResult=run_fixture_solver(command)), {'count': 7})

    def test_imminent_danger_heals_and_preserves_answer_until_safe(self):
        self.start(task='Return a token')
        self.data['robot']['roles'] = [robot(point=(22, 14))]
        self.data['teamOur']['roles'][3].update(health=50, backpack=['Medicine'])
        r = self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'saved'}}))
        self.assertEqual(r['roleCommandMap']['20011']['name'], 'Medicine')
        self.assertFalse(r['prompt'])
        self.assertFalse(r['executeCmd'])
        self.data['robot']['roles'] = []
        self.data['teamOur']['roles'][3].update(health=200, backpack=[])
        self.assert_answer(self.step(), {'token': 'saved'})

    def test_imminent_danger_without_medicine_still_escapes_and_records_response(self):
        self.start(task='Return a token')
        self.data['robot']['roles'] = [robot(point=(22, 14))]
        r = self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'saved'}}))
        self.assertEqual(r['roleCommandMap']['20011']['action'], 'move')
        self.assertEqual(self.agent.missions.runner.ready['answer'], {'token': 'saved'})
        trace = next(Path(self.temp.name).glob('*_trace.jsonl')).read_text(encoding='utf-8')
        self.assertIn('saved', trace)
        self.data['robot']['roles'] = []
        self.step(phaseTask='')
        r = self.start(task='Return a different token')
        self.assertNotIn('20011', r['roleCommandMap'], 'old task answer must not leak into new task')
        self.assertTrue(r['prompt'])

    def test_healing_preserves_pending_command_for_next_safe_turn(self):
        self.start(task='Read task data')
        self.data['robot']['roles'] = [robot(point=(22, 14))]
        self.data['teamOur']['roles'][3].update(health=50, backpack=['Medicine'])
        r = self.step(llmResp=json.dumps({'action': 'command', 'command': 'cat data.json'}))
        self.assertEqual(r['roleCommandMap']['20011']['name'], 'Medicine')
        self.assertFalse(r['executeCmd'])
        self.data['robot']['roles'] = []
        self.data['teamOur']['roles'][3].update(health=200, backpack=[])
        self.assertEqual(self.step()['executeCmd'], 'cat data.json')

    def test_wrong_answer_feedback_repairs_and_resubmits(self):
        self.start(task='Return the correct token')
        self.assert_answer(self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'wrong'}})), {'token': 'wrong'})
        r = self.step(errors=[{'errorCode': 2, 'description': 'token field is incorrect'}],
                      lastRoundRoleActionResults={'20011': True})
        self.assertIn('token field is incorrect', r['prompt'])
        self.assert_answer(self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'correct'}})), {'token': 'correct'})

    def test_task_may_use_more_than_twelve_model_calls(self):
        self.start(task='Need several corrections')
        for _ in range(13):
            r = self.step(llmResp='invalid model output')
            self.assertTrue(r['prompt'])
            self.assertNotIn('20011', r['roleCommandMap'])
        self.assert_answer(self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'value': 9}})), {'value': 9})

    def test_timeout_count_starts_at_accept_and_last_answer_can_submit(self):
        self.start(task='Answer with evidence', timeout=4)
        r = self.step(llmResp=json.dumps({'action': 'command', 'command': 'cat data.json'}))
        self.assertFalse(r['executeCmd'], 'command would leave no round for model interpretation')
        prompt = json.loads(r['prompt'].splitlines()[-1])
        self.assertEqual(prompt['remaining_rounds'], 2)
        self.assertIn('deadline_error', prompt['recent_history'][-1])
        self.assert_answer(self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'value': 9}})), {'value': 9})
        r = self.step()
        self.assertFalse(r['prompt'])
        self.assertFalse(r['executeCmd'])
        self.assertEqual(r['roleCommandMap']['20011']['action'], 'move')

    def test_legal_submission_alone_never_proves_success(self):
        for ending in ('timeout', 'wrong', 'left', 'dead', 'still_active'):
            with self.subTest(ending=ending):
                self.agent = Agent(self.temp.name)
                self.data = scene(10)
                self.data['teamOur']['roles'][3]['pos'] = {'x': 24, 'y': 14}
                self.skill_answer()
                changes = {'phaseTask': '', 'lastRoundRoleActionResults': {'20011': True}}
                if ending == 'timeout':
                    changes['roundNo'] = 70
                elif ending == 'wrong':
                    changes['errors'] = [{'errorCode': 2, 'description': 'partial answer'}]
                elif ending == 'left':
                    self.data['teamOur']['roles'][3]['pos'] = {'x': 26, 'y': 14}
                elif ending == 'dead':
                    self.data['teamOur']['roles'][3]['health'] = 0
                else:
                    changes['phaseTask'] = self.data['phaseTask']
                self.step(**changes)
                self.assertEqual(self.agent.missions.runner.skills, [])
                self.assertFalse(self.agent.missions.runner.cache_path.exists())

    def test_sandbox_failure_and_network_errors_have_distinct_feedback(self):
        self.start()
        r = self.step(lastCmdResult='[exitCode:1]\nmissing task file',
                      errors=[{'errorCode': 4, 'description': 'file not found'}])
        self.assertIn('missing task file', r['prompt'])
        self.assertEqual(self.agent.missions.runner.brief, '')
        r = self.step(errors=[{'errorCode': 3, 'description': 'model network failure'}])
        history = json.loads(r['prompt'].splitlines()[-1])['recent_history']
        self.assertIn('model_error', history[-1])
        self.assertNotIn('format_error', history[-1])

    def test_failed_skill_execution_does_not_submit_marker_from_partial_output(self):
        self.start()
        self.body()
        self.step(llmResp=json.dumps(SKILL))
        r = self.step(lastCmdResult='[TIMEOUT]\n__ZK_ANSWER__={"count":7}')
        self.assertNotIn('20011', r['roleCommandMap'])
        self.assertIn('[TIMEOUT]', r['prompt'])
        self.assertIsNone(self.agent.missions.runner.candidate)

    def test_unverified_legacy_cache_is_ignored(self):
        config = SimpleNamespace(state_dir=self.temp.name)
        runner = TaskRunner(config, 'legacy')
        runner.cache_path.write_text(json.dumps([SKILL]), encoding='utf-8')
        self.assertEqual(TaskRunner(config, 'legacy').skills, [])

    def test_long_explanation_then_protocol_is_accepted_without_nested_action_execution(self):
        answer = {'action': 'answer', 'answer': {'count': 7}}
        self.assertEqual(parse_object('explanation ' * 600 + '```json\n' + json.dumps(answer) + '\n```'), answer)
        self.assertIsNone(parse_object(json.dumps({'example': answer})))
        self.assertIsNone(parse_object('{"action": ["answer"]}'))
        self.assertIsNone(parse_object('x' * 131073))


if __name__ == '__main__':
    unittest.main()
