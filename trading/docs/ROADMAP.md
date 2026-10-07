# ATLAS — Roadmap

Ordered by **value per unit of risk**, not by how interesting each item is to
build. The guiding rule: nothing that can place an order gets added until the
thing that evaluates whether it should exists.

---

## Phase 1 — Foundation ✅ *complete*

The architecture, the safety layer, and one end-to-end path proven.

- [x] Config system: `.env` + validated YAML, hard ceilings above config
- [x] Trading-mode gate with three independent live-trading flags
- [x] Structured logging with decision traces
- [x] Alpaca adapter: account, positions, orders, clock, assets, capabilities
- [x] Alpaca market data: bars, quotes, trades, snapshots
- [x] Single websocket market-data service with reconnect and staleness detection
- [x] Trade-update stream
- [x] Internal async event bus with per-subscriber backpressure
- [x] Agent framework + manager with health monitoring and protected agents
- [x] Nine agents: orchestrator, market data, portfolio, scanner, technical,
      strategy, risk, execution, mentor
- [x] Risk engine: ~25 deterministic rules, veto power, no AI in the loop
- [x] Kill switch: file-based and in-process, with automatic triggers
- [x] Execution with deterministic `client_order_id` and duplicate protection
- [x] Portfolio reconciliation where broker state wins
- [x] One example strategy, disabled by default
- [x] SQLite persistence with retention policy and audit trail
- [x] React/TypeScript dashboard, eight pages
- [x] 419 automated tests, including adversarial risk-bypass tests
- [x] Offline simulator so everything runs without credentials

---

## Phase 2 — Can we tell whether anything works?

**The single most valuable thing to build next.** Until there is a backtester,
every strategy decision is guesswork, and adding more strategies just multiplies
the guessing.

### 2.1 Backtesting engine
Replay historical Alpaca bars through the **same** `Strategy.generate()` the
live system calls. The `StrategyContext` already exists for exactly this.

Must model, because ignoring them is how backtests lie:
- transaction costs and the spread (crossing it twice is often the whole edge)
- configurable slippage
- realistic fill assumptions for limit and stop orders
- market hours, halts and missing data
- position sizing identical to the live Risk Officer's

Metrics: total and annualised return, Sharpe, Sortino, max drawdown, win rate,
profit factor, expectancy, average winner and loser, exposure, trade count.

Must prevent look-ahead bias structurally — a bar's data must be unavailable
until that bar closes — rather than relying on care.

**Definition of done:** the example strategy produces an out-of-sample equity
curve against buy-and-hold SPY over 2016–today, and the honest answer is
published even if it is "worse than holding".

### 2.2 Replay mode
Feed recorded market data through the real event bus so agents behave exactly as
they would live. The mode already exists in the enum and is blocked from
reaching a broker; it needs a historical provider behind the same interface.

Invaluable for debugging an agent interaction that only appears under live
sequencing.

### 2.3 Walk-forward and robustness
Out-of-sample windows, parameter sensitivity, Monte Carlo on trade order. A
strategy that only works on one parameter set is a curve fit.

---

## Phase 3 — More information, carefully

Only after Phase 2, so each new signal can be evaluated rather than assumed.

### 3.1 News agent
Alpaca provides a news endpoint. Associate articles with tickers, sectors and
macro themes; classify earnings, guidance, M&A, legal, regulatory, FDA, analyst
and executive changes.

**Every stored claim keeps its source URL and timestamp. Never invent news.**

### 3.2 Fundamental agent
Revenue and earnings growth, margins, free cash flow, debt, valuation multiples,
ROIC, dilution. Later: SEC EDGAR filings directly (EDGAR requires a real contact
in the User-Agent — the `SEC_USER_AGENT` variable already exists for this).

Also where **publicly disclosed** insider activity belongs: SEC Form 4
open-market purchases and cluster buys, EU directors' dealings under Art. 19
MAR, 13F changes.

> Only lawful, published information. Trading on non-public information is
> illegal under MAR Art. 14 and nothing in this system will pursue it.

