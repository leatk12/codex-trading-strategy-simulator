# Trading Strategy Simulator — Milestones 1–11

This is the project foundation, historical OHLCV data layer, and simulated
portfolio-accounting layer for an educational, deterministic backtester. It
does **not** connect to an exchange. Example
thresholds are assumptions for software testing, not investment advice or
claims of optimality.

## Architecture

```text
configs/                         Human-editable asset profiles (TOML)
src/trading_simulator/
  config.py                      TOML parsing and validation
  domain.py                      Financial records and market states
  market_data.py                 CSV loading and series validation
  execution.py                   Spread, slippage, fee, and break-even maths
  portfolio.py                   Cash, positions, trades, and profit accounting
  strategy.py                    Abstract strategy boundary
  basic_strategy.py              Fixed net-profit/re-entry decision rules
  backtest.py                    Chronological strategy/portfolio coordinator
  market_states.py               Explainable rolling state classification
  analytics.py                   Equity replay, metrics, and buy-and-hold
  experiments.py                 Manual comparisons and chronological holdouts
  audit.py                       Persistent CSV/JSON audit bundles
  cli.py                         Command-line data inspection
data/                            Small example OHLCV dataset
tests/                           Executable examples of foundation behaviour
```

The core uses ordinary Python objects. pandas is confined to the market-data
boundary, where it reads tabular CSV data. Rows are converted to
`MarketSnapshot` values before they reach a strategy. A future CSV loader and a
future live-data adapter can therefore implement the same `MarketDataSource`
contract.

`Strategy` is an abstract base class, similar to a Java abstract class. It
accepts a snapshot plus explicit context and returns a decision; it neither
downloads data nor executes orders. This dependency direction keeps simulated
and future exchange execution outside the strategy engine.

`TradingCostModel` is a pure calculation service: calling it returns an
`ExecutionQuote` but changes no state. `Portfolio` is closer to a Java domain
aggregate. Its methods enforce invariants and are the only place that updates
cash, positions, realised profit, and transaction history.

## Python concepts for a Java developer

- A pandas `DataFrame` is a labelled two-dimensional table. It is useful for
  CSV parsing but is intentionally not the core domain model.
- A `@dataclass` generates constructor, representation, and equality methods,
  much like a concise Java record. `frozen=True` prevents field reassignment.
- Type hints improve editor and static-checker support but are not enforced by
  Python at runtime. Runtime configuration validation is therefore explicit.
- `ABC` and `@abstractmethod` provide a familiar interface-like contract.
- `Enum` gives named states instead of fragile strings.
- `Decimal` avoids binary floating-point surprises in financial arithmetic.
- Modules are `.py` files; `__init__.py` makes the public package API explicit.

## Configuration

Each TOML file defines one `AssetProfile`. Percentages are decimal fractions:
`0.05` means 5%. Money and rates are read from TOML strings so they convert to
`Decimal` exactly. Durations are stored as integer hours so profiles remain
independent of candle resolution.

Profile validation rejects negative costs, impossible rates, inconsistent
position limits, and missing version identifiers. Strategy versions are manual
labels such as `BTC-v1.0`; no code mutates parameters from test performance.

