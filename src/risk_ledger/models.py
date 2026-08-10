"""Immutable domain contracts for the RiskLedger bounded replay."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MONEY_QUANTUM = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]


def money(value: Decimal) -> Decimal:
    """Normalize monetary values to exact cents."""

    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


class ImmutableModel(BaseModel):
    """Closed, frozen base model used at every trust boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    RECONCILING = "RECONCILING"
    COMPLETE = "COMPLETE"
    RISK_VETOED = "RISK_VETOED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECTED = "REJECTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    FAILED_CLOSED = "FAILED_CLOSED"


class TerminalOutcome(StrEnum):
    COMPLETE = "COMPLETE"
    HALTED = "HALTED"


class RiskVerdict(StrEnum):
    PASS = "PASS"
    VETO = "VETO"
    ERROR_VETO = "ERROR_VETO"


class RiskViolationCode(StrEnum):
    MAX_POSITION = "MAX_POSITION"
    MAX_NOTIONAL = "MAX_NOTIONAL"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    MAX_ORDER_COUNT = "MAX_ORDER_COUNT"
    STALE_PRICE = "STALE_PRICE"
    NO_LEVERAGE = "NO_LEVERAGE"
    LONG_ONLY = "LONG_ONLY"
    MALFORMED_INPUT = "MALFORMED_INPUT"


class ApprovalAction(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class EventType(StrEnum):
    SESSION_CREATED = "SESSION_CREATED"
    REPLAY_LOADED = "REPLAY_LOADED"
    ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
    PROPOSAL_CREATED = "PROPOSAL_CREATED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    RISK_CHECK_PASSED = "RISK_CHECK_PASSED"
    RISK_VETOED = "RISK_VETOED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    PAPER_ORDER_SUBMITTED = "PAPER_ORDER_SUBMITTED"
    PAPER_FILL_RECORDED = "PAPER_FILL_RECORDED"
    LEDGER_RECONCILED = "LEDGER_RECONCILED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    LOOP_HALTED = "LOOP_HALTED"


class MarketBar(ImmutableModel):
    bar_id: Identifier
    timestamp: datetime
    open: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    high: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    low: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    close: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    volume: int = Field(ge=0, le=10_000_000_000)

    @model_validator(mode="after")
    def validate_ohlc(self) -> Self:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.high < self.low:
            raise ValueError("High must not be below low")
        if self.timestamp.tzinfo is None:
            raise ValueError("Market bar timestamps must be timezone-aware")
        return self


class ReplayScenario(ImmutableModel):
    scenario_id: Annotated[str, Field(min_length=3, max_length=50, pattern=r"^[a-z0-9-]+$")]
    name: Annotated[str, Field(min_length=3, max_length=80)]
    description: Annotated[str, Field(min_length=10, max_length=300)]
    symbol: Annotated[str, Field(min_length=1, max_length=10, pattern=r"^[A-Z][A-Z0-9.-]*$")]
    decision_time: datetime
    initial_cash: Decimal = Field(gt=0, le=1_000_000, max_digits=18, decimal_places=2)
    analysis_bars: Annotated[tuple[MarketBar, ...], Field(min_length=1, max_length=500)]
    execution_bar: MarketBar

    @property
    def last_analysis_bar(self) -> MarketBar:
        return self.analysis_bars[-1]

    @model_validator(mode="after")
    def validate_replay_boundary(self) -> Self:
        if self.decision_time.tzinfo is None:
            raise ValueError("Decision time must be timezone-aware")
        timestamps = tuple(bar.timestamp for bar in self.analysis_bars)
        if timestamps != tuple(sorted(timestamps)) or len(set(timestamps)) != len(timestamps):
            raise ValueError("Analysis bars must be uniquely ordered")
        if len({bar.bar_id for bar in self.analysis_bars}) != len(self.analysis_bars):
            raise ValueError("Bar IDs must be unique")
        if any(timestamp > self.decision_time for timestamp in timestamps):
            raise ValueError("Analysis data may not cross the decision boundary")
        if self.execution_bar.timestamp <= self.decision_time:
            raise ValueError("Execution must use the next eligible bar")
        if self.execution_bar.bar_id in {bar.bar_id for bar in self.analysis_bars}:
            raise ValueError("Execution bar must be distinct from analysis bars")
        return self


class ScenarioSummary(ImmutableModel):
    scenario_id: str
    name: str
    description: str
    symbol: str
    bar_count: int
    decision_time: datetime


class SignalEvidence(ImmutableModel):
    signal_id: Identifier
    kind: Annotated[str, Field(min_length=3, max_length=40, pattern=r"^[A-Z_]+$")]
    value: Decimal
    threshold: Decimal
    description: Annotated[str, Field(min_length=8, max_length=240)]
    bar_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=20)]


