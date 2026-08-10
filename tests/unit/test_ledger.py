from decimal import Decimal

import pytest

from risk_ledger.analysis import StrategyAnalyzer
from risk_ledger.ledger import PaperExecutionSimulator, reconcile_fill, reconcile_ledger
from risk_ledger.models import PortfolioSnapshot
from risk_ledger.scenarios import ScenarioCatalog


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        cash=Decimal("10000.00"),
        equity=Decimal("10000.00"),
        peak_equity=Decimal("10000.00"),
        drawdown_fraction=Decimal("0"),
        position_quantity=0,
        average_price=Decimal("0"),
        order_count=0,
    )


@pytest.mark.unit
def test_paper_execution_and_reconciliation_are_deterministic() -> None:
    scenario = ScenarioCatalog.bundled().get("steady-rise")
    proposal = StrategyAnalyzer().analyze(scenario).proposal
    assert proposal is not None
    simulator = PaperExecutionSimulator()

    first = simulator.execute(proposal, _portfolio(), scenario.execution_bar)
    second = simulator.execute(proposal, _portfolio(), scenario.execution_bar)
    reconciliation = reconcile_fill(_portfolio(), first.fill, first.portfolio)

    assert first == second
    assert first.fill.venue == "LOCAL_PAPER_SIMULATOR"
    assert first.fill.simulated is True
    assert first.portfolio.cash >= Decimal("0")
    assert reconciliation.balanced is True


@pytest.mark.unit
def test_reconciliation_detects_a_tampered_portfolio() -> None:
    scenario = ScenarioCatalog.bundled().get("steady-rise")
    proposal = StrategyAnalyzer().analyze(scenario).proposal
    assert proposal is not None
    result = PaperExecutionSimulator().execute(proposal, _portfolio(), scenario.execution_bar)
    tampered = result.portfolio.model_copy(update={"cash": result.portfolio.cash + Decimal("1")})

    reconciliation = reconcile_fill(_portfolio(), result.fill, tampered)

    assert reconciliation.balanced is False
    assert "cash mismatch" in reconciliation.message.lower()


@pytest.mark.unit
def test_reconciliation_rejects_missing_duplicate_and_altered_raw_fills() -> None:
    scenario = ScenarioCatalog.bundled().get("steady-rise")
    proposal = StrategyAnalyzer().analyze(scenario).proposal
    assert proposal is not None
    result = PaperExecutionSimulator().execute(proposal, _portfolio(), scenario.execution_bar)
    altered = result.fill.model_copy(
        update={"gross_amount": result.fill.gross_amount + Decimal("1")}
    )

    missing = reconcile_ledger(_portfolio(), (result.order,), (), result.portfolio)
    duplicated = reconcile_ledger(
        _portfolio(), (result.order,), (result.fill, result.fill), result.portfolio
    )
    changed = reconcile_ledger(_portfolio(), (result.order,), (altered,), result.portfolio)

    assert missing.balanced is False
    assert duplicated.balanced is False
    assert changed.balanced is False
