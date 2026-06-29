from agent.iteration_budget import IterationBudget


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
