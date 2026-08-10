import pytest

from risk_ledger.budget import BudgetExceededError, BudgetTracker
from risk_ledger.models import LoopBudget, LoopBudgetUsage


@pytest.mark.unit
@pytest.mark.parametrize(
    ("budget", "action"),
    [
        (LoopBudget(max_steps=1), lambda tracker: tracker.consume(cost_units=1)),
        (LoopBudget(max_cost_units=1), lambda tracker: tracker.consume(cost_units=2)),
        (LoopBudget(max_retries=0), lambda tracker: tracker.record_retry()),
    ],
)
def test_budget_tracker_halts_when_bound_is_exceeded(budget: LoopBudget, action: object) -> None:
    tracker = BudgetTracker(budget, LoopBudgetUsage(steps=1))

    with pytest.raises(BudgetExceededError):
        action(tracker)  # type: ignore[operator]


@pytest.mark.unit
def test_budget_tracker_returns_new_usage_without_mutating_prior_snapshot() -> None:
    prior = LoopBudgetUsage()
    tracker = BudgetTracker(LoopBudget(), prior)

    current = tracker.consume(cost_units=2)

    assert prior.steps == 0
    assert current.steps == 1
    assert current.cost_units == 2
