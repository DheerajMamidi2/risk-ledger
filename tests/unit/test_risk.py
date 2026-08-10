from datetime import timedelta
from decimal import Decimal

import pytest

from risk_ledger.analysis import StrategyAnalyzer
from risk_ledger.models import PortfolioSnapshot, RiskLimits, RiskViolationCode
from risk_ledger.risk import DeterministicRiskEngine
from risk_ledger.scenarios import ScenarioCatalog


def _inputs() -> tuple[object, PortfolioSnapshot, RiskLimits, object]:
    scenario = ScenarioCatalog.bundled().get("steady-rise")
    proposal = StrategyAnalyzer().analyze(scenario).proposal
    assert proposal is not None
    portfolio = PortfolioSnapshot(
        cash=Decimal("10000.00"),
        equity=Decimal("10000.00"),
        peak_equity=Decimal("10000.00"),
        drawdown_fraction=Decimal("0"),
        position_quantity=0,
        average_price=Decimal("0"),
        order_count=0,
    )
    return proposal, portfolio, RiskLimits(), scenario


@pytest.mark.unit
def test_independent_risk_engine_accepts_bounded_proposal() -> None:
    proposal, portfolio, limits, scenario = _inputs()

    decision = DeterministicRiskEngine().evaluate(
        proposal=proposal,
        portfolio=portfolio,
        limits=limits,
        dataset_digest=ScenarioCatalog.bundled().dataset_digest,
        price_observed_at=scenario.last_analysis_bar.timestamp,
        decision_time=scenario.decision_time,
    )

    assert decision.approved is True
    assert decision.violations == ()
    assert decision.proposal_digest


@pytest.mark.unit
def test_exact_position_limit_passes_but_one_share_over_vetoes() -> None:
    proposal, portfolio, limits, scenario = _inputs()
    at_limit = portfolio.model_copy(
        update={"position_quantity": limits.max_position_quantity - proposal.quantity}
    )
    over_limit = at_limit.model_copy(update={"position_quantity": at_limit.position_quantity + 1})
    engine = DeterministicRiskEngine()

    passing = engine.evaluate(
        proposal=proposal,
        portfolio=at_limit,
        limits=limits,
        dataset_digest=ScenarioCatalog.bundled().dataset_digest,
        price_observed_at=scenario.last_analysis_bar.timestamp,
        decision_time=scenario.decision_time,
    )
    vetoed = engine.evaluate(
        proposal=proposal,
        portfolio=over_limit,
        limits=limits,
        dataset_digest=ScenarioCatalog.bundled().dataset_digest,
        price_observed_at=scenario.last_analysis_bar.timestamp,
        decision_time=scenario.decision_time,
    )

    assert passing.approved is True
    assert RiskViolationCode.MAX_POSITION in {item.code for item in vetoed.violations}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("limits_update", "portfolio_update", "time_delta", "code"),
    [
        ({"max_position_quantity": 5}, {}, timedelta(0), RiskViolationCode.MAX_POSITION),
        ({"max_notional": Decimal("500")}, {}, timedelta(0), RiskViolationCode.MAX_NOTIONAL),
        (
            {"max_drawdown_fraction": Decimal("0.05")},
            {"drawdown_fraction": Decimal("0.06")},
            timedelta(0),
            RiskViolationCode.MAX_DRAWDOWN,
        ),
        (
            {"max_order_count": 1},
            {"order_count": 1},
            timedelta(0),
            RiskViolationCode.MAX_ORDER_COUNT,
        ),
        ({"max_price_age_seconds": 30}, {}, timedelta(minutes=2), RiskViolationCode.STALE_PRICE),
        ({}, {"cash": Decimal("100")}, timedelta(0), RiskViolationCode.NO_LEVERAGE),
    ],
)
def test_risk_engine_vetoes_each_locked_constraint(
    limits_update: dict[str, object],
    portfolio_update: dict[str, object],
    time_delta: timedelta,
    code: RiskViolationCode,
) -> None:
    proposal, portfolio, limits, scenario = _inputs()
    decision_time = scenario.decision_time + time_delta

    decision = DeterministicRiskEngine().evaluate(
        proposal=proposal,
        portfolio=portfolio.model_copy(update=portfolio_update),
        limits=limits.model_copy(update=limits_update),
        dataset_digest=ScenarioCatalog.bundled().dataset_digest,
        price_observed_at=scenario.last_analysis_bar.timestamp,
        decision_time=decision_time,
    )

    assert decision.approved is False
    assert code in {violation.code for violation in decision.violations}
