from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from risk_ledger.models import RiskLimits, Side, TradeProposal


def proposal_payload() -> dict[str, object]:
    generated_at = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)
    return {
        "proposal_id": "proposal-demo-001",
        "scenario_id": "steady-rise",
        "symbol": "ACME",
        "side": Side.BUY,
        "quantity": 10,
        "reference_price": Decimal("106.00"),
        "notional": Decimal("1060.00"),
        "signal_ids": ("SIG-TREND-001", "SIG-VOLUME-001"),
        "rationale": "A deterministic trend confirmed by replay volume.",
        "generated_at": generated_at,
        "expires_at": generated_at + timedelta(minutes=5),
    }


@pytest.mark.unit
def test_trade_proposal_is_frozen_and_typed() -> None:
    proposal = TradeProposal.model_validate(proposal_payload())

    assert proposal.notional == Decimal("1060.00")
    with pytest.raises(ValidationError):
        proposal.quantity = 99  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signal_ids", ()),
        ("symbol", "../../bad"),
        ("quantity", 0),
        ("rationale", "x" * 501),
        ("notional", Decimal("1.00")),
    ],
)
def test_trade_proposal_rejects_invalid_or_uncited_values(field: str, value: object) -> None:
    payload = {**proposal_payload(), field: value}

    with pytest.raises(ValidationError):
        TradeProposal.model_validate(payload)


@pytest.mark.unit
def test_risk_limits_reject_leverage_and_excessive_bounds() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(allow_leverage=True)
    with pytest.raises(ValidationError):
        RiskLimits(max_order_count=101)