class TradeProposal(ImmutableModel):
    proposal_id: Identifier
    scenario_id: Annotated[str, Field(min_length=3, max_length=50, pattern=r"^[a-z0-9-]+$")]
    symbol: Annotated[str, Field(min_length=1, max_length=10, pattern=r"^[A-Z][A-Z0-9.-]*$")]
    side: Side
    quantity: int = Field(gt=0, le=10_000)
    reference_price: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    notional: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    signal_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=20)]
    rationale: Annotated[str, Field(min_length=10, max_length=500)]
    generated_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        expected_notional = money(self.reference_price * self.quantity)
        if self.notional != expected_notional:
            raise ValueError("Proposal notional does not equal price times quantity")
        if len(set(self.signal_ids)) != len(self.signal_ids):
            raise ValueError("Signal citations must be unique")
        if self.generated_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Proposal timestamps must be timezone-aware")
        if self.expires_at <= self.generated_at:
            raise ValueError("Proposal expiry must follow generation")
        return self


class AnalysisResult(ImmutableModel):
    sufficient_evidence: bool
    signals: tuple[SignalEvidence, ...] = ()
    proposal: TradeProposal | None = None
    insufficient_reason: Annotated[str, Field(max_length=240)] | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        if self.sufficient_evidence:
            if self.proposal is None or not self.signals:
                raise ValueError("Sufficient analysis requires signals and a proposal")
            signal_ids = {signal.signal_id for signal in self.signals}
            if set(self.proposal.signal_ids) != signal_ids:
                raise ValueError("Proposal citations must resolve to analysis signals")
        elif self.proposal is not None or not self.insufficient_reason:
            raise ValueError("Insufficient analysis must stop without a proposal")
        return self


class PortfolioSnapshot(ImmutableModel):
    cash: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    equity: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    peak_equity: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    drawdown_fraction: Decimal = Field(ge=0, le=1, max_digits=8, decimal_places=6)
    position_quantity: int = Field(ge=0, le=1_000_000)
    average_price: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    order_count: int = Field(ge=0, le=1_000_000)


class RiskLimits(ImmutableModel):
    policy_version: Literal["risk-policy-v1"] = "risk-policy-v1"
    max_position_quantity: int = Field(default=25, ge=1, le=10_000)
    max_notional: Decimal = Field(default=Decimal("2500.00"), gt=0, le=1_000_000, decimal_places=2)
    max_drawdown_fraction: Decimal = Field(default=Decimal("0.10"), ge=0, le=1, decimal_places=6)
    max_order_count: int = Field(default=3, ge=1, le=100)
    max_price_age_seconds: int = Field(default=300, ge=1, le=86_400)
    allow_leverage: Literal[False] = False


class RiskViolation(ImmutableModel):
    code: RiskViolationCode
    message: Annotated[str, Field(min_length=5, max_length=200)]
    observed: Annotated[str, Field(max_length=80)]
    limit: Annotated[str, Field(max_length=80)]


class RiskDecision(ImmutableModel):
    verdict: RiskVerdict
    approved: bool
    violations: tuple[RiskViolation, ...]
    proposal_digest: Digest
    dataset_digest: Digest
    policy_digest: Digest
    input_digest: Digest
    risk_decision_digest: Digest
    engine_version: Literal["deterministic-risk-v1"] = "deterministic-risk-v1"

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        if self.approved != (self.verdict is RiskVerdict.PASS):
            raise ValueError("Approval flag and risk verdict disagree")
        if self.approved and self.violations:
            raise ValueError("A passing risk decision cannot contain violations")
        if not self.approved and not self.violations:
            raise ValueError("A veto must contain a reason")
        return self


class ApprovalBinding(ImmutableModel):
    digest: Digest
    proposal_digest: Digest
    dataset_digest: Digest
    policy_digest: Digest
    risk_decision_digest: Digest
    expires_at: datetime


