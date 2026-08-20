"""Fixed-profit/re-entry strategy with a momentum trailing-exit overlay."""

from __future__ import annotations

from decimal import Decimal

from .config import AssetProfile
from .domain import Action, Decision, MarketSnapshot, MarketState
from .execution import TradingCostModel
from .strategy import Strategy, StrategyContext


class FixedProfitReentryStrategy(Strategy):
    """Buy base capital, take fixed net profit, then wait for re-entry.

    This class only decides. It does not mutate a portfolio or execute trades.
    """

    def __init__(self, profile: AssetProfile) -> None:
        super().__init__(profile)
        self.cost_model = TradingCostModel.from_profile(profile)

    def evaluate(
        self, snapshot: MarketSnapshot, context: StrategyContext
    ) -> Decision:
        if snapshot.symbol != self.profile.symbol:
            raise ValueError(
                f"strategy for {self.profile.symbol!r} cannot evaluate "
                f"snapshot for {snapshot.symbol!r}"
            )
        if context.position is None:
            return self._evaluate_in_cash(snapshot, context)
        return self._evaluate_in_position(snapshot, context)

    def _evaluate_in_cash(
        self, snapshot: MarketSnapshot, context: StrategyContext
    ) -> Decision:
        if not context.automatic_buying_enabled:
            return Decision(
                action=Action.SUSPEND_AUTOMATIC_BUYING,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason="Automatic buying is disabled; cash is preserved.",
                facts={"available_cash": str(context.cash)},
            )

        if context.cash <= 0:
            return Decision(
                action=Action.HOLD,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason="No cash is available for a purchase.",
                facts={"available_cash": str(context.cash)},
            )

        if context.previous_buy_price is None:
            budget = min(
                context.base_capital,
                context.cash,
                self.profile.maximum_position_size,
            )
            return Decision(
                action=Action.BUY,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason="No position or previous purchase exists; allocate base capital.",
                facts={
                    "base_capital": str(context.base_capital),
                    "available_cash": str(context.cash),
                },
                cash_budget=budget,
            )

        reentry_price = (
            context.previous_buy_price
            * self.profile.reentry_at_previous_buy_rate
        )
        if snapshot.close <= reentry_price:
            profit_slice = self._profit_slice(
                context, self.profile.staged_reentry_profit_rate
            )
            budget, profit_used = self._budget_with_base(
                context, profit_slice
            )
            return Decision(
                action=Action.BUY,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason=(
                    "Market price reached the fixed re-entry threshold; "
                    "allocate base capital plus the configured realised-profit slice."
                ),
                facts={
                    "previous_buy_price": str(context.previous_buy_price),
                    "reentry_threshold": str(reentry_price),
                    "available_cash": str(context.cash),
                    "reentry_kind": "primary",
                    "requested_profit_reinvestment": str(profit_used),
                },
                cash_budget=budget,
                profit_reinvestment=profit_used,
                reentry_stage=1,
            )

        if context.state in {MarketState.STABILISING, MarketState.RECOVERING}:
            profit_slice = self._profit_slice(
                context, self.profile.conservative_reentry_profit_rate
            )
            budget, profit_used = self._budget_with_base(
                context, profit_slice
            )
            return Decision(
                action=Action.BUY,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason=(
                    "Price stabilised above the previous purchase threshold; "
                    "make a conservative re-entry with base capital and the "
                    "configured smaller profit slice."
                ),
                facts={
                    "previous_buy_price": str(context.previous_buy_price),
                    "reentry_threshold": str(reentry_price),
                    "available_cash": str(context.cash),
                    "reentry_kind": "conservative",
                    "requested_profit_reinvestment": str(profit_used),
                },
                cash_budget=budget,
                profit_reinvestment=profit_used,
                reentry_stage=1,
            )

        return Decision(
            action=Action.HOLD,
            state=context.state,
            timestamp=snapshot.timestamp,
            price=snapshot.close,
            reason="Price remains above the fixed re-entry threshold.",
            facts={
                "previous_buy_price": str(context.previous_buy_price),
                "reentry_threshold": str(reentry_price),
                "available_cash": str(context.cash),
            },
        )

    def _evaluate_in_position(
        self, snapshot: MarketSnapshot, context: StrategyContext
    ) -> Decision:
        position = context.position
        if position is None:  # Defensive narrowing for type checkers.
            raise RuntimeError("position unexpectedly missing")
        sale_quote = self.cost_model.quote_sell(snapshot.close, position.quantity)
        net_profit = sale_quote.cash_amount - position.cost_basis
        net_profit_rate = net_profit / position.cost_basis
        facts = {
            "all_in_cost_basis": str(position.cost_basis),
            "estimated_net_sale_proceeds": str(sale_quote.cash_amount),
            "net_profit_if_sold": str(net_profit),
            "net_profit_rate_if_sold": str(net_profit_rate),
            "required_net_profit_rate": str(
                self.profile.minimum_net_profit_rate
            ),
        }
        if context.momentum_active:
            return self._evaluate_momentum_exit(snapshot, context, facts)

        if net_profit_rate >= self.profile.minimum_net_profit_rate:
            return Decision(
                action=Action.SELL,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason=(
                    "Estimated net profit after selling costs reached the "
                    "configured fixed-profit target."
                ),
                facts=facts,
            )

        if context.reentry_stage > 0:
            return self._evaluate_staged_reentry(snapshot, context, facts)

        return Decision(
            action=Action.HOLD,
            state=context.state,
            timestamp=snapshot.timestamp,
            price=snapshot.close,
            reason=(
                "Estimated net profit after selling costs remains below the "
                "configured fixed-profit target."
            ),
            facts=facts,
        )

    def _evaluate_staged_reentry(
        self,
        snapshot: MarketSnapshot,
        context: StrategyContext,
        facts: dict[str, str],
    ) -> Decision:
        next_evaluation = context.next_reentry_evaluation_at
        facts.update(
            {
                "reentry_stage": str(context.reentry_stage),
                "reentry_profit_pool": str(context.reentry_profit_pool),
                "profit_reinvested": str(context.profit_reinvested),
                "next_reentry_evaluation_at": (
                    "not_scheduled"
                    if next_evaluation is None
                    else next_evaluation.isoformat()
                ),
            }
        )
        if next_evaluation is None or snapshot.timestamp < next_evaluation:
            return Decision(
                action=Action.HOLD,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason=(
                    "A staged re-entry is active, but its observation period "
                    "has not elapsed."
                ),
                facts=facts,
            )

        if context.state is MarketState.DECLINING:
            return Decision(
                action=Action.HOLD,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason=(
                    "The observation period elapsed, but price is still "
                    "declining; preserve cash and schedule another evaluation."
                ),
                facts=facts,
            )

        if context.state in {MarketState.STABILISING, MarketState.RECOVERING}:
            requested = self._profit_slice(
                context, self.profile.staged_reentry_profit_rate
            )
            capacity = self.profile.maximum_position_size - (
                Decimal("0")
                if context.position is None
                else context.position.cost_basis
            )
            budget = min(requested, context.cash, capacity)
            if budget > 0:
                facts["requested_profit_reinvestment"] = str(budget)
                facts["reentry_kind"] = "additional_stage"
                return Decision(
                    action=Action.BUY,
                    state=context.state,
                    timestamp=snapshot.timestamp,
                    price=snapshot.close,
                    reason=(
                        "The observation period elapsed and price shows "
                        "stabilisation or recovery; allocate one configured "
                        "additional profit slice."
                    ),
                    facts=facts,
                    cash_budget=budget,
                    profit_reinvestment=budget,
                    reentry_stage=context.reentry_stage + 1,
                )

        return Decision(
            action=Action.HOLD,
            state=context.state,
            timestamp=snapshot.timestamp,
            price=snapshot.close,
            reason=(
                "The observation period elapsed without configured evidence "
                "for another allocation; preserve cash and re-evaluate later."
            ),
            facts=facts,
        )

    @staticmethod
    def _profit_slice(context: StrategyContext, rate: Decimal) -> Decimal:
        remaining = max(
            context.reentry_profit_pool - context.profit_reinvested,
            Decimal("0"),
        )
        return min(context.reentry_profit_pool * rate, remaining)

    def _budget_with_base(
        self, context: StrategyContext, requested_profit: Decimal
    ) -> tuple[Decimal, Decimal]:
        capacity = self.profile.maximum_position_size - (
            Decimal("0")
            if context.position is None
            else context.position.cost_basis
        )
        budget = min(
            context.base_capital + requested_profit,
            context.cash,
            capacity,
        )
        base_used = min(context.base_capital, budget)
        profit_used = min(requested_profit, budget - base_used)
        return budget, profit_used

    def _evaluate_momentum_exit(
        self,
        snapshot: MarketSnapshot,
        context: StrategyContext,
        facts: dict[str, str],
    ) -> Decision:
        peak = context.momentum_peak_price
        if peak is None:
            raise RuntimeError("momentum is active but no peak price is available")

        trailing_rate = (
            self.profile.momentum_trailing_exit_rate
            if context.state is MarketState.EXPLOSIVE_MOMENTUM
            else self.profile.normal_trailing_exit_rate
        )
        trailing_threshold = peak * (Decimal("1") - trailing_rate)
        drawdown_from_peak = (peak - snapshot.close) / peak
        facts.update(
            {
                "momentum_active": "true",
                "momentum_triggered_at": (
                    "unknown"
                    if context.momentum_triggered_at is None
                    else context.momentum_triggered_at.isoformat()
                ),
                "peak_since_momentum_trigger": str(peak),
                "trailing_exit_rate": str(trailing_rate),
                "trailing_exit_threshold": str(trailing_threshold),
                "drawdown_from_peak": str(drawdown_from_peak),
                "trailing_regime": (
                    "explosive"
                    if context.state is MarketState.EXPLOSIVE_MOMENTUM
                    else "normal"
                ),
            }
        )

        if snapshot.close <= trailing_threshold:
            return Decision(
                action=Action.SELL,
                state=context.state,
                timestamp=snapshot.timestamp,
                price=snapshot.close,
                reason=(
                    "Momentum trailing exit triggered because price closed at "
                    "or below the peak-adjusted threshold."
                ),
                facts=facts,
            )

        return Decision(
            action=Action.HOLD,
            state=context.state,
            timestamp=snapshot.timestamp,
            price=snapshot.close,
            reason=(
                "Momentum trailing mode is active and the trailing exit "
                "threshold has not been crossed."
            ),
            facts=facts,
        )
