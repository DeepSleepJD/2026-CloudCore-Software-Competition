"""Check that critical regression tests actually reject deliberate bad behavior.

In-memory patches only; never edits production files or the submitted package.
"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from test_jd_v4 import JDTests
from test_task_protocol import TaskProtocolTests
from test_task_quality import QualityFlowTests
from test_query_recovery import QueryRecoveryTests
from test_task_trace import TaskTraceTests
from test_task_iteration import RecordedMatchTests, VariableTaskTests
from agent.task_sandbox import _query
from agent.tasks import TaskRunner
from agent.strategy import Planner
from agent.combat import rocket_targets


def target_every_robot(world, tower):
    world.threats = world.robots
    return rocket_targets(world, tower)


def main():
    mutants = [
        ("shoot_foreign_wave", "agent.strategy.rocket_targets", target_every_robot,
         "test_foreign_wave_is_not_targeted_even_when_in_range"),
        ("spend_wall_budget_on_gun", "agent.strategy.Planner.defence_reserve", lambda self: 0,
         "test_wall_budget_prevents_weapon_purchase"),
        ("pioneer_never_accepts_tasks", "agent.missions.Missions.choose", lambda *args: False,
         "test_pioneer_accepts_available_task_instead_of_waiting"),
        ("retreat_instead_of_urgent_repair", "agent.strategy.Planner.urgent_local_supply", lambda *args: False,
         "test_local_critical_wall_repaired_before_retreat"),
    ]
    results = {}
    for name, target, bad, test in mutants:
        with patch(target, bad):
            result = unittest.TestResult()
            JDTests(test).run(result)
            results[name] = bool(result.failures or result.errors)
    task_mutants = [
        ("drop_task_body", "agent.tasks.TaskRunner.context", lambda self: self.active,
         "test_body_matched_solver_executes_and_submits_computed_answer"),
        ("restore_twelve_call_limit", "agent.tasks.TaskRunner.at_limit",
         lambda self, kind: getattr(self, kind + '_calls') >= (12 if kind == 'llm' else 16),
         "test_task_may_use_more_than_twelve_model_calls"),
        ("discard_answer_when_work_paused", "agent.tasks.TaskRunner.step",
         None, "test_returned_answer_is_submitted_before_broad_danger_retreat"),
    ]
    original_step = TaskRunner.step

    def discard_on_pause(self, world, allow_work=True, allow_submit=True):
        return original_step(self, world) if allow_work else {}

    for name, target, bad, test in task_mutants:
        with patch(target, bad or discard_on_pause):
            result = unittest.TestResult()
            TaskProtocolTests(test).run(result)
            results[name] = bool(result.failures or result.errors)
    quality_mutants = [
        ('remove_answer_shape_check', 'agent.tasks.shape_error', lambda *args: '',
         'test_format_gate_rejects_wrong_type_but_accepts_partial_fields'),
        ('ignore_incomplete_query_page', 'agent.tasks.query_observation', lambda *args: '',
         'test_partial_page_generic_output_does_not_pass_as_complete'),
        ('allow_endless_identical_command', 'agent.tasks.TaskRunner.repeat_blocked', lambda *args: False,
         'test_repeated_identical_failure_blocks_third_command_but_not_correction'),
    ]
    for name, target, bad, test in quality_mutants:
        with patch(target, bad):
            result = unittest.TestResult()
            QualityFlowTests(test).run(result)
            results[name] = bool(result.failures or result.errors)
    def discard_partial(plan, evidence):
        value = _query(plan, evidence)
        if value.get('field_errors'):
            raise ValueError('one field failed; discard everything')
        return value

    recovery_mutants = [
        ('hide_api_error_body', 'agent.task_sandbox.response_evidence', lambda raw: {},
         'test_http_status_body_and_authentication_header_survive'),
        ('discard_valid_fields_after_one_failure', 'agent.task_sandbox._query', discard_partial,
         'test_one_failed_field_preserves_other_results_and_real_null_sample'),
    ]
    for name, target, bad, test in recovery_mutants:
        with patch(target, bad):
            result = unittest.TestResult()
            QueryRecoveryTests(test).run(result)
            results[name] = bool(result.failures or result.errors)
    def forget_only_in_memory(self, world, reason):
        if self.candidate in self.skills:
            self.skills.remove(self.candidate)
        self.candidate = None

    trace_mutants = [
        ('drop_outgoing_task_calls', 'agent.task_trace.TaskTrace.response', lambda *args, **kwargs: None,
         'test_first_success_then_wrong_answer_has_separate_tasks_and_exact_wire_calls'),
        ('reload_rejected_solver_after_restart', 'agent.tasks.TaskRunner.reject_candidate', forget_only_in_memory,
         'test_rejected_cached_solver_is_removed_on_disk_even_if_task_disappears'),
    ]
    for name, target, bad, test in trace_mutants:
        with patch(target, bad):
            result = unittest.TestResult()
            TaskTraceTests(test).run(result)
            results[name] = bool(result.failures or result.errors)
    iteration_mutants = [
        ('forget_previous_query_pages', 'agent.task_evidence.PageEvidence.observe', lambda *args: False,
         RecordedMatchTests, 'test_beijing_two_observed_pages_submit_at_round_35'),
        ('reject_documented_endpoint_without_query_string', 'agent.tasks.grounded_url', lambda *args: False,
         VariableTaskTests, 'test_short_followups_discover_renamed_docs_and_bind_new_parameters'),
        ('forget_verified_interface_protocol', 'agent.task_evidence.ProtocolMemory.store', lambda *args, **kwargs: None,
         RecordedMatchTests, 'test_verified_protocol_survives_new_task_without_old_business_values'),
    ]
    for name, target, bad, case, test in iteration_mutants:
        with patch(target, bad):
            result = unittest.TestResult()
            case(test).run(result)
            results[name] = bool(result.failures or result.errors)
    print(json.dumps({"detected": sum(results.values()), "total": len(results), "mutants": results}, indent=2))
    if not all(results.values()):
        raise SystemExit("Some deliberate regressions escaped the tests")


if __name__ == "__main__":
    main()
