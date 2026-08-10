"""Durable bounded-loop accounting."""

from __future__ import annotations

import time
from collections.abc import Callable

from risk_ledger.models import LoopBudget, LoopBudgetUsage


class BudgetExceededError(RuntimeError):
    """Raised before an operation would cross a locked session bound."""


class BudgetTracker:
    def __init__(
        self,
        budget: LoopBudget,
        initial: LoopBudgetUsage | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = budget
        self._usage = initial or LoopBudgetUsage()
        self._monotonic = monotonic
        self._started_at = monotonic()

    @property
    def usage(self) -> LoopBudgetUsage:
        return self._usage

    def consume(self, *, cost_units: int) -> LoopBudgetUsage:
        elapsed_ms = self._usage.elapsed_ms + int((self._monotonic() - self._started_at) * 1_000)
        candidate = self._usage.model_copy(
            update={
                "steps": self._usage.steps + 1,
                "cost_units": self._usage.cost_units + cost_units,
                "elapsed_ms": elapsed_ms,
            }
        )
        self._usage = candidate
        self._check(candidate)
        self._started_at = self._monotonic()
        return candidate

    def record_retry(self) -> LoopBudgetUsage:
        candidate = self._usage.model_copy(update={"retries": self._usage.retries + 1})
        self._usage = candidate
        self._check(candidate)
        return candidate

    def _check(self, usage: LoopBudgetUsage) -> None:
        if usage.steps > self._budget.max_steps:
            raise BudgetExceededError("Maximum step count was exceeded")
        if usage.retries > self._budget.max_retries:
            raise BudgetExceededError("Maximum retry count was exceeded")
        if usage.elapsed_ms > self._budget.max_elapsed_ms:
            raise BudgetExceededError("Maximum active time was exceeded")
        if usage.cost_units > self._budget.max_cost_units:
            raise BudgetExceededError("Maximum cost-unit budget was exceeded")
