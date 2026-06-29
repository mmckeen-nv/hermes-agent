from agent.iteration_budget import IterationBudget
from agent.iteration_extension import (
    build_iteration_extension_state,
    heuristic_iteration_extension_decision,
    normalize_extension_decision,
)
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


def test_iteration_budget_extends_until_hard_cap():
    budget = IterationBudget(2)

    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.remaining == 0

    assert budget.extend(3, hard_cap=4) == 4
    assert budget.max_total == 4
    assert budget.remaining == 2

    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False

def test_iteration_budget_extend_noops_at_cap_or_nonpositive():
    budget = IterationBudget(5)

    assert budget.extend(0, hard_cap=10) == 5
    assert budget.extend(-3, hard_cap=10) == 5
    assert budget.extend(10, hard_cap=5) == 5


class _BudgetDecisionProvider(MemoryProvider):
    @property
    def name(self):
        return "daystrom_dml"

    def __init__(self, decision=None, error=None):
        self.decision = decision or {}
        self.error = error

    def is_available(self):
        return True

    def initialize(self, session_id: str, **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def decide_iteration_extension(self, run_state):
        if self.error:
            raise self.error
        assert run_state["schema_version"] == "hermes.iteration_extension.v1"
        return self.decision


def test_iteration_extension_state_and_heuristic_grants_incomplete_tool_work():
    state = build_iteration_extension_state(
        messages=[
            {"role": "user", "content": "fix the tests"},
            {"role": "assistant", "content": "I will run tests", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "name": "terminal", "content": "FAILED test_api.py::test_x traceback"},
        ],
        user_message="fix the tests",
        api_call_count=30,
        budget_used=30,
        budget_max=30,
        hard_cap=300,
    )

    decision = heuristic_iteration_extension_decision(state)

    assert decision["decision"] == "grant"
    assert "incomplete_signal" in decision["reason_codes"]


def test_iteration_extension_heuristic_denies_completed_noise():
    state = build_iteration_extension_state(
        messages=[
            {"role": "user", "content": "merge it"},
            {"role": "assistant", "content": "Done. PR merged and validation passed. Nothing remains."},
        ],
        user_message="merge it",
        budget_used=30,
        budget_max=30,
        hard_cap=300,
    )

    decision = heuristic_iteration_extension_decision(state)

    assert decision["decision"] == "deny"
    assert "completion_signal" in decision["reason_codes"]


def test_memory_manager_returns_provider_extension_decision():
    mgr = MemoryManager()
    mgr.add_provider(_BudgetDecisionProvider({"decision": "grant", "extend_by": 30, "reason_codes": ["dcn_incomplete"]}))

    decision = mgr.decide_iteration_extension({"schema_version": "hermes.iteration_extension.v1"})

    assert decision["decision"] == "grant"
    assert decision["extend_by"] == 30
    assert decision["source"] == "daystrom_dml"


def test_memory_manager_provider_error_fails_closed():
    mgr = MemoryManager()
    mgr.add_provider(_BudgetDecisionProvider(error=RuntimeError("dcn unavailable")))

    decision = mgr.decide_iteration_extension({"schema_version": "hermes.iteration_extension.v1"})

    assert decision["decision"] == "deny"
    assert "provider_error" in decision["reason_codes"]


def test_normalize_extension_decision_aliases_and_invalids():
    assert normalize_extension_decision({"action": "needs_more_turns"})["decision"] == "grant"
    assert normalize_extension_decision({"decision": "noise"})["decision"] == "deny"
    assert normalize_extension_decision(object())["decision"] == "deny"
