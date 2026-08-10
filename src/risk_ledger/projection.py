"""Pure fold from immutable events to the session read model."""

from __future__ import annotations

from risk_ledger.events import EventIntegrityError, verify_stream
from risk_ledger.models import (
    AnalysisResult,
    ApprovalBinding,
    ApprovalRecord,
    LoopBudget,
    LoopBudgetUsage,
    PaperFill,
    PaperOrder,
    PortfolioSnapshot,
    ReconciliationResult,
    RiskDecision,
    RiskLimits,
    ScenarioSummary,
    SessionView,
    TradeProposal,
)


def project_session(events: tuple) -> SessionView:
    if not events or not verify_stream(events):
        raise EventIntegrityError("Session event stream failed integrity validation")

    scenario: ScenarioSummary | None = None
    dataset_digest: str | None = None
    risk_limits: RiskLimits | None = None
    budget: LoopBudget | None = None
    budget_usage = LoopBudgetUsage()
    analysis: AnalysisResult | None = None
    proposal: TradeProposal | None = None
    risk_decision: RiskDecision | None = None
    approval_binding: ApprovalBinding | None = None
    approval: ApprovalRecord | None = None
    orders: tuple[PaperOrder, ...] = ()
    fills: tuple[PaperFill, ...] = ()
    portfolio: PortfolioSnapshot | None = None
    reconciliation: ReconciliationResult | None = None

    for event in events:
        scenario = event.scenario or scenario
        dataset_digest = event.dataset_digest or dataset_digest
        risk_limits = event.risk_limits or risk_limits
        budget = event.budget or budget
        budget_usage = event.budget_usage or budget_usage
        analysis = event.analysis or analysis
        proposal = event.proposal or proposal
        risk_decision = event.risk_decision or risk_decision
        approval_binding = event.approval_binding or approval_binding
        approval = event.approval or approval
        portfolio = event.portfolio or portfolio
        reconciliation = event.reconciliation or reconciliation
        if event.order is not None:
            orders = (*orders, event.order)
        if event.fill is not None:
            fills = (*fills, event.fill)

    if scenario is None or dataset_digest is None or risk_limits is None or budget is None:
        raise EventIntegrityError("Session creation record is incomplete")
    if portfolio is None:
        raise EventIntegrityError("Session portfolio record is incomplete")
    last = events[-1]
    return SessionView(
        session_id=last.session_id,
        version=len(events),
        scenario=scenario,
        dataset_digest=dataset_digest,
        status=last.status,
        outcome=last.outcome,
        risk_limits=risk_limits,
        budget=budget,
        budget_usage=budget_usage,
        analysis=analysis,
        proposal=proposal,
        risk_decision=risk_decision,
        approval_binding=approval_binding,
        approval=approval,
        orders=orders,
        fills=fills,
        portfolio=portfolio,
        reconciliation=reconciliation,
        events=events,
    )
