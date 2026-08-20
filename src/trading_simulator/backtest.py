"""Chronological coordination of market data, strategy, and portfolio."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from .basic_strategy import FixedProfitReentryStrategy
from .config import AssetProfile
from .domain import Action, Decision, MarketSnapshot, MarketState, StrategyResult
from .market_data import HistoricalMarketData
from .market_states import MarketStateAssessment, MarketStateClassifier
from .portfolio import Portfolio
from .risk import RiskAssessment, StructuralBreakdownPolicy
from .strategy import Strategy, StrategyContext, StrategyRuntimeState


class BacktestError(ValueError):
    """Raised when a backtest's inputs or strategy actions are inconsistent."""


class Backtest:
    """Run one deterministic strategy over one chronological data series."""

    def __init__(
        self,
        profile: AssetProfile,
        data: HistoricalMarketData,
        strategy: Strategy | None = None,
        starting_capital: Decimal | None = None,
        state_classifier: MarketStateClassifier | None = None,
        risk_policy: StructuralBreakdownPolicy | None = None,
        manual_approval_at: datetime | None = None,
        manual_approval_times: tuple[datetime, ...] = (),
    ) -> None:
        if data.symbol != profile.symbol:
            raise BacktestError(
                f"data symbol {data.symbol!r} does not match profile "
                f"symbol {profile.symbol!r}"
            )
        self.profile = profile
        self.data = data
        self.strategy = strategy or FixedProfitReentryStrategy(profile)
        self.state_classifier = state_classifier or MarketStateClassifier(profile)
        self.risk_policy = risk_policy or StructuralBreakdownPolicy(profile)
        approvals = (
            ((manual_approval_at,) if manual_approval_at is not None else ())
            + tuple(manual_approval_times)
        )
        if any(value.tzinfo is None for value in approvals):
            raise BacktestError("manual approval timestamps must include a timezone")
        if tuple(sorted(approvals)) != approvals or len(set(approvals)) != len(approvals):
            raise BacktestError("manual approval timestamps must be unique and ordered")
        self.manual_approval_at = manual_approval_at
        self.manual_approval_times = approvals
        self.starting_capital = (
            profile.initial_investment
            if starting_capital is None
            else starting_capital
        )

    def run(self) -> StrategyResult:
        portfolio = Portfolio(self.profile, starting_cash=self.starting_capital)
        decisions: list[Decision] = []
        history = []
        previous_state = MarketState.NORMAL
        runtime = StrategyRuntimeState()
        approval_index = 0

        for snapshot in self.data:
            history.append(snapshot)
            assessment = self.state_classifier.classify(history, previous_state)
            manual_approved = False
            if (
                runtime.manual_review_active
                and approval_index < len(self.manual_approval_times)
                and snapshot.timestamp >= self.manual_approval_times[approval_index]
            ):
                runtime.approve_manual_review()
                approval_index += 1
                manual_approved = True

            risk_assessment = self.risk_policy.assess(history, portfolio.position)
            newly_triggered = False
            if (
                not manual_approved
                and risk_assessment.triggered
                and not runtime.manual_review_active
            ):
                runtime.activate_manual_review(
                    snapshot.timestamp, risk_assessment.reasons
                )
                newly_triggered = True

            effective_state = (
                MarketState.STRUCTURAL_BREAKDOWN
                if newly_triggered
                else (
                    MarketState.MANUAL_REVIEW
                    if runtime.manual_review_active
                    else assessment.state
                )
            )
            self._prepare_momentum_runtime(
                runtime, portfolio, snapshot, assessment
            )
            context = StrategyContext(
                state=effective_state,
                cash=portfolio.cash,
                realised_profit=portfolio.realised_profit,
                base_capital=portfolio.base_capital,
                position=portfolio.position,
                previous_buy_price=portfolio.previous_buy_price,
                previous_sale_price=portfolio.previous_sale_price,
                automatic_buying_enabled=not runtime.manual_review_active,
                momentum_active=runtime.momentum_active,
                momentum_peak_price=runtime.momentum_peak_price,
                momentum_triggered_at=runtime.momentum_triggered_at,
                reentry_stage=runtime.reentry_stage,
                reentry_profit_pool=runtime.reentry_profit_pool,
                profit_reinvested=runtime.profit_reinvested,
                next_reentry_evaluation_at=runtime.next_reentry_evaluation_at,
            )
            raw_decision = self.strategy.evaluate(snapshot, context)
            safe_decision = self._enforce_manual_review(
                raw_decision, runtime, newly_triggered
            )
            decision = self._add_state_explanation(
                safe_decision,
                assessment,
                effective_state,
                risk_assessment,
                newly_triggered,
                manual_approved,
            )
            decisions.append(decision)
            self._execute_decision(portfolio, decision)
            self._finish_strategy_runtime(
                runtime, portfolio, snapshot, assessment, decision
            )
            previous_state = assessment.state

        final_price = self.data[-1].close
        ending_capital = portfolio.liquidation_value(final_price)
        total_return = (
            ending_capital - portfolio.starting_capital
        ) / portfolio.starting_capital
        metrics = {
            "realised_profit": portfolio.realised_profit,
            "unrealised_profit": portfolio.unrealised_profit(final_price),
            "total_return_rate": total_return,
            "trading_costs": portfolio.total_trading_costs,
            "ending_cash": portfolio.cash,
            "invested_capital": portfolio.invested_capital,
            "largest_position": portfolio.largest_position,
            "profit_reinvested": sum(
                (
                    decision.profit_reinvestment
                    for decision in decisions
                ),
                Decimal("0"),
            ),
            "reentry_allocations": Decimal(
                sum(
                    1
                    for decision in decisions
                    if decision.reentry_stage is not None
                )
            ),
            "manual_review_events": Decimal(runtime.manual_review_events),
            "manual_approvals": Decimal(runtime.manual_approvals),
        }
        return StrategyResult(
            strategy_version=self.profile.strategy_version,
            starting_capital=portfolio.starting_capital,
            ending_capital=ending_capital,
            trades=portfolio.trades,
            decisions=tuple(decisions),
            metrics=metrics,
        )

    @staticmethod
    def _prepare_momentum_runtime(
        runtime: StrategyRuntimeState,
        portfolio: Portfolio,
        snapshot: MarketSnapshot,
        assessment: MarketStateAssessment,
    ) -> None:
        if portfolio.position is None:
            runtime.reset_momentum()
            return
        if (
            assessment.state is MarketState.EXPLOSIVE_MOMENTUM
            and not runtime.momentum_active
        ):
            # The state is known only at this candle's close, so its earlier
            # intrabar high is deliberately not used as the trigger peak.
            runtime.activate_momentum(snapshot.close, snapshot.timestamp)
            return
        if runtime.momentum_active:
            # On candles after activation, the high occurs before the final
            # close and can safely advance the peak used by the close signal.
            runtime.update_momentum_peak(snapshot.high)

    def _finish_strategy_runtime(
        self,
        runtime: StrategyRuntimeState,
        portfolio: Portfolio,
        snapshot: MarketSnapshot,
        assessment: MarketStateAssessment,
        decision: Decision,
    ) -> None:
        if decision.action is Action.SELL:
            runtime.reset_momentum()
            runtime.begin_reentry_cycle(portfolio.realised_profit)
        elif (
            decision.action is Action.BUY
            and assessment.state is MarketState.EXPLOSIVE_MOMENTUM
        ):
            runtime.activate_momentum(snapshot.close, snapshot.timestamp)
        elif portfolio.position is None:
            runtime.reset_momentum()

        if decision.action is Action.BUY and decision.reentry_stage is not None:
            runtime.record_reentry(
                stage=decision.reentry_stage,
                profit_reinvestment=decision.profit_reinvestment,
                next_evaluation_at=(
                    snapshot.timestamp
                    + timedelta(hours=self.profile.observation_period_hours)
                ),
            )
        elif (
            decision.action is Action.HOLD
            and runtime.reentry_stage > 0
            and runtime.next_reentry_evaluation_at is not None
            and snapshot.timestamp >= runtime.next_reentry_evaluation_at
        ):
            runtime.postpone_reentry_evaluation(
                snapshot.timestamp
                + timedelta(hours=self.profile.observation_period_hours)
            )

    @staticmethod
    def _add_state_explanation(
        decision: Decision,
        assessment: MarketStateAssessment,
        effective_state: MarketState,
        risk_assessment: RiskAssessment,
        newly_triggered: bool,
        manual_approved: bool,
    ) -> Decision:
        facts = dict(decision.facts)
        facts["market_state_transition"] = assessment.transition
        facts["market_state_reason"] = assessment.reason
        for name, value in assessment.facts.items():
            facts[f"market_state_{name}"] = value
        facts["risk_triggered_on_candle"] = str(newly_triggered).lower()
        facts["manual_approval_on_candle"] = str(manual_approved).lower()
        facts["risk_signals_currently_breached"] = str(
            len(risk_assessment.reasons)
        )
        if risk_assessment.reasons:
            facts["risk_reasons"] = "; ".join(risk_assessment.reasons)
        for name, value in risk_assessment.facts.items():
            facts[f"risk_{name}"] = value
        return Decision(
            action=decision.action,
            state=effective_state,
            timestamp=decision.timestamp,
            price=decision.price,
            reason=decision.reason,
            facts=facts,
            cash_budget=decision.cash_budget,
            profit_reinvestment=decision.profit_reinvestment,
            reentry_stage=decision.reentry_stage,
        )

    @staticmethod
    def _enforce_manual_review(
        decision: Decision,
        runtime: StrategyRuntimeState,
        newly_triggered: bool,
    ) -> Decision:
        should_suspend = runtime.manual_review_active and (
            decision.action is Action.BUY
            or (newly_triggered and decision.action is Action.HOLD)
        )
        if not should_suspend:
            return decision
        facts = dict(decision.facts)
        if decision.action is Action.BUY:
            facts["blocked_buy_budget"] = str(decision.cash_budget)
            facts["blocked_reentry_stage"] = str(decision.reentry_stage)
        return Decision(
            action=Action.SUSPEND_AUTOMATIC_BUYING,
            state=decision.state,
            timestamp=decision.timestamp,
            price=decision.price,
            reason=(
                "Automatic buying is suspended by the structural-breakdown "
                "safeguard; preserve cash until simulated manual approval."
            ),
            facts=facts,
        )

    def _execute_decision(
        self, portfolio: Portfolio, decision: Decision
    ) -> None:
        if decision.action is Action.BUY:
            permitted_budget = min(
                portfolio.cash,
                self.profile.maximum_position_size - portfolio.invested_capital,
            )
            budget = decision.cash_budget or min(
                portfolio.base_capital, permitted_budget
            )
            if budget <= 0:
                raise BacktestError("strategy requested BUY with no permitted budget")
            if budget > permitted_budget:
                raise BacktestError("strategy requested BUY above the permitted budget")
            portfolio.buy(
                cash_budget=budget,
                market_price=decision.price,
                timestamp=decision.timestamp,
                reason=decision.reason,
            )
        elif decision.action is Action.SELL:
            if portfolio.position is None:
                raise BacktestError("strategy requested SELL without a position")
            portfolio.sell_all(
                market_price=decision.price,
                timestamp=decision.timestamp,
                reason=decision.reason,
            )
        elif decision.action in (
            Action.HOLD,
            Action.SUSPEND_AUTOMATIC_BUYING,
        ):
            return
        else:
            raise BacktestError(f"unsupported strategy action: {decision.action}")
