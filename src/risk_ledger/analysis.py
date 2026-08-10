"""Deterministic, citation-producing strategy analysis."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from risk_ledger.hashing import canonical_digest
from risk_ledger.models import (
    AnalysisResult,
    ReplayScenario,
    Side,
    SignalEvidence,
    TradeProposal,
    money,
)

MINIMUM_BARS = 5
TREND_THRESHOLD = Decimal("0.02")
VOLUME_THRESHOLD = Decimal("1.10")
DEFAULT_QUANTITY = 10


class StrategyAnalyzer:
    """Analyze only pre-decision bars; the execution bar is never read."""

    def analyze(self, scenario: ReplayScenario) -> AnalysisResult:
        bars = scenario.analysis_bars
        if len(bars) < MINIMUM_BARS:
            return AnalysisResult(
                sufficient_evidence=False,
                insufficient_reason="At least five analysis bars are required.",
            )

        first = bars[0]
        last = bars[-1]
        trend = (last.close - first.close) / first.close
        volume_ratio = Decimal(last.volume) / Decimal(max(first.volume, 1))
        if trend < TREND_THRESHOLD or volume_ratio < VOLUME_THRESHOLD:
            return AnalysisResult(
                sufficient_evidence=False,
                insufficient_reason="Trend and volume confirmation thresholds were not both met.",
            )

        stable_slug = scenario.scenario_id.upper()
        shared_bar_ids = (first.bar_id, last.bar_id)
        signals = (
            SignalEvidence(
                signal_id=f"SIG-{stable_slug}-TREND",
                kind="PRICE_TREND",
                value=trend.quantize(Decimal("0.000001")),
                threshold=TREND_THRESHOLD,
                description="Close-to-close return across the sealed analysis window.",
                bar_ids=shared_bar_ids,
            ),
            SignalEvidence(
                signal_id=f"SIG-{stable_slug}-VOLUME",
                kind="VOLUME_CONFIRMATION",
                value=volume_ratio.quantize(Decimal("0.000001")),
                threshold=VOLUME_THRESHOLD,
                description="Last-bar volume relative to the first sealed analysis bar.",
                bar_ids=shared_bar_ids,
            ),
        )
        analysis_fingerprint = canonical_digest(
            {
                "scenario_id": scenario.scenario_id,
                "symbol": scenario.symbol,
                "decision_time": scenario.decision_time,
                "analysis_bars": bars,
                "signals": signals,
            }
        )
        proposal = TradeProposal(
            proposal_id=f"proposal-{scenario.scenario_id}-{analysis_fingerprint[:12]}",
            scenario_id=scenario.scenario_id,
            symbol=scenario.symbol,
            side=Side.BUY,
            quantity=DEFAULT_QUANTITY,
            reference_price=last.close,
            notional=money(last.close * DEFAULT_QUANTITY),
            signal_ids=tuple(signal.signal_id for signal in signals),
            rationale=(
                "The sealed replay shows a threshold-crossing price trend with independent "
                "volume confirmation; this proposal remains subject to deterministic risk review."
            ),
            generated_at=scenario.decision_time,
            expires_at=scenario.decision_time + timedelta(minutes=5),
        )
        return AnalysisResult(
            sufficient_evidence=True,
            signals=signals,
            proposal=proposal,
        )
