"""Independent, deterministic and fail-closed pre-trade risk engine."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from risk_ledger.hashing import canonical_digest
from risk_ledger.models import (
    ApprovalBinding,
    PortfolioSnapshot,
    RiskDecision,
    RiskLimits,
    RiskVerdict,
    RiskViolation,
    RiskViolationCode,
    Side,
    TradeProposal,
    money,
)


class DeterministicRiskEngine:
    """Apply a versioned policy without consulting the strategy analyzer."""

    def evaluate(
        self,
        *,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        limits: RiskLimits,
        dataset_digest: str,
        price_observed_at: datetime,
        decision_time: datetime,
    ) -> RiskDecision:
        violations: list[RiskViolation] = []
        signed_quantity = proposal.quantity if proposal.side is Side.BUY else -proposal.quantity
        projected_position = portfolio.position_quantity + signed_quantity

        if projected_position < 0:
            violations.append(
                self._violation(
                    RiskViolationCode.LONG_ONLY,
                    "A paper order may not create a short position.",
                    projected_position,
                    0,
                )
            )
        if projected_position > limits.max_position_quantity:
            violations.append(
                self._violation(
                    RiskViolationCode.MAX_POSITION,
                    "Projected position exceeds the locked quantity limit.",
                    projected_position,
                    limits.max_position_quantity,
                )
            )
        if proposal.notional > limits.max_notional:
            violations.append(
                self._violation(
                    RiskViolationCode.MAX_NOTIONAL,
                    "Proposal notional exceeds the locked order limit.",
                    proposal.notional,
                    limits.max_notional,
                )
            )
        if portfolio.drawdown_fraction > limits.max_drawdown_fraction:
            violations.append(
                self._violation(
                    RiskViolationCode.MAX_DRAWDOWN,
                    "Portfolio drawdown is beyond the locked session limit.",
                    portfolio.drawdown_fraction,
                    limits.max_drawdown_fraction,
                )
            )
        if portfolio.order_count >= limits.max_order_count:
            violations.append(
                self._violation(
                    RiskViolationCode.MAX_ORDER_COUNT,
                    "The session order-count limit has been reached.",
                    portfolio.order_count,
                    limits.max_order_count,
                )
            )

        price_age = (decision_time - price_observed_at).total_seconds()
        if price_age < 0:
            violations.append(
                self._violation(
                    RiskViolationCode.MALFORMED_INPUT,
                    "A future price observation cannot be used for risk review.",
                    price_age,
                    0,
                )
            )
        elif price_age > limits.max_price_age_seconds:
            violations.append(
                self._violation(
                    RiskViolationCode.STALE_PRICE,
                    "The decision price is older than the policy allows.",
                    int(price_age),
                    limits.max_price_age_seconds,
                )
            )

        slippage_and_fee_reserve = money(
            proposal.notional * Decimal("0.01") + Decimal(proposal.quantity) * Decimal("0.01")
        )
        required_cash = proposal.notional + slippage_and_fee_reserve
        if proposal.side is Side.BUY and required_cash > portfolio.cash:
            violations.append(
                self._violation(
                    RiskViolationCode.NO_LEVERAGE,
                    "Available paper cash cannot cover notional and execution reserve.",
                    required_cash,
                    portfolio.cash,
                )
            )

        proposal_digest = canonical_digest(proposal)
        policy_digest = canonical_digest(limits)
        input_digest = canonical_digest(
            {
                "proposal": proposal,
                "portfolio": portfolio,
                "dataset_digest": dataset_digest,
                "policy_digest": policy_digest,
                "price_observed_at": price_observed_at,
                "decision_time": decision_time,
            }
        )
        verdict = RiskVerdict.VETO if violations else RiskVerdict.PASS
        decision_payload = {
            "verdict": verdict,
            "violations": tuple(violations),
            "proposal_digest": proposal_digest,
            "dataset_digest": dataset_digest,
            "policy_digest": policy_digest,
            "input_digest": input_digest,
            "engine_version": "deterministic-risk-v1",
        }
        return RiskDecision(
            verdict=verdict,
            approved=not violations,
            violations=tuple(violations),
            proposal_digest=proposal_digest,
            dataset_digest=dataset_digest,
            policy_digest=policy_digest,
            input_digest=input_digest,
            risk_decision_digest=canonical_digest(decision_payload),
        )

    @staticmethod
    def _violation(
        code: RiskViolationCode,
        message: str,
        observed: object,
        limit: object,
    ) -> RiskViolation:
        return RiskViolation(
            code=code,
            message=message,
            observed=str(observed),
            limit=str(limit),
        )


def build_approval_binding(
    decision: RiskDecision,
    *,
    expires_at: datetime,
) -> ApprovalBinding:
    """Bind all authority-bearing artifacts into one expiring digest."""

    digest = canonical_digest(
        {
            "proposal_digest": decision.proposal_digest,
            "dataset_digest": decision.dataset_digest,
            "policy_digest": decision.policy_digest,
            "risk_decision_digest": decision.risk_decision_digest,
            "expires_at": expires_at,
        }
    )
    return ApprovalBinding(
        digest=digest,
        proposal_digest=decision.proposal_digest,
        dataset_digest=decision.dataset_digest,
        policy_digest=decision.policy_digest,
        risk_decision_digest=decision.risk_decision_digest,
        expires_at=expires_at,
    )