### 3.3 Sentiment agent
Reddit, public social feeds, Google Trends, analyst commentary. The hard part is
separating signal from noise, and sentiment alone is never a trade signal — it
is a tie-breaker at most.

### 3.4 Macro agent
Fed and ECB policy, rates, inflation, unemployment, GDP, the yield curve, DXY,
oil, gold, VIX. FRED has a good free API.

### 3.5 Options data
Where the account supports it. Unusual options activity is a genuine signal
source, and ATLAS already detects whether options are enabled rather than
assuming.

---

## Phase 4 — The AI research layer

Deliberately late. The deterministic core had to be correct first.

### 4.1 Provider interface
A generic `AIProvider` with adapters for Anthropic, OpenAI and local models. No
deep coupling to one vendor.

### 4.2 Where a model may and may not act

**May:** summarise news and filings, generate hypotheses, classify documents,
explain a trade or a rejection in better prose than the current templates,
investigate anomalies, draft trade reviews.

**May not:** evaluate risk, size a position, place or modify an order, hold
credentials, or change its own risk rules. A model may *propose* an improvement;
a human reviews and merges it.

### 4.3 Better mentoring
The Mentor currently uses deterministic templates — correct every time, but
stiff. An LLM layered on top of the assembled facts (never instead of them)
would read far more naturally.

---

## Phase 5 — Research depth

### 5.1 Intelligence agent
Second- and third-order relationship discovery:

```
AI datacenter growth → electricity demand → grid investment
  → transformers, cooling, uranium, natural gas, copper
```

This is where domain knowledge in HVAC, cooling and electrification is a real
edge — the kind of connection a generic screener does not make.

### 5.2 More strategies
Only with backtest evidence: trend following, mean reversion, VWAP pullback,
opening-range breakout, volatility expansion, gap continuation and reversal,
relative strength, sector rotation, swing momentum.

### 5.3 Long-term investing sleeve
Weekly contributions, core ETF allocation, rebalance-by-buying rather than
selling. For a German investor this matters: realised gains are taxed at roughly
26.4%, so churn is expensive in a way backtests ignore. The core should be
bought and held, never traded by a bot.

---

## Phase 6 — Presentation

### 6.1 Professional charts
Candlesticks with indicator overlays and trade markers, so a journal entry can
be read against the chart it happened on.

### 6.2 The Agent Office
The animated visualisation from the brief: a digital office where each agent has
a workstation, the Risk Officer guards the execution room, and agents move when
handing off work.

Explicitly **presentation only**, driven by the existing event stream. The
backend stays independent of it.

---

## Phase 7 — Expansion

- Additional broker adapters (Interactive Brokers, Kraken, Coinbase)
- PostgreSQL + TimescaleDB, if time-series volume ever justifies it
- Local authentication, if ATLAS ever leaves localhost
- `Numeric` money columns, if a tax-grade ledger is needed
- Containerisation, if the setup ever becomes painful enough to warrant it

---

## The live-trading gate

Not a phase — a standing bar, to be met before any real money moves:

1. A strategy has **months** of paper results.
2. Those results beat simply holding the core ETFs, after costs and tax.
3. Out-of-sample backtests agree with the paper results.
4. The maximum drawdown is one you can actually tolerate.
5. Every failure mode has been seen and handled at least once on paper.
6. A cash account first. No margin, no shorting.
7. Position sizes that would be survivable if the system were badly wrong.

Most systems never pass step 2, and that is useful information rather than a
failure.

---

## Known debt

Recorded so it is a decision rather than an oversight:

| Item | Why it was deferred |
|---|---|
| Money stored as `Float` | Fine for a record of broker-reported numbers; becomes `Numeric` if a tax ledger is added |
| No sector classification | Correlation groups are a crude stand-in; proper GICS data arrives with the fundamental layer |
| `create_all` instead of migrations | Alembic is configured and ready; `create_all` is right until the schema changes under real data |
| Simulated fills are optimistic | Market orders fill at the quote, resting orders never fill. Realistic modelling belongs in the backtester |
| No partial-fill handling in position tracking | Partial fills are tracked on the order but position maths assumes completion |
| One strategy, unproven | Deliberate: it exists to validate the pipeline, not to make money |