## Install and test

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
```

If the project was installed before Milestone 2, rerun the install command so
that pip installs the newly declared pandas dependency.

## Try Milestone 2

Validate and inspect the included hourly candles:

```powershell
python -m trading_simulator inspect-data data\btc_hourly_example.csv --symbol BTC-USD
```

The installed console-script form is equivalent:

```powershell
trading-sim inspect-data data\btc_hourly_example.csv --symbol BTC-USD
```

CSV files must contain exactly these headers:

```text
timestamp,open,high,low,close,volume
```

Timestamps must include a UTC offset, such as `2025-01-01T00:00:00Z` or
`2025-01-01T01:00:00+01:00`. Input must already be in ascending order with no
duplicate timestamps. Prices use `Decimal`; timestamps are normalised to UTC.

### What OHLCV does and does not tell us

Each candle summarises an interval. `high` and `low` show its extremes, and
`volume` shows the reported quantity traded. A candle does not reveal whether
its high occurred before its low. Later backtests must use an explicit,
conservative assumption when two rules could trigger inside one candle. Volume
can hint at liquidity or participation but does not directly reveal order-book
depth, bid/ask spread, or the price available for a realistically sized order.

## Try Milestone 3

Run a transparent round-trip accounting example. It buys with 1,000 at the
first sample close and sells at the final sample close:

```powershell
python -m trading_simulator accounting-demo data\btc_hourly_example.csv --config configs\btc_example.toml
```

### Trading-cost assumptions

- The CSV close is treated as the market midpoint, not a guaranteed fill.
- The configured spread is the full difference between bid and ask. A buy pays
  half above the midpoint and a sell receives half below it.
- Slippage moves execution farther against the trade on each side. Slippage is
  the difference between expected and obtained execution price; volatility,
  latency, limited liquidity, and order size can all cause it.
- Fees apply to executed notional on both buys and sells.
- A cash budget includes buy fees. Spending 1,000 therefore never silently
  requires more than 1,000 of available cash.

The position's `average_entry_price` is its all-in cost per unit, including buy
costs. Realised profit is net sale proceeds minus the allocated all-in entry
cost. Unrealised profit estimates the same calculation using an immediate
simulated sale, including expected exit costs.

A fee-adjusted break-even price is higher than the purchase midpoint. It is the
future midpoint at which net sale proceeds exactly repay the all-in entry cost.
This is why a nominal 1% price rise is not necessarily a 1% profit.

## Try Milestone 4

Run the first deterministic strategy over a small dataset designed to exercise
every basic rule:

```powershell
python -m trading_simulator basic-backtest data\basic_strategy_example.csv --config configs\btc_example.toml
```

The command prints one decision for every candle, including `HOLD`, followed by
capital and profit totals. Its expected action sequence is:

```text
BUY -> SELL -> HOLD -> BUY -> HOLD
```

### Basic rules and assumptions

1. With no position and no previous purchase, buy using base capital.
2. While invested, estimate the cash a sale would produce after spread,
   slippage, and fees. Sell when that net profit rate reaches
   `minimum_net_profit_rate`.
3. After selling, wait until the midpoint is at or below the previous simulated
   purchase price multiplied by `reentry_at_previous_buy_rate`.
4. The original Milestone 4 rule re-entered with base capital only. Milestone 7
   extends this with explicitly capped realised-profit slices as documented
   below.

For example, `reentry_at_previous_buy_rate = "1.00"` means re-enter at 100% of
the previous purchase price or below. `"0.95"` would require a fall to 95% of
that price. These are manually configured assumptions, not learned parameters.

`FixedProfitReentryStrategy` is stateless: it receives a snapshot and immutable
`StrategyContext`, then returns a `Decision`. `Backtest` applies that decision
to `Portfolio`. This resembles command/query separation in Java and makes it
possible to unit-test decision rules without executing transactions.

### Close-price execution limitation

Milestone 4 observes a candle close and simulates execution at that same close
midpoint. In reality, the closing price is only known when the interval ends,
so an order based on it would normally execute later. Reusing the close can
make results optimistic. This is a form of timing/look-ahead risk. A later
execution policy should support next-candle-open fills and define how orders
behave across gaps.

Look-ahead bias occurs whenever a decision uses information that would not have
been available at the simulated decision time. It can make a weak strategy look
excellent in historical testing while remaining impossible to execute.

## Try Milestone 5

Inspect market-state transitions independently from trading:

```powershell
python -m trading_simulator inspect-states data\market_states_example.csv --config configs\btc_example.toml
```

The example progresses through `NORMAL`, `DECLINING`, `STABILISING`,
`RECOVERING`, and `EXPLOSIVE_MOMENTUM`. `UPTREND` is also implemented and unit
tested. The regular `basic-backtest` command now includes the classified state
and transition evidence in every decision record.

### What the classifier measures

- **Rate of change** is `(latest close - earlier close) / earlier close` over a
  configured time window. It describes direction and magnitude, but its result
  can change substantially when the lookback changes.
- **Closing range** is `(highest close - lowest close) / lowest close` in the
  lookback window. A narrow range after a decline is treated as stabilisation.
  This assumes quiet closing prices carry useful evidence that selling pressure
  has eased; it does not prove a durable bottom exists.
- **Recovery from the recent low** measures the rise from the lowest close in
  the state window. Recovery is only permitted after a declining, stabilising,
  or already recovering state, which makes the model explicitly path-dependent.
- **Rapid appreciation** uses its own asset-specific rate and time window. In
  Milestone 5 this only labels `EXPLOSIVE_MOMENTUM`; trailing-exit behavior is
  intentionally deferred to Milestone 6.

A market regime is a simplified label for a pattern of market behavior, such
as decline, range, or strong trend. Real markets do not reveal a definitive
regime label; this classifier imposes one using manually chosen thresholds.
Borderline values can switch states frequently, and different candle
resolutions may produce different classifications.

The example profile adds `market_state_lookback_hours`, `uptrend_rate`,
`declining_rate`, `stabilising_range_rate`, and `recovery_rate`. These values
are illustrative assumptions, not financially optimal BTC settings.

## Try Milestone 6

Run the momentum and trailing-exit example:

```powershell
python -m trading_simulator basic-backtest data\momentum_strategy_example.csv --config configs\btc_example.toml
```

Its expected action sequence is:

```text
BUY -> HOLD -> HOLD -> HOLD -> SELL -> BUY
```

At 113 the position enters `EXPLOSIVE_MOMENTUM`. Although its estimated net
profit already exceeds the ordinary 5% target, the strategy holds and activates
an 8% trailing distance. A later candle raises the recorded peak to 126. After
the rapid-appreciation state expires, the strategy retains the peak but switches
to the normal 5% trailing distance, producing an exit threshold of 119.70. A
close at 119 triggers the sale.

### Trailing exits and drawdown

A trailing exit is a threshold that follows a rising peak but never moves down.
For peak `P` and trailing rate `r`, the threshold is `P × (1 - r)`. It avoids
claiming that the strategy can predict the exact top.

Drawdown from peak is `(peak - current price) / peak`. It measures how much of
the peak value has been surrendered. Drawdown describes loss relative to a
reference high; it is not the same as profit or loss relative to entry.

The configured `momentum_trailing_exit_rate` is used while the classifier still
reports explosive conditions. When that state cools, the tighter
`normal_trailing_exit_rate` applies to the same stored peak. This assumes wider
room is useful during unusually volatile appreciation and that tightening the
trail afterward protects gains. A wide trail can surrender substantial profit;
a tight trail can exit during ordinary noise.

The momentum trigger candle uses its close as the initial peak. Its high may
have occurred before the close revealed the momentum state, so using that high
would insert information from before activation. On subsequent completed
candles, the high can advance the peak and the final close can test the exit.
Gaps can still produce execution well below a trailing threshold in real
markets; this fixed-cost simulator does not yet model that behavior.

## Try Milestone 7

Run the staged re-entry example:

```powershell
python -m trading_simulator basic-backtest data\staged_reentry_example.csv --config configs\btc_example.toml
```

The example first earns realised profit, re-enters near the previous purchase
price, observes continued decline for 48 hours, refuses another purchase, waits
another 48 hours, and finally allocates one additional profit slice after the
state becomes `STABILISING`.

### Capital-allocation rules

- A **primary re-entry** at or below the previous buy threshold uses base
  capital plus `staged_reentry_profit_rate` of the current re-entry profit pool.
- A **conservative re-entry** above the previous threshold requires a
  `STABILISING` or `RECOVERING` state. It uses base capital plus the smaller
  `conservative_reentry_profit_rate` slice.
- An **additional stage** never allocates base capital again. It can use one
  `staged_reentry_profit_rate` slice after each completed observation period,
  and only during `STABILISING` or `RECOVERING`.
- If the state remains `DECLINING`, cash is preserved and a new full observation
  period is scheduled.
- Allocated profit is tracked against the pool established after the preceding
  sale. Total staged allocation cannot exceed that pool, available cash, or the
  configured maximum position size.

Realised profit is profit from completed sales. Reinvesting it changes where
the cash is deployed but does not erase the historical realised-profit figure.
Unrealised profit or loss belongs to the open position and estimates what would
happen after selling costs if it were liquidated now.

The included example deliberately ends with positive realised profit but a
larger unrealised loss, producing a negative total return. This demonstrates
why buying at lower prices is not automatically profitable. The staged rule is
making a limited mean-reversion assumption: after a decline and observed
stabilisation, price may be more likely to recover. Stabilisation is evidence
of a narrow recent range, not proof of value or recovery.

`Decision` now carries typed `cash_budget`, `profit_reinvestment`, and
`reentry_stage` fields. This is preferable to parsing monetary instructions
from log text and resembles a typed command object in Java.

## Try Milestone 8

Run a structural breakdown without approval:

```powershell
python -m trading_simulator basic-backtest data\structural_breakdown_example.csv --config configs\btc_example.toml
```

The crash changes the effective state to `STRUCTURAL_BREAKDOWN`, blocks the
pending re-entry, and latches subsequent decisions in `MANUAL_REVIEW`. A later
price recovery does not silently resume trading because absence of a current
signal is not equivalent to human approval.

Simulate one manual approval:

```powershell
python -m trading_simulator basic-backtest data\structural_breakdown_example.csv --config configs\btc_example.toml --manual-approval-at 2025-06-09T00:00:00Z
```

The approval allows trading to resume on that candle. A later independent crash
triggers a second structural-breakdown event, demonstrating that approval is
not a permanent exemption.

### Independent safeguard signals

- **Entry decline** compares the close with the position's all-in average entry
  price. This is directly relevant to the held position but can be distorted by
  an unusually timed purchase.
- **Recent-peak drawdown** compares the close with the highest close in the
  configured range window. Drawdown measures loss from a reference high, not
  loss from entry.
- **Historical-range break** compares the close with the lowest *prior* close
  in the rolling range. The current candle is excluded from the prior range,
  preventing the benchmark from moving down with the price being tested.
- **Rolling volatility** is the population standard deviation of recent simple
  returns. It measures dispersion, not direction. A high value can arise from
  violent rises, falls, or reversals, and depends strongly on candle resolution.
- **Persistent decline** counts consecutive lower closes. It detects repeated
  directional deterioration that might not contain one dramatic move.

Any one configured signal can activate review. The example profile adds
`structural_peak_drawdown_rate`, `structural_range_lookback_hours`,
`structural_range_break_rate`, `volatility_lookback_hours`,
`extreme_volatility_rate`, and `persistent_decline_candles`. As with every
example threshold, these are software assumptions—not recommended BTC limits.

The safeguard is enforced twice. The strategy receives
`automatic_buying_enabled=False`, and the `Backtest` coordinator independently
replaces any requested `BUY` with `SUSPEND_AUTOMATIC_BUYING`. An automated test
uses an intentionally unsafe strategy to prove that averaging down cannot
bypass the coordinator boundary. Sell decisions remain permitted.

Version 1 manual approval is a supplied simulation timestamp rather than an
interactive interface. The approval candle acknowledges the known episode and
is not re-triggered by that candle's already observed measurements. Every later
candle is assessed normally and may open a new manual review.

## Try Milestone 9

Generate the complete performance report:

```powershell
python -m trading_simulator performance-report data\basic_strategy_example.csv --config configs\btc_example.toml
```

The report replays every trade rather than trusting precomputed totals. It
reconstructs cash, quantity, all-in cost basis, realised P&L, and net liquidation
equity at each close. If replay requires negative cash or cannot reproduce the
backtest's ending capital, analytics raises an error.

### Metric definitions and limitations

- **Total return** is `(ending capital - starting capital) / starting capital`.
  Ending capital includes the estimated cost of liquidating any open position.
- A **completed trade** is one sale matched against the weighted all-in basis of
  the quantity sold. Buys and sells are also reported separately as orders.
- **Win rate** is profitable completed sales divided by all completed sales,
  including break-even sales in the denominator. A high win rate can coexist
  with poor results if occasional losses are much larger than wins.
- **Maximum drawdown** is the largest close-to-close percentage fall from a
  previous equity peak. Starting capital is included as the initial peak, so
  entry costs are not hidden. Intrabar drawdown may be worse because the metric
  does not value the portfolio at candle lows.
- **Time invested** and **time in cash** use actual timestamp differences, so
  irregular data gaps count as elapsed exposure. The state after one candle's
  action applies until the next candle.
- **Period return volatility** is the population standard deviation of equity
  returns between observations. It measures variability, not whether returns
  are good or bad.
- The **period Sharpe ratio** is mean period return divided by period-return
  volatility with a zero risk-free rate. It is deliberately not annualised;
  annualisation would require an explicit resolution and trading-calendar
  policy. Sharpe can be distorted by non-normal returns, serial correlation,
  rare crashes, and small samples.

The buy-and-hold benchmark invests the same starting capital at the first close
and liquidates at the final close using the same fee, spread, and slippage
model. Comparing a costed strategy against an uncosted benchmark would be
misleading. Excess return is strategy return minus benchmark return; a positive
historical value does not establish future superiority.

### No leverage—absolute system rule

Leverage is prohibited under all circumstances. This is not a TOML setting and
cannot be enabled for an asset or experiment:

- buying power always equals available cash;
- `Portfolio.buy()` rejects any budget above cash;
- the backtest coordinator caps permitted budgets at cash;
- analytics rejects any ledger that would require negative cash; and
- every report declares `Leverage allowed: false` and `Leverage used: 0`.

There is no borrowing, margin, leveraged token, derivative exposure, or short
selling in Version 1. A maximum position size above current cash does not grant
additional buying power.

### Biases not solved by more statistics

Repeatedly designing against the same history creates overfitting: rules can
capture accidents in that sample rather than durable behavior. Milestone 10
provides a chronological holdout, but it cannot prevent a person from
repeatedly consulting that holdout and overfitting to it.

Survivorship bias occurs when testing only assets that survived or remain easy
to obtain today. Failed, delisted, or illiquid assets may be absent, making past
performance look safer than the opportunity set actually was. The simulator
cannot correct a biased input dataset automatically.

Correlation measures how assets move together and matters for portfolio
diversification, but Version 1 currently holds one asset, so it does not report
portfolio correlation. Adding a meaningless single-asset correlation statistic
would create sophistication without information.

## Try Milestone 10

Compare manually chosen trailing distances on separate development and
out-of-sample periods:

```powershell
python -m trading_simulator parameter-experiment data\experiment_example.csv --config configs\btc_example.toml --parameter momentum_trailing_exit_rate --values 0.05 0.08 0.10 --versions BTC-trail-5 BTC-trail-8 BTC-trail-10 --development-fraction 0.50
```

You can replace `--development-fraction 0.50` with an explicit UTC cutoff such
as `--split-at 2025-07-02T00:00:00Z`. Candles before the cutoff are development
data; the cutoff candle and later candles are out of sample. Both periods must
be non-empty and cannot overlap.

Every value and unique strategy version must be supplied explicitly. Results
remain in supplied order: the simulator does not rank cases, select a winner,
rewrite TOML, or promote a parameter. The original frozen `AssetProfile` stays
unchanged. Each case receives fresh strategy state and fresh starting cash in
each period, and the no-leverage invariant is enforced by the normal backtest
and analytics boundaries.

The development period is where rules may be inspected and revised. The
out-of-sample period is an independent historical check, not another tuning
set. Choosing a value after seeing its holdout result contaminates that
holdout; it can no longer honestly be described as unseen. Trying many values,
assets, cutoffs, or rule variants increases the chance of finding an accidental
historical success. There is no universal optimum, and a favorable comparison
does not establish future profitability.

The periods deliberately start independently. No position, peak, classifier
history, or realised profit crosses the split, and the holdout starts with the
configured initial investment. This makes comparisons clear but introduces a
warm-up limitation near the boundary: a production strategy continuing through
that timestamp could have different state and exposure. The existing
close-price timing, data-quality, and survivorship-bias limitations also still
apply.

Useful Version 1 review questions include whether fills should use a candle's
close or the following candle's open, what warm-up policy a future walk-forward
test should use, and what conservative rule applies when one candle crosses
both an entry and exit level.

## Try Milestone 11

Persist one complete backtest audit bundle:

```powershell
python -m trading_simulator export-backtest data\basic_strategy_example.csv --config configs\btc_example.toml --output-dir outputs\audit\BTC-v1.0-example
```

The directory contains `manifest.json`, `decisions.csv`, `trades.csv`,
`equity_curve.csv`, and `performance.json`. Decimal values are stored as text,
preserving their exact representation instead of converting them to binary
floating point. Decision facts use JSON inside one CSV column so the evidence
for each action remains reconstructable. Every relevant row carries its
strategy version, and the manifest and performance report explicitly declare
that leverage is not allowed.

Audit files are append-by-new-bundle in Version 1.1: the exporter refuses to
replace any of its expected files in an existing directory. Choose a new,
meaningful directory for each run. These exports record simulated results; they
are not broker statements, accounting records, or evidence of real disposals.

## Planned Milestone 12 — UK capital-gains obligation records

This future milestone will import actual transaction evidence separately from
simulated trades and produce a reviewable UK tax-year record of acquisitions,
disposals, proceeds, fees, allowable-cost inputs, gains/losses, source currency,
GBP conversion evidence, and source-document references. It will preserve the
calculation-rule version and distinguish estimates, missing evidence, and
confirmed records.

It will not treat backtest sales as taxable disposals, submit anything to HMRC,
decide personal tax residency, or present a calculated amount as definitive tax
liability. UK cryptoasset matching/pooling rules and reporting requirements can
change and depend on personal circumstances, so implementation must be checked
against then-current HMRC guidance and reviewed by a qualified UK tax adviser
where appropriate. Tax rates and allowances will not be embedded as timeless
strategy configuration.

## Read-only eToro Demo adapter and shadow mode (Version 0.12.0)

The optional adapter can authenticate against eToro's Demo portfolio and read
market data. It cannot place orders. Its URL allowlist accepts only the official
HTTPS API origin and the Demo portfolio, Demo P&L, and market-data `GET` paths.
It rejects real-account paths, execution paths, redirects, and other hosts
before credentials can be forwarded. This adapter does not connect the strategy
runtime to eToro and is not a live bot.

The eToro portal currently labels generated credentials `Public Key` and
`Private Key`, while its HTTP examples call the headers `x-api-key` and
`x-user-key`. This adapter maps public to `x-api-key` and private to
`x-user-key`. The first real request must remain read-only so that mapping can
be verified without creating an order.

Set process-only environment variables without typing secrets directly into a
PowerShell command or saving them in shell history:

```powershell
$publicSecret = Read-Host "eToro public key" -AsSecureString
$publicPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($publicSecret)
try { $env:ETORO_PUBLIC_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($publicPointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($publicPointer) }

$privateSecret = Read-Host "eToro private key" -AsSecureString
$privatePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($privateSecret)
try { $env:ETORO_PRIVATE_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($privatePointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($privatePointer) }

python -m trading_simulator etoro-demo-check
```

After that check succeeds, run one local strategy replay against recent,
completed eToro candles:

```powershell
python -m trading_simulator etoro-dry-run `
  --config configs\btc_example.toml `
  --symbol BTC `
  --resolution one-hour `
  --candles 200
```

The command resolves an exact eToro symbol, downloads up to the requested
number of recent candles, excludes a candle that is still forming, and replays
the existing strategy locally. It prints the latest decision and a proposed
action. It never calls an execution endpoint, never submits an order, and
always fixes leverage at 1x with no borrowing. A dry run starts with the local
profile's simulated capital; it does not manage or allocate the Demo account's
virtual balance.

For continuous observation, start shadow mode. It polls for completed candles
and appends each new candle's decision once to a JSONL audit log:

```powershell
python -m trading_simulator etoro-shadow-loop `
  --config configs\btc_example.toml `
  --symbol BTC `
  --resolution one-hour `
  --candles 200 `
  --log outputs\shadow\btc-one-hour.jsonl
```

Leave that PowerShell window open and press `Ctrl+C` to stop. Restarting with
the same log is safe: duplicate or older candle decisions are not appended.
Individual API failures are reported and the next polling cycle continues. For
a short supervised trial, add `--max-cycles 3`. Shadow mode cannot submit an
order, access real-account endpoints, borrow, or use leverage above 1x.

When a structural safeguard latches manual review, first stop and restart the
updated loop for one cycle so the triggering event and evidence are recorded,
then inspect it:

```powershell
python -m trading_simulator etoro-shadow-review `
  --log outputs\shadow\btc-one-hour.jsonl
```

Approval requires the exact event ID printed by that command, an operator
label, and an explicit acknowledgement. It applies only to that event and does
not enable broker execution:

```powershell
python -m trading_simulator etoro-shadow-approve `
  --log outputs\shadow\btc-one-hour.jsonl `
  --event-id EVENT_ID_FROM_REVIEW `
  --approved-by local-operator `
  --acknowledge-risk
```

The persistent kill switch overrides every approval. Enabling it also
invalidates the stored approval; disabling it never creates a replacement:

```powershell
python -m trading_simulator etoro-shadow-kill-switch `
  --log outputs\shadow\btc-one-hour.jsonl `
  --enable `
  --changed-by local-operator `
  --reason "supervised stop"
```

## Demo execution-readiness intents (Version 0.14.0)

The readiness command reconciles the latest approved strategy replay with the
read-only eToro Demo P&L response and, only when a buy or close decision is
fully consistent, appends a non-executing request intent to JSONL:

```powershell
python -m trading_simulator etoro-demo-intent `
  --config configs\btc_example.toml `
  --symbol BTC `
  --resolution one-hour `
  --candles 200 `
  --shadow-log outputs\shadow\btc-one-hour.jsonl `
  --intent-log outputs\shadow\btc-intents.jsonl `
  --minimum-order-usd 10.00 `
  --amount-increment-usd 0.01
```

The minimum and increment above are explicit local validation assumptions, not
a timeless statement of eToro's instrument rules; verify them against the
current instrument and account before any later execution milestone. The
command rejects stale candles, active risk reviews, an enabled kill switch,
pending orders, mirror exposure, unknown/multiple positions, short or leveraged
positions, insufficient cash, position-limit violations, and state mismatch.
Every actual intent receives a deterministic idempotency key. Duplicate keys
are not appended. The stored path and body are audit templates only: the
read-only client still has no POST/DELETE transport and `order_submitted` is
always false.

For continuous readiness observation, use the combined monitor. It updates the
shadow decision log, evaluates each completed candle once, records accepted and
rejected outcomes, and writes deduplicated order intents when appropriate:

```powershell
python -m trading_simulator etoro-readiness-loop `
  --config configs\btc_example.toml `
  --symbol BTC `
  --resolution one-hour `
  --candles 200 `
  --shadow-log outputs\shadow\btc-one-hour.jsonl `
  --readiness-log outputs\shadow\btc-readiness.jsonl `
  --intent-log outputs\shadow\btc-intents.jsonl `
  --minimum-order-usd 10.00 `
  --amount-increment-usd 0.01
```

It polls every 60 seconds but appends only one readiness evaluation per
completed candle. It halts safely on stale data, an active review, kill-switch
activation, pending orders, portfolio mismatch, unknown/multiple/copy
positions, leveraged or short exposure, and failed order constraints. API or
audit failures also halt rather than being ignored. No execution method exists.

After collecting observations, print a readiness report:

```powershell
python -m trading_simulator etoro-readiness-report `
  --readiness-log outputs\shadow\btc-readiness.jsonl
```

The current public documentation clearly identifies instrument discovery and
the unified Demo order shape, but it does not expose a stable, clearly
documented minimum-order/amount-increment response in the material verified for
this milestone. Those two constraints therefore remain explicit local inputs
and must be independently checked before any execution work.

## Explicitly armed Demo execution (Version 0.16.0)

Version 0.16 adds a separate write client that accepts only the exact unified
Demo URL `/api/v2/trading/execution/demo/orders`. It rejects the real route,
other hosts, redirects, altered payloads, shorts, sells, leverage other than 1,
and every action except an audited cash-amount long buy. It is never called by
either continuous loop.

Execution is intentionally unavailable until the readiness monitor has created
an intent. For a selected intent, the supervised command reloads the audit,
reruns current market/portfolio/control checks, requires the regenerated intent
ID and payload to match exactly, applies a separate maximum Demo order amount,
and requires the literal arming phrase. It writes `attempting` to an execution
ledger before the network request. Any ledger entry blocks automatic retry,
including after a timeout or uncertain response.

```powershell
python -m trading_simulator etoro-demo-execute-intent `
  --config configs\btc_example.toml `
  --symbol BTC `
  --resolution one-hour `
  --candles 200 `
  --shadow-log outputs\shadow\btc-one-hour.jsonl `
  --intent-log outputs\shadow\btc-intents.jsonl `
  --intent-id REVIEWED_INTENT_ID `
  --execution-ledger outputs\shadow\btc-demo-executions.jsonl `
  --minimum-order-usd 10.00 `
  --amount-increment-usd 0.01 `
  --max-demo-order-usd 1000.00 `
  --arm-demo-execution I_UNDERSTAND_THIS_SUBMITS_A_DEMO_ORDER
```

This command really submits a virtual Demo order when every check passes and
the credential has Demo Write permission. Do not run it with placeholder IDs,
do not retry a ledgered intent, and independently inspect the Demo portfolio
after any response or uncertain outcome. Real-account execution remains
structurally absent.

The variables disappear when that PowerShell process closes. The command
prints a sanitised USD summary: credit, available cash, total invested,
unrealised P&L, calculated equity, open-position count, and pending manual-order
count. It never prints credentials, raw responses, position IDs, instrument
IDs, or individual holdings. Equity follows eToro's documented formula of
available cash plus total invested plus unrealised P&L. The key should have only
`Trading — Demo: Read` and `Market Data: Read` permissions.

## Current limitations

- Market states, momentum trailing exits, staged re-entry, and latched
  structural-breakdown safeguards all affect actions.
- Series ordering and duplicates are validated. Expected-interval gap detection
  is deferred because exchanges differ in whether zero-volume candles are
  omitted; that policy needs to be configurable rather than assumed.
- CSV and one-shot read-only eToro candle downloads are supported. No live
  trading or continuous bot loop is present.
- One portfolio holds at most one long asset. Leverage, short selling,
  borrowing, margin, derivatives, and leveraged tokens are prohibited.
- Costs are configurable fixed-rate estimates. Real spreads and slippage vary
  over time and with liquidity and order size.
- Exchange tick sizes, quantity increments, minimum orders, taxes, currency
  conversion, and partial fills are not modelled yet.
- Audit bundles are persistent but do not yet include cryptographic checksums,
  source-data copies, or a database index across runs.
- Analytics values equity at candle closes; intrabar and gap drawdowns can be
  worse than reported close-to-close drawdown.
- Parameter experiments are manual, single-parameter comparisons with one
  chronological split. They do not implement optimisation, statistical
  significance, cross-validation, or walk-forward analysis.
- Completed-trade statistics count sales. More sophisticated lot-selection and
  tax accounting are outside Version 1.
- Classification currently uses closing prices, not intrabar highs/lows or
  volume. It also uses simple thresholds rather than statistical confidence.
- Safeguard volatility uses unannualised simple-return dispersion and does not
  model liquidity, order-book depth, correlations, news, or fundamental events.
