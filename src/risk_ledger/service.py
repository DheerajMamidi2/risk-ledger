"""Bounded event-sourced orchestration for the paper-only session loop."""

from __future__ import annotations

import hmac
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from risk_ledger.analysis import StrategyAnalyzer
from risk_ledger.budget import BudgetExceededError, BudgetTracker
from risk_ledger.events import EventStore, new_event
from risk_ledger.hashing import canonical_digest
from risk_ledger.ledger import PaperExecutionError, PaperExecutionSimulator, reconcile_ledger
from risk_ledger.models import (
    ApprovalAction,
    ApprovalRecord,
    ApproveRequest,
    CreateSessionRequest,
    EventType,
    LoopBudgetUsage,
    PortfolioSnapshot,
    RejectRequest,
    RiskDecision,
    RiskLimits,
    RiskVerdict,
    RiskViolation,
    RiskViolationCode,
    ScenarioSummary,
    SessionStatus,
    SessionView,
    TerminalOutcome,
    TradeProposal,
)
from risk_ledger.projection import project_session
from risk_ledger.risk import DeterministicRiskEngine, build_approval_binding
from risk_ledger.scenarios import ReplayScenario, ScenarioCatalog


class SessionNotFoundError(LookupError):
    pass


class SessionConflictError(RuntimeError):
    pass


class ApprovalExpiredError(SessionConflictError):
    pass


Clock = Callable[[], datetime]
_SERVER_RISK_POLICY = RiskLimits()