class ApprovalRecord(ImmutableModel):
    actor: Annotated[str, Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9 ._@-]+$")]
    decision_id: Identifier
    action: ApprovalAction
    approval_digest: Digest
    reason: Annotated[str, Field(min_length=5, max_length=300)] | None = None
    decided_at: datetime


class PaperOrder(ImmutableModel):
    order_id: Identifier
    proposal_id: Identifier
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    submitted_at: datetime
    execution_bar_id: Identifier
    mode: Literal["LOCAL_PAPER_ONLY"] = "LOCAL_PAPER_ONLY"


class PaperFill(ImmutableModel):
    fill_id: Identifier
    order_id: Identifier
    proposal_id: Identifier
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    ordered_quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    gross_amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    fee: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    executed_at: datetime
    bar_id: Identifier
    venue: Literal["LOCAL_PAPER_SIMULATOR"] = "LOCAL_PAPER_SIMULATOR"
    simulated: Literal[True] = True


class ExecutionResult(ImmutableModel):
    order: PaperOrder
    fill: PaperFill
    portfolio: PortfolioSnapshot


class ReconciliationResult(ImmutableModel):
    balanced: bool
    expected_cash: Decimal
    actual_cash: Decimal
    expected_position_quantity: int
    actual_position_quantity: int
    fill_digest: Digest
    message: Annotated[str, Field(min_length=5, max_length=240)]


class LoopBudget(ImmutableModel):
    max_steps: int = Field(default=12, ge=1, le=50)
    max_retries: int = Field(default=1, ge=0, le=3)
    max_elapsed_ms: int = Field(default=2_000, ge=1, le=30_000)
    max_cost_units: int = Field(default=30, ge=1, le=200)
    approval_ttl_seconds: int = Field(default=900, ge=30, le=3_600)


class LoopBudgetUsage(ImmutableModel):
    steps: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    cost_units: int = Field(default=0, ge=0)


class LedgerEvent(ImmutableModel):
    event_id: Identifier
    session_id: Identifier
    sequence: int = Field(ge=1)
    event_type: EventType
    occurred_at: datetime
    status: SessionStatus
    outcome: TerminalOutcome | None = None
    previous_hash: Digest
    event_hash: Digest
    scenario: ScenarioSummary | None = None
    dataset_digest: Digest | None = None
    risk_limits: RiskLimits | None = None
    budget: LoopBudget | None = None
    budget_usage: LoopBudgetUsage | None = None
    analysis: AnalysisResult | None = None
    proposal: TradeProposal | None = None
    portfolio: PortfolioSnapshot | None = None
    risk_decision: RiskDecision | None = None
    approval_binding: ApprovalBinding | None = None
    approval: ApprovalRecord | None = None
    order: PaperOrder | None = None
    fill: PaperFill | None = None
    reconciliation: ReconciliationResult | None = None
    reason: Annotated[str, Field(max_length=300)] | None = None


class SessionView(ImmutableModel):
    session_id: Identifier
    version: int = Field(ge=1)
    scenario: ScenarioSummary
    dataset_digest: Digest
    status: SessionStatus
    outcome: TerminalOutcome | None
    risk_limits: RiskLimits
    budget: LoopBudget
    budget_usage: LoopBudgetUsage
    analysis: AnalysisResult | None
    proposal: TradeProposal | None
    risk_decision: RiskDecision | None
    approval_binding: ApprovalBinding | None
    approval: ApprovalRecord | None
    orders: tuple[PaperOrder, ...]
    fills: tuple[PaperFill, ...]
    portfolio: PortfolioSnapshot
    reconciliation: ReconciliationResult | None
    events: tuple[LedgerEvent, ...]
    disclosure: Literal[
        "Paper-only simulation. Simulated fills. Not financial advice. No profit claim."
    ] = "Paper-only simulation. Simulated fills. Not financial advice. No profit claim."


class CreateSessionRequest(ImmutableModel):
    scenario_id: Annotated[str, Field(min_length=3, max_length=50, pattern=r"^[a-z0-9-]+$")]
    budget: LoopBudget = Field(default_factory=LoopBudget)


class DecisionRequest(ImmutableModel):
    actor: Annotated[str, Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9 ._@-]+$")]
    decision_id: Identifier
    expected_version: int = Field(ge=0)
    approval_digest: Digest


class ApproveRequest(DecisionRequest):
    pass


class RejectRequest(DecisionRequest):
    reason: Annotated[str, Field(min_length=5, max_length=300)]
