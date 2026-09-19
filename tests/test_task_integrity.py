"""Regressions from the real failed tasks, without an official sandbox."""
import json
import unittest
from types import SimpleNamespace

from agent.tasks import TaskScheduler
from tests.test_tasks import TaskRollout


class IntegrityTests(unittest.TestCase):
    def scheduler(self):
        obj = TaskScheduler.__new__(TaskScheduler)
        obj.m = {'active': {'type': 'test', 'submits': 0, 'calls': 0, 'deadline': 30,
                            'transcript': [], 'bootstrap': {'httpHelper': '/tmp/random/task_http.py'}},
                 'discoveries': {}, 'experience': {}}
        obj.w = SimpleNamespace(round=10)
        obj.phase = 'Read task.md'
        obj.trace = lambda *a, **kw: None
        return obj

    def test_prompt_contains_actual_helper_path(self):
        obj = self.scheduler()
        obj.ask()
        context = json.loads(obj.prompt.split('\n', 1)[1])
        self.assertEqual('/tmp/random/task_http.py', context['bootstrap']['httpHelper'])

    def test_zero_exit_mixed_failure_is_not_evidence(self):
        obj = self.scheduler()
        obj.observe_output("[exitCode:0]\nSuccess: {'ok': False}")
        self.assertFalse(obj.answer_evidence({'token': 'made-up'})[0])

    def test_llm_cannot_replace_observed_counts(self):
        sim = TaskRollout(); sim.reach('llm')
        state = sim.agent.memory['tasks']['active']
        state['evidence'] = {'successfulCommand': True, 'expectedCount': 15, 'observedCount': 10}
        response = sim.step(False, llmResp=json.dumps({'task_result': {
            'complete': True, 'answer': {'total_count': 15},
            'evidence': {'successfulCommand': True, 'expectedCount': 15, 'observedCount': 15}}}))
        self.assertNotEqual('submitAnswer', response['roleCommandMap'].get(sim.rid, {}).get('action'))
        self.assertEqual(10, state['evidence']['observedCount'])

    def test_model_cannot_rewrite_sandbox_answer(self):
        obj = self.scheduler()
        obj.observe_output('[exitCode:0]\n{"token":"actual"}')
        self.assertTrue(obj.answer_evidence({'token': 'actual'})[0])
        self.assertFalse(obj.answer_evidence({'token': 'invented'})[0])

    def test_checker_result_directly_submits_without_another_llm(self):
        sim = TaskRollout(); sim.reach('llm')
        sim.step(False, llmResp=json.dumps({'executeCmd': 'run-current-checker'}))
        result = {'kind': 'task_result', 'complete': True, 'answer': {'token': 'actual'},
                  'checkerOutput': 'All checks passed\nTOKEN=actual\n'}
        response = sim.step(False, lastCmdResult='[exitCode:0]\n' + json.dumps(result))
        self.assertEqual('submitAnswer', response['roleCommandMap'][sim.rid]['action'])
        self.assertFalse(response['prompt'])

    def test_checker_token_must_match_and_failure_clears_old_answer(self):
        obj = self.scheduler()
        result = {'kind': 'task_result', 'complete': True, 'answer': {'token': 'invented'},
                  'checkerOutput': 'TOKEN=actual\n'}
        obj.observe_output('[exitCode:0]\n' + json.dumps(result))
        self.assertFalse(obj.answer_evidence(result['answer'])[0])
        obj.observe_output('[exitCode:0]\n{"token":"actual"}')
        obj.observe_output('[TIMEOUT]\npartial')
        self.assertFalse(obj.answer_evidence({'token': 'actual'})[0])


if __name__ == '__main__':
    unittest.main()