class SessionService:
    """Forward-only controller that stops for human approval and every safety veto."""

    def __init__(
        self,
        *,
        catalog: ScenarioCatalog | None = None,
        store: EventStore | None = None,
        analyzer: StrategyAnalyzer | None = None,
        simulator: PaperExecutionSimulator | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.catalog = catalog or ScenarioCatalog.bundled()
        self._store = store or EventStore()
        self._analyzer = analyzer or StrategyAnalyzer()
        self._simulator = simulator or PaperExecutionSimulator()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._risk_policy = _SERVER_RISK_POLICY
        self._lock = threading.RLock()

    def create_session(self, request: CreateSessionRequest) -> SessionView:
        scenario = self.catalog.get(request.scenario_id)
        session_id = f"session-{uuid.uuid4().hex}"
        initial_portfolio = self._initial_portfolio(scenario)
        summary = self._summary(scenario)
        self._record(
            session_id,
            EventType.SESSION_CREATED,
            SessionStatus.CREATED,
            scenario=summary,
            dataset_digest=self.catalog.dataset_digest,
            risk_limits=self._risk_policy,
            budget=request.budget,
            budget_usage=LoopBudgetUsage(),
            portfolio=initial_portfolio,
            reason="A sealed local replay session was created in paper-only mode.",
        )
        tracker = BudgetTracker(request.budget)
        try:
            tracker.consume(cost_units=1)
            self._record(
                session_id,
                EventType.REPLAY_LOADED,
                SessionStatus.ANALYZING,
                budget_usage=tracker.usage,
                reason="Bundled replay data passed schema and lookahead-boundary validation.",
            )
            tracker.consume(cost_units=2)
            analysis = self._analyzer.analyze(scenario)
            self._record(
                session_id,
                EventType.ANALYSIS_COMPLETED,
                SessionStatus.ANALYZING,
                budget_usage=tracker.usage,
                analysis=analysis,
                reason="Strategy analysis completed using only sealed pre-decision bars.",
            )
            if not analysis.sufficient_evidence or analysis.proposal is None:
                return self._record(
                    session_id,
                    EventType.INSUFFICIENT_EVIDENCE,
                    SessionStatus.INSUFFICIENT_EVIDENCE,
                    outcome=TerminalOutcome.HALTED,
                    budget_usage=tracker.usage,
                    reason=analysis.insufficient_reason or "Evidence validation failed closed.",
                )

            proposal = analysis.proposal
            self._record(
                session_id,
                EventType.PROPOSAL_CREATED,
                SessionStatus.ANALYZING,
                proposal=proposal,
                budget_usage=tracker.usage,
                reason="A typed proposal was created with resolvable signal citations.",
            )
            tracker.consume(cost_units=2)
            decision = self._evaluate_risk_fail_closed(
                proposal=proposal,
                portfolio=initial_portfolio,
                scenario=scenario,
            )
            if not decision.approved:
                return self._record(
                    session_id,
                    EventType.RISK_VETOED,
                    SessionStatus.RISK_VETOED,
                    outcome=TerminalOutcome.HALTED,
                    risk_decision=decision,
                    budget_usage=tracker.usage,
                    reason="The independent risk engine produced a terminal no-trade veto.",
                )

            approval_binding = build_approval_binding(
                decision,
                expires_at=self._clock() + timedelta(seconds=request.budget.approval_ttl_seconds),
            )
            return self._record(
                session_id,
                EventType.RISK_CHECK_PASSED,
                SessionStatus.AWAITING_APPROVAL,
                risk_decision=decision,
                approval_binding=approval_binding,
                budget_usage=tracker.usage,
                reason="Automation stopped. A human may approve or reject this exact digest.",
            )
        except BudgetExceededError as exc:
            return self._halt(session_id, tracker.usage, str(exc))
        except Exception:
            return self._halt(
                session_id,
                tracker.usage,
                "A deterministic analysis stage failed closed; no proposal can execute.",
            )

    def get_session(self, session_id: str) -> SessionView:
        events = self._store.read(session_id)
        if not events:
            raise SessionNotFoundError("Session was not found")
        return project_session(events)

    def approve(self, session_id: str, request: ApproveRequest) -> SessionView:
        with self._lock:
            view = self.get_session(session_id)
            if self._is_same_decision(view, request.decision_id, request.approval_digest, True):
                return view
            self._validate_decision_authority(
                view, request.expected_version, request.approval_digest
            )
            assert view.approval_binding is not None
            if self._clock() >= view.approval_binding.expires_at:
                self._record(
                    session_id,
                    EventType.APPROVAL_EXPIRED,
                    SessionStatus.APPROVAL_EXPIRED,
                    outcome=TerminalOutcome.HALTED,
                    budget_usage=view.budget_usage,
                    reason="The digest-bound approval window expired; no order was submitted.",
                )
                raise ApprovalExpiredError("Approval window expired")

            tracker = BudgetTracker(view.budget, view.budget_usage)
            try:
                tracker.consume(cost_units=1)
            except BudgetExceededError as exc:
                return self._halt(session_id, tracker.usage, str(exc))
            approval = ApprovalRecord(
                actor=request.actor,
                decision_id=request.decision_id,
                action=ApprovalAction.APPROVE,
                approval_digest=request.approval_digest,
                decided_at=self._clock(),
            )
            self._record(
                session_id,
                EventType.APPROVAL_GRANTED,
                SessionStatus.APPROVED,
                approval=approval,
                budget_usage=tracker.usage,
                reason="A human approved the exact unexpired artifact binding.",
            )
            return self._execute_and_reconcile(session_id, tracker)

    def reject(self, session_id: str, request: RejectRequest) -> SessionView:
        with self._lock:
            view = self.get_session(session_id)
            if self._is_same_decision(view, request.decision_id, request.approval_digest, False):
                return view
            self._validate_decision_authority(
                view, request.expected_version, request.approval_digest
            )
            assert view.approval_binding is not None
            if self._clock() >= view.approval_binding.expires_at:
                self._record(
                    session_id,
                    EventType.APPROVAL_EXPIRED,
                    SessionStatus.APPROVAL_EXPIRED,
                    outcome=TerminalOutcome.HALTED,
                    budget_usage=view.budget_usage,
                    reason="The digest-bound approval window expired; no order was submitted.",
                )
                raise ApprovalExpiredError("Approval window expired")

            tracker = BudgetTracker(view.budget, view.budget_usage)
            try:
                tracker.consume(cost_units=1)
            except BudgetExceededError as exc:
                return self._halt(session_id, tracker.usage, str(exc))
            approval = ApprovalRecord(
                actor=request.actor,
                decision_id=request.decision_id,
                action=ApprovalAction.REJECT,
                approval_digest=request.approval_digest,
                reason=request.reason,
                decided_at=self._clock(),
            )
            return self._record(
                session_id,
                EventType.APPROVAL_REJECTED,
                SessionStatus.REJECTED,
                outcome=TerminalOutcome.HALTED,
                approval=approval,
                budget_usage=tracker.usage,
                reason="A human rejected the proposal; the session is a terminal no-trade outcome.",
            )

    def _execute_and_reconcile(self, session_id: str, tracker: BudgetTracker) -> SessionView:
        view = self.get_session(session_id)
        if view.proposal is None or view.risk_decision is None or view.approval is None:
            return self._halt(
                session_id,
                tracker.usage,
                "Execution authority was incomplete and failed closed.",
            )
        scenario = self.catalog.get(view.scenario.scenario_id)
        if self.catalog.dataset_digest != view.dataset_digest:
            return self._halt(
                session_id,
                tracker.usage,
                "The replay dataset digest changed after approval; execution was frozen.",
            )
        try:
            tracker.consume(cost_units=3)
            order_id = (
                f"ORDER-{canonical_digest({'session': session_id, 'approval': view.approval})[:24]}"
            )
            result = self._simulator.execute(
                view.proposal,
                view.portfolio,
                scenario.execution_bar,
                order_id=order_id,
            )
            self._record(
                session_id,
                EventType.PAPER_ORDER_SUBMITTED,
                SessionStatus.EXECUTING,
                order=result.order,
                budget_usage=tracker.usage,
                reason="One idempotent local paper order was submitted to the simulator.",
            )
            self._record(
                session_id,
                EventType.PAPER_FILL_RECORDED,
                SessionStatus.EXECUTING,
                fill=result.fill,
                portfolio=result.portfolio,
                budget_usage=tracker.usage,
                reason="A deterministic next-bar simulated fill was recorded.",
            )
            tracker.consume(cost_units=2)
            initial = self._initial_portfolio(scenario)
            reconciliation = reconcile_ledger(
                initial,
                (result.order,),
                (result.fill,),
                result.portfolio,
            )
            self._record(
                session_id,
                EventType.LEDGER_RECONCILED,
                SessionStatus.RECONCILING,
                reconciliation=reconciliation,
                budget_usage=tracker.usage,
                reason="An independent reducer rebuilt cash and position from raw fills.",
            )
            if not reconciliation.balanced:
                return self._halt(
                    session_id,
                    tracker.usage,
                    "Independent ledger reconciliation found a mismatch; execution is frozen.",
                )
            return self._record(
                session_id,
                EventType.SESSION_COMPLETED,
                SessionStatus.COMPLETE,
                outcome=TerminalOutcome.COMPLETE,
                budget_usage=tracker.usage,
                reason="The paper fill reconciled exactly; the bounded session is complete.",
            )
        except BudgetExceededError as exc:
            return self._halt(session_id, tracker.usage, str(exc))
        except PaperExecutionError:
            return self._halt(
                session_id,
                tracker.usage,
                "The local paper simulator failed closed without retrying or replanning.",
            )

    def _validate_decision_authority(
        self,
        view: SessionView,
        expected_version: int,
        approval_digest: str,
    ) -> None:
        if view.status is not SessionStatus.AWAITING_APPROVAL:
            raise SessionConflictError("Session is not awaiting a decision")
        if view.version != expected_version:
            raise SessionConflictError("Session version changed")
        if view.approval_binding is None or view.proposal is None or view.risk_decision is None:
            raise SessionConflictError("Approval authority is incomplete")
        expected_binding = build_approval_binding(
            view.risk_decision,
            expires_at=view.approval_binding.expires_at,
        )
        artifacts_match = (
            view.risk_decision.approved
            and view.risk_decision.verdict is RiskVerdict.PASS
            and canonical_digest(view.proposal) == view.risk_decision.proposal_digest
            and canonical_digest(view.risk_limits) == view.risk_decision.policy_digest
            and view.dataset_digest == view.risk_decision.dataset_digest
            and expected_binding == view.approval_binding
        )
        if not artifacts_match or not hmac.compare_digest(
            approval_digest, view.approval_binding.digest
        ):
            raise SessionConflictError("Approval artifact digest does not match")

    @staticmethod
    def _is_same_decision(
        view: SessionView,
        decision_id: str,
        approval_digest: str,
        approved: bool,
    ) -> bool:
        if view.approval is None:
            return False
        expected_action = ApprovalAction.APPROVE if approved else ApprovalAction.REJECT
        return (
            view.approval.decision_id == decision_id
            and view.approval.action is expected_action
            and hmac.compare_digest(view.approval.approval_digest, approval_digest)
        )

    def _evaluate_risk_fail_closed(
        self,
        *,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        scenario: ReplayScenario,
    ) -> RiskDecision:
        try:
            return DeterministicRiskEngine().evaluate(
                proposal=proposal,
                portfolio=portfolio,
                limits=self._risk_policy,
                dataset_digest=self.catalog.dataset_digest,
                price_observed_at=scenario.last_analysis_bar.timestamp,
                decision_time=scenario.decision_time,
            )
        except Exception:
            proposal_digest = canonical_digest(proposal)
            policy_digest = canonical_digest(self._risk_policy)
            input_digest = canonical_digest(
                {
                    "proposal": proposal,
                    "portfolio": portfolio,
                    "dataset_digest": self.catalog.dataset_digest,
                    "policy_digest": policy_digest,
                }
            )
            violation = RiskViolation(
                code=RiskViolationCode.MALFORMED_INPUT,
                message="The deterministic risk engine failed closed on malformed input.",
                observed="unavailable",
                limit="valid typed inputs",
            )
            payload = {
                "verdict": RiskVerdict.ERROR_VETO,
                "violations": (violation,),
                "proposal_digest": proposal_digest,
                "dataset_digest": self.catalog.dataset_digest,
                "policy_digest": policy_digest,
                "input_digest": input_digest,
                "engine_version": "deterministic-risk-v1",
            }
            return RiskDecision(
                verdict=RiskVerdict.ERROR_VETO,
                approved=False,
                violations=(violation,),
                proposal_digest=proposal_digest,
                dataset_digest=self.catalog.dataset_digest,
                policy_digest=policy_digest,
                input_digest=input_digest,
                risk_decision_digest=canonical_digest(payload),
            )

    def _halt(
        self,
        session_id: str,
        usage: LoopBudgetUsage,
        reason: str,
    ) -> SessionView:
        return self._record(
            session_id,
            EventType.LOOP_HALTED,
            SessionStatus.FAILED_CLOSED,
            outcome=TerminalOutcome.HALTED,
            budget_usage=usage,
            reason=reason,
        )

    def _record(
        self,
        session_id: str,
        event_type: EventType,
        status: SessionStatus,
        *,
        outcome: TerminalOutcome | None = None,
        **fields: object,
    ) -> SessionView:
        current = self._store.read(session_id)
        previous_hash = current[-1].event_hash if current else "0" * 64
        event = new_event(
            session_id=session_id,
            sequence=len(current) + 1,
            event_type=event_type,
            status=status,
            outcome=outcome,
            previous_hash=previous_hash,
            occurred_at=self._clock(),
            **fields,
        )
        updated = self._store.append(
            session_id,
            expected_version=len(current),
            events=(event,),
        )
        return project_session(updated)

    @staticmethod
    def _initial_portfolio(scenario: ReplayScenario) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            cash=scenario.initial_cash,
            equity=scenario.initial_cash,
            peak_equity=scenario.initial_cash,
            drawdown_fraction=Decimal("0"),
            position_quantity=0,
            average_price=Decimal("0"),
            order_count=0,
        )

    @staticmethod
    def _summary(scenario: ReplayScenario) -> ScenarioSummary:
        return ScenarioSummary(
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            description=scenario.description,
            symbol=scenario.symbol,
            bar_count=len(scenario.analysis_bars),
            decision_time=scenario.decision_time,
        )
