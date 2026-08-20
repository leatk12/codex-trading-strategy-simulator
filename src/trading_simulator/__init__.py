"""Public API for the trading simulator foundation."""

from .config import AssetProfile, ConfigurationError, load_asset_profile
from .backtest import Backtest, BacktestError
from .audit import AuditExporter, AuditExportError
from .analytics import (
    AnalyticsError,
    EquityPoint,
    PerformanceAnalyzer,
    PerformanceReport,
)
from .basic_strategy import FixedProfitReentryStrategy
from .domain import (
    Action,
    Decision,
    MarketSnapshot,
    MarketState,
    Position,
    StrategyResult,
    Trade,
    TradeSide,
)
from .market_data import (
    CsvMarketDataLoader,
    HistoricalMarketData,
    MarketDataError,
    MarketDataSource,
)
from .market_states import MarketStateAssessment, MarketStateClassifier
from .execution import ExecutionError, ExecutionQuote, TradingCostModel
from .etoro_demo import (
    EtoroCredentials,
    EtoroDemoError,
    EtoroDemoPortfolioSummary,
    EtoroDemoReadOnlyClient,
)
from .etoro_shadow import (
    EtoroDryRunner,
    EtoroDryRunResult,
    EtoroResolution,
    ShadowRiskEvent,
)
from .etoro_shadow_loop import EtoroShadowRecorder, ShadowRecordOutcome
from .shadow_control import (
    ShadowApproval,
    ShadowControlState,
    ShadowControlStore,
    load_latest_risk_event,
)
from .etoro_intent import (
    EtoroIntentBuilder,
    EtoroOrderIntent,
    IntentAuditWriter,
    IntentConstraints,
    IntentReadinessResult,
    ReadinessAuditWriter,
    ReadinessReport,
)
from .etoro_demo_execution import (
    ARMING_PHRASE,
    AuditedIntent,
    EtoroDemoExecutionClient,
    ExecutionLedger,
    IntentAuditReader,
)
from .experiments import (
    DatasetSplit,
    ExperimentCase,
    ExperimentComparison,
    ExperimentError,
    ExperimentOutcome,
    OutOfSampleSplitter,
    ParameterExperiment,
)
from .portfolio import Portfolio, PortfolioError
from .risk import RiskAssessment, StructuralBreakdownPolicy
from .strategy import Strategy, StrategyContext, StrategyRuntimeState

__all__ = [
    "Action",
    "ARMING_PHRASE",
    "AssetProfile",
    "AuditExporter",
    "AuditExportError",
    "AnalyticsError",
    "Backtest",
    "BacktestError",
    "ConfigurationError",
    "CsvMarketDataLoader",
    "Decision",
    "DatasetSplit",
    "ExecutionError",
    "ExecutionQuote",
    "ExperimentCase",
    "ExperimentComparison",
    "ExperimentError",
    "ExperimentOutcome",
    "EquityPoint",
    "EtoroCredentials",
    "EtoroDemoError",
    "EtoroDemoPortfolioSummary",
    "EtoroDemoReadOnlyClient",
    "EtoroDemoExecutionClient",
    "EtoroDryRunner",
    "EtoroIntentBuilder",
    "EtoroOrderIntent",
    "EtoroDryRunResult",
    "EtoroResolution",
    "EtoroShadowRecorder",
    "ShadowRecordOutcome",
    "ShadowRiskEvent",
    "ShadowApproval",
    "ShadowControlState",
    "ShadowControlStore",
    "FixedProfitReentryStrategy",
    "MarketSnapshot",
    "IntentAuditWriter",
    "IntentAuditReader",
    "IntentConstraints",
    "IntentReadinessResult",
    "ExecutionLedger",
    "AuditedIntent",
    "ReadinessAuditWriter",
    "ReadinessReport",
    "MarketState",
    "MarketStateAssessment",
    "MarketStateClassifier",
    "HistoricalMarketData",
    "MarketDataError",
    "MarketDataSource",
    "Position",
    "Portfolio",
    "PortfolioError",
    "OutOfSampleSplitter",
    "ParameterExperiment",
    "PerformanceAnalyzer",
    "PerformanceReport",
    "RiskAssessment",
    "Strategy",
    "StrategyContext",
    "StrategyRuntimeState",
    "StructuralBreakdownPolicy",
    "StrategyResult",
    "Trade",
    "TradeSide",
    "TradingCostModel",
    "load_asset_profile",
    "load_latest_risk_event",
]
