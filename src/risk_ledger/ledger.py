"""Local paper execution and independent raw-fill reconciliation."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from risk_ledger.hashing import canonical_digest
from risk_ledger.models import (
    ExecutionResult,
    MarketBar,
    PaperFill,
    PaperOrder,
    PortfolioSnapshot,
    ReconciliationResult,
    Side,
    TradeProposal,
    money,
)

SLIPPAGE_FRACTION = Decimal("0.0005")
FEE_PER_SHARE = Decimal("0.01")


class PaperExecutionError(RuntimeError):
    """Raised when the local simulator cannot safely apply an approved proposal."""


class PaperExecutionSimulator:
    """A structural paper-only boundary with no network or broker configuration."""

    def execute(
        self,
        proposal: TradeProposal,
        portfolio: PortfolioSnapshot,
        execution_bar: MarketBar,
        *,
        order_id: str | None = None,
    ) -> ExecutionResult:
        if execution_bar.timestamp <= proposal.generated_at:
            raise PaperExecutionError("Execution bar must follow the replay decision")

        direction = Decimal("1") if proposal.side is Side.BUY else Decimal("-1")
        fill_price = money(execution_bar.open * (Decimal("1") + direction * SLIPPAGE_FRACTION))
        gross_amount = money(fill_price * proposal.quantity)
        fee = money(max(FEE_PER_SHARE * proposal.quantity, MONEY_MINIMUM_FEE))
        cash_delta = -(gross_amount + fee) if proposal.side is Side.BUY else gross_amount - fee
        next_cash = money(portfolio.cash + cash_delta)
        quantity_delta = proposal.quantity if proposal.side is Side.BUY else -proposal.quantity
        next_quantity = portfolio.position_quantity + quantity_delta
        if next_cash < 0 or next_quantity < 0:
            raise PaperExecutionError("Paper execution would violate the no-leverage boundary")

        stable_order_id = order_id or f"ORDER-{canonical_digest(proposal)[:24]}"
        order = PaperOrder(
            order_id=stable_order_id,
            proposal_id=proposal.proposal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            submitted_at=execution_bar.timestamp,
            execution_bar_id=execution_bar.bar_id,
        )
        fill = PaperFill(
            fill_id=f"FILL-{canonical_digest({'order': order, 'bar': execution_bar})[:24]}",
            order_id=order.order_id,
            proposal_id=proposal.proposal_id,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            ordered_quantity=proposal.quantity,
            price=fill_price,
            gross_amount=gross_amount,
            fee=fee,
            executed_at=execution_bar.timestamp,
            bar_id=execution_bar.bar_id,
        )

        if next_quantity == 0:
            average_price = Decimal("0.00")
        elif proposal.side is Side.BUY:
            prior_cost = portfolio.average_price * portfolio.position_quantity
            average_price = money((prior_cost + gross_amount) / next_quantity)
        else:
            average_price = portfolio.average_price

        equity = money(next_cash + Decimal(next_quantity) * execution_bar.close)
        peak_equity = max(portfolio.peak_equity, equity)
        drawdown = (
            Decimal("0")
            if peak_equity == 0
            else ((peak_equity - equity) / peak_equity).quantize(Decimal("0.000001"))
        )
        next_portfolio = PortfolioSnapshot(
            cash=next_cash,
            equity=equity,
            peak_equity=peak_equity,
            drawdown_fraction=drawdown,
            position_quantity=next_quantity,
            average_price=average_price,
            order_count=portfolio.order_count + 1,
        )
        return ExecutionResult(order=order, fill=fill, portfolio=next_portfolio)


MONEY_MINIMUM_FEE = Decimal("0.01")


def _raw_fill_state(
    initial: PortfolioSnapshot,
    fills: Iterable[PaperFill],
) -> tuple[Decimal, int, tuple[str, ...]]:
    cash = initial.cash
    position = initial.position_quantity
    issues: list[str] = []
    for fill in fills:
        expected_gross = money(fill.price * fill.quantity)
        if fill.gross_amount != expected_gross:
            issues.append(f"Fill {fill.fill_id} gross amount mismatch")
        if fill.quantity > fill.ordered_quantity:
            issues.append(f"Fill {fill.fill_id} exceeds ordered quantity")
        if fill.side is Side.BUY:
            cash = money(cash - expected_gross - fill.fee)
            position += fill.quantity
        else:
            cash = money(cash + expected_gross - fill.fee)
            position -= fill.quantity
        if cash < 0:
            issues.append("Raw fills imply leveraged cash")
        if position < 0:
            issues.append("Raw fills imply a short position")
    return cash, position, tuple(issues)


def reconcile_fill(
    initial: PortfolioSnapshot,
    fill: PaperFill,
    actual: PortfolioSnapshot,
) -> ReconciliationResult:
    """Rebuild cash and quantity from one raw fill, independently of the executor."""

    expected_cash, expected_position, raw_issues = _raw_fill_state(initial, (fill,))
    issues = list(raw_issues)
    if actual.cash != expected_cash:
        issues.append("Cash mismatch")
    if actual.position_quantity != expected_position:
        issues.append("Position mismatch")
    if actual.order_count != initial.order_count + 1:
        issues.append("Order count mismatch")
    return ReconciliationResult(
        balanced=not issues,
        expected_cash=expected_cash,
        actual_cash=actual.cash,
        expected_position_quantity=expected_position,
        actual_position_quantity=actual.position_quantity,
        fill_digest=canonical_digest(fill),
        message="Ledger reconciled exactly from the raw paper fill."
        if not issues
        else "; ".join(issues),
    )


def reconcile_ledger(
    initial: PortfolioSnapshot,
    orders: tuple[PaperOrder, ...],
    fills: tuple[PaperFill, ...],
    actual: PortfolioSnapshot,
) -> ReconciliationResult:
    """Validate fill provenance and independently reconstruct session cash and position."""

    issues: list[str] = []
    order_by_id = {order.order_id: order for order in orders}
    if len(order_by_id) != len(orders):
        issues.append("Duplicate paper order ID")
    if len({fill.fill_id for fill in fills}) != len(fills):
        issues.append("Duplicate paper fill ID")
    quantities_by_order: dict[str, int] = {}
    for fill in fills:
        order = order_by_id.get(fill.order_id)
        if order is None:
            issues.append(f"Fill {fill.fill_id} has no paper order")
            continue
        if (
            order.proposal_id != fill.proposal_id
            or order.symbol != fill.symbol
            or order.side is not fill.side
            or order.execution_bar_id != fill.bar_id
        ):
            issues.append(f"Fill {fill.fill_id} does not match its paper order")
        total_quantity = quantities_by_order.get(order.order_id, 0) + fill.quantity
        quantities_by_order = {**quantities_by_order, order.order_id: total_quantity}
        if total_quantity > order.quantity:
            issues.append(f"Paper order {order.order_id} is overfilled")
    for order in orders:
        if quantities_by_order.get(order.order_id, 0) != order.quantity:
            issues.append(f"Paper order {order.order_id} is not fully filled")

    expected_cash, expected_position, raw_issues = _raw_fill_state(initial, fills)
    issues.extend(raw_issues)
    if actual.cash != expected_cash:
        issues.append("Cash mismatch")
    if actual.position_quantity != expected_position:
        issues.append("Position mismatch")
    if actual.order_count != initial.order_count + len(orders):
        issues.append("Order count mismatch")
    return ReconciliationResult(
        balanced=not issues,
        expected_cash=expected_cash,
        actual_cash=actual.cash,
        expected_position_quantity=expected_position,
        actual_position_quantity=actual.position_quantity,
        fill_digest=canonical_digest(fills),
        message="Ledger reconciled exactly from raw paper fills."
        if not issues
        else "; ".join(issues),
    )
