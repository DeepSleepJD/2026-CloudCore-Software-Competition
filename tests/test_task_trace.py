"""Evidence tests: actual Agent decisions, repeated tasks, stdout recovery and I/O failure."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.strategy import Agent
from agent.task_trace import TaskTrace
from agent.tasks import TaskRunner
from tools.read_task_trace import decode, summarize
import test_task_protocol as protocol
from test_jd_v4 import robot


class TaskTraceTests(unittest.TestCase):
    setUp = protocol.TaskProtocolTests.setUp
    tearDown = protocol.TaskProtocolTests.tearDown
    step = protocol.TaskProtocolTests.step
    start = protocol.TaskProtocolTests.start
    body = protocol.TaskProtocolTests.body
    assert_answer = protocol.TaskProtocolTests.assert_answer
    skill_answer = protocol.TaskProtocolTests.skill_answer

    def events(self):
        paths = Path(self.temp.name).glob('*_trace.jsonl*')
        events, issues = decode(line for path in paths for line in path.read_text(encoding='utf-8').splitlines())
        self.assertEqual(issues, [])
        return events

    def test_first_success_then_wrong_answer_has_separate_tasks_and_exact_wire_calls(self):
        first = self.start(task='Return first token')
        self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'one'}}))
        self.data['teamOur']['playerTasks'] = []
        self.step(phaseTask='', lastRoundRoleActionResults={'20011': True})
        self.start(task='Return second token')
        self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'two'}}))
        self.data['teamOur']['playerTasks'] = []
        self.step(phaseTask='', errors=[{'errorCode': 2, 'description': 'token rejected'}])
        events = self.events()
        tasks = summarize(events)
        self.assertEqual(len(tasks), 2)
        self.assertNotEqual(tasks[0]['task_id'], tasks[1]['task_id'])
        self.assertEqual(tasks[0]['end'], 'ended_after_accepted_submission')
        self.assertEqual(tasks[1]['end'], 'judge_wrong_answer')
        self.assertEqual(tasks[1]['errors'][0]['description'], 'token rejected')
        outputs = [e for e in events if e['event'] == 'response' and e.get('prompt')]
        self.assertEqual(outputs[0]['prompt'], first['prompt'])
        replies = [e for e in events if e['event'] == 'input' and e['llm_response']]
        self.assertEqual(replies[0]['replies_to']['prompt']['event_id'], outputs[0]['event_id'])
        self.assertEqual(replies[0]['replies_to']['prompt']['round_gap'], 1)
        second_payload = json.loads(outputs[1]['prompt'].splitlines()[-1])
        self.assertEqual(second_payload['task'], 'Return second token')
        self.assertEqual(second_payload['recent_history'], [])
        self.assertEqual(second_payload['confirmed_fields'], {})

    def test_outgoing_command_and_timeout_are_correlated_and_model_sees_error(self):
        self.start(task='Fetch current task data')
        outgoing = self.step(llmResp=json.dumps({'action': 'command', 'command': 'cat data.json'}))
        feedback = self.step(lastCmdResult='[TIMEOUT]\npartial response')
        self.assertIn('[TIMEOUT]', feedback['prompt'])
        events = self.events()
        cmd = next(e for e in events if e['event'] == 'response' and e.get('executeCmd'))
        self.assertEqual(cmd['executeCmd'], outgoing['executeCmd'])
        reply = next(e for e in events if e['event'] == 'input' and e['command_result'])
        self.assertEqual(reply['replies_to']['executeCmd']['event_id'], cmd['event_id'])
        self.assertEqual(reply['command_result'], '[TIMEOUT]\npartial response')

    def test_sandbox_plan_and_document_contract_are_logged(self):
        outgoing = self.start()
        self.body()
        events = self.events()
        plan = next(e for e in events if e['event'] == 'tool_plan')
        self.assertEqual(plan['plan']['path'], 'task_1_beijing.md')
        cmd = next(e for e in events if e['event'] == 'response' and e.get('executeCmd'))
        self.assertEqual(cmd['executeCmd'], outgoing['executeCmd'])
        self.assertTrue(any(e['event'] == 'task_contract' for e in events))

    def test_danger_retreat_records_cause_without_claiming_answer_wrong(self):
        self.start(task='Return token')
        self.data['robot']['roles'] = [robot(point=(22, 14))]
        result = self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'saved'}}))
        self.assertEqual(result['roleCommandMap']['20011']['action'], 'move')
        self.data['teamOur']['playerTasks'] = []
        self.step(phaseTask='')
        summary = summarize(self.events())[0]
        self.assertEqual(summary['exit_reason'], 'robot_danger_retreat')
        self.assertEqual(summary['end'], 'task_disappeared_unconfirmed')
        self.assertEqual(summary['submissions'], [])

    def test_task_disappears_without_feedback_is_not_reported_as_success(self):
        self.start(task='Return token')
        self.data['teamOur']['playerTasks'] = []
        self.step(phaseTask='')
        self.assertEqual(summarize(self.events())[0]['end'], 'task_disappeared_unconfirmed')

    def test_base_loss_is_visible_even_when_planner_early_returns(self):
        self.start(task='Return token')
        self.data['teamOur']['roles'] = []
        self.assertEqual(self.step()['roleCommandMap'], {})
        self.assertEqual(summarize(self.events())[0]['end'], 'base_or_all_actors_missing')

    def test_duplicate_request_is_logged_but_not_counted_twice(self):
        result = self.start(task='Return token')
        self.data['roundNo'] -= 1
        self.assertEqual(self.agent.decide(self.data), result)
        events = self.events()
        self.assertTrue(any(e.get('duplicate') for e in events))
        self.assertEqual(summarize(events)[0]['prompt_calls'], 1)

    def test_failed_trace_file_cannot_suppress_real_submission(self):
        self.start(task='Return token')
        with patch.object(Path, 'open', side_effect=OSError('read-only filesystem')):
            with self.assertLogs('agent.task_trace', level='INFO') as capture:
                result = self.step(llmResp=json.dumps({'action': 'answer', 'answer': {'token': 'valid'}}))
        self.assert_answer(result, {'token': 'valid'})
        events, issues = decode(capture.output)
        self.assertEqual(issues, [])
        response = next(e for e in events if e['event'] == 'response')
        self.assertEqual(json.loads(response['pioneer_action']['taskAnswer']), {'token': 'valid'})

    def test_network_error_and_judge_timeout_remain_distinguishable(self):
        self.start(task='Return token', timeout=4)
        response = self.step(errors=[{'errorCode': 3, 'description': 'LLM network error'}])
        self.assertIn('model_error', response['prompt'])
        self.data['teamOur']['playerTasks'] = []
        self.step(phaseTask='', errors=[{'errorCode': 1, 'description': 'Task timed out'}])
        summary = summarize(self.events())[0]
        self.assertEqual(summary['end'], 'judge_timeout')
        self.assertEqual([e['errorCode'] for e in summary['errors']], [3, 1])

    def test_rejected_cached_solver_is_removed_on_disk_even_if_task_disappears(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                with tempfile.TemporaryDirectory() as temp:
                    runner = TaskRunner(SimpleNamespace(state_dir=temp), 'cache-test')
                    skill = runner.validate_skill(protocol.SKILL)
                    runner.candidate = skill
                    runner.save_candidate()
                    runner.accepted(1, 60)
                    world = SimpleNamespace(round=2, data={'phaseTask': '城市历史建筑统计 count=7'},
                                            pioneer={'id': 7, 'pos': (1, 1)}, put=lambda *_: True)
                    runner.step(world)
                    self.assertEqual(runner.pending, 'skill')
                    world.round = 3
                    world.data['lastCmdResult'] = '[exitCode:0]\n__ZK_ANSWER__={"count":7}'
                    runner.step(world)
                    self.assertTrue(runner.awaiting_answer)
                    world.round = 4
                    world.data.update(errors=[{'errorCode': 2, 'description': 'bad cached answer'}])
                    if terminal:
                        world.data['phaseTask'] = ''
                    runner.step(world)
                    self.assertEqual(TaskRunner(SimpleNamespace(state_dir=temp), 'cache-test').skills, [])
                    self.assertIn('cache_rejected', runner.trace.path.read_text(encoding='utf-8'))


class TraceTransportTests(unittest.TestCase):
    def test_stdout_large_unicode_and_newline_payload_matches_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            trace = TaskTrace(Path(temp) / 'test_skills.json')
            with self.assertLogs('agent.task_trace', level='INFO') as capture:
                trace.start(10)
                value = '中文\n"quote"\\slash ' * 2000
                trace.emit('response', 10, prompt=value, executeCmd='python3 -c fixture')
            events, issues = decode(capture.output)
            self.assertEqual(issues, [])
            self.assertEqual(events[-1]['prompt'], value)
            stored, _ = decode(trace.path.read_text(encoding='utf-8').splitlines())
            self.assertEqual(events, stored)
            self.assertTrue(all(len(line) < 6500 for line in capture.output))
            self.assertTrue(all('\n' not in line for line in capture.output))
            incomplete, issues = decode(capture.output[:-1])
            self.assertEqual(len(incomplete), 1)
            self.assertIn('Incomplete event', issues[0])

    def test_file_failure_does_not_prevent_stdout_or_task_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            trace = TaskTrace(Path(temp) / 'test_skills.json')
            with patch.object(Path, 'open', side_effect=OSError('disk full')):
                with self.assertLogs('agent.task_trace', level='INFO') as capture:
                    trace.emit('submission', 10, answer='fixture')
            events, issues = decode(capture.output)
            self.assertEqual(issues, [])
            self.assertEqual(events[0]['answer'], 'fixture')

    def test_clipping_is_explicit_and_rotation_preserves_previous_events(self):
        with tempfile.TemporaryDirectory() as temp:
            trace = TaskTrace(Path(temp) / 'test_skills.json')
            trace.MAX_FILE, trace.MAX_TEXT = 300, 50
            trace.emit('input', 1, llm_response='x' * 80)
            trace.emit('input', 2, llm_response='y' * 80)
            trace.emit('input', 3, llm_response='z' * 80)
            events, issues = decode(line for path in Path(temp).glob('*_trace.jsonl*')
                                   for line in path.read_text(encoding='utf-8').splitlines())
            self.assertEqual(issues, [])
            self.assertEqual([e['round'] for e in events], [1, 2, 3])
            self.assertEqual(events[0]['clipped'][0]['characters'], 80)
            self.assertEqual(events[0]['clipped'][0]['kept'], 50)
            self.assertEqual(len(events[0]['clipped'][0]['sha256']), 64)


if __name__ == '__main__':
    unittest.main()
