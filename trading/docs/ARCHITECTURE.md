# ATLAS — Architecture

This document explains how ATLAS is built and, more importantly, **why**. Where
a decision could reasonably have gone another way, the reasoning is recorded so
it can be revisited deliberately rather than by accident.

---

## 1. Design principles

**Reliability before features.** A trading system that is wrong occasionally is
worse than one that does less and is right. Every ambiguous situation — a stale
quote, an unreachable broker, a disagreement about positions — blocks trading
rather than guessing.

**Determinism where money is at stake.** Research and explanation can be fuzzy.
Risk arithmetic cannot. The risk engine is pure Python with no model, no
randomness and no IO, which means it can be tested exhaustively.

**Config is input, not authority.** `risk.yaml` can only make ATLAS more
conservative. Compiled-in ceilings clamp anything reckless, loudly.

**One definition of each concept.** Where a definition was duplicated — the set
of valid timeframes lived in two places and the two disagreed — it has been
pulled into one module. A simulator more permissive than the real adapter means
a bug that passes tests and fails in production.

**Observable by construction.** Every decision carries a trace id from the bar
that triggered it to the order that resulted, so "why did ATLAS buy this?" is a
query rather than an archaeology project.

---

## 2. The big picture

```
                          ┌───────────────────────────┐
      Alpaca WebSocket ───▶│   MarketDataService       │
      (ONE connection)     │   normalise · cache ·     │
                          │   detect staleness        │
                          └─────────────┬─────────────┘
                                        │
                          ┌─────────────▼─────────────┐
                          │     Internal Event Bus    │
                          │  topics · wildcards ·     │
                          │  per-subscriber queues    │
                          └─────────────┬─────────────┘
       ┌───────────┬────────────┬───────┴────┬────────────┬───────────┐
       ▼           ▼            ▼            ▼            ▼           ▼
   Scanner    Technical    Portfolio      Mentor      Recorder    Dashboard
       │           │            │                    (SQLite)    (websocket)
       └─────┬─────┘            │
             ▼                  │
      Strategy Agent            │  reconciles against the broker;
             │                  │  broker state always wins
             ▼                  │
       TradeProposal            │
             │                  │
             ▼                  ▼
      ┌──────────────────────────────┐
      │        RISK OFFICER          │   ~25 deterministic checks
      │        (VETO POWER)          │   owns the kill switch
      └──────────────┬───────────────┘
                     │ approved (or approved-reduced) only
                     ▼
      ┌──────────────────────────────┐
      │       EXECUTION AGENT        │   the ONLY component that may
      │  idempotency · retries ·     │   call submit_order()
      │  fills · rejections          │
      └──────────────┬───────────────┘
                     ▼
                  ALPACA
```

---

## 3. Layers

### 3.1 Configuration (`app/config`)

Two kinds, deliberately separated:

* **`.env`** — secrets and environment: API keys, the trading mode, ports, the
  database URL. Read through `pydantic-settings`.
* **`config/*.yaml`** — trading *behaviour*: risk limits, universes, strategy
  parameters, agent wiring. Validated into Pydantic models at startup.

The YAML models set `extra="forbid"`. A misspelled key is a hard error, because
a silently-ignored `max_risk_per_trade_pc` would mean trading at the default
limit while believing it was configured.

#### The trading mode gate

`Settings.effective_mode` is the single chokepoint deciding whether ATLAS may
touch real money. LIVE requires three independent conditions:

```
ATLAS_TRADING_MODE=live
ATLAS_LIVE_TRADING_ENABLED=true
ATLAS_MANUAL_LIVE_CONFIRMATION=I_UNDERSTAND_THE_RISK
```

Anything else — including a typo like `liv` or `LIVE ` — resolves to PAPER. No
other code computes this, and no API route can change it.

### 3.2 Events (`app/events`)

An in-process asyncio pub/sub over dotted topics with `*` wildcards.

**Why not Kafka.** ATLAS runs as one process on one PC. An in-process bus has
microsecond latency and no operational burden. The *interface* is broker-shaped,
so moving to Redis Streams or NATS later means reimplementing one class, not
rewriting every agent.

**Each subscriber owns a bounded queue and a worker task.** This is the property
that matters: a slow subscriber cannot block a fast publisher. If the journal is
busy writing to disk, market data keeps flowing; the journal's own queue fills
and drops, with a counter the System page shows. A handler that raises is logged
and its subscription survives — one buggy agent must not take down the bus.

Trace ids are restored inside the handler, so a subscriber's logs join the
publisher's decision chain automatically.

### 3.3 Models (`app/models`)

ATLAS's own vocabulary: `Bar`, `Quote`, `Order`, `Position`, `TradeProposal`,
`RiskDecision`, `Signal`, `ScannerResult`. Every agent speaks these; only
`app.brokers.*` knows what an Alpaca SDK object looks like.

`TradeProposal` is the most important type. Its validators make an incoherent
proposal impossible to construct:

* a long's stop must be below entry (a "stop" above entry triggers instantly)
* a target must be on the winning side
* quantity, entry and stop must be positive
* timestamps must be timezone-aware (a naive datetime raises later, inside the
  staleness check, which is a confusing place to debug a timezone bug)

### 3.4 Brokers (`app/brokers`)

`BrokerAdapter` and `MarketDataProvider` are abstract. Two implementations:

* **`AlpacaBroker` / `AlpacaMarketData`** — the official `alpaca-py` SDK. The
  SDK is **synchronous**, so every call goes through `asyncio.to_thread`.
  Calling it directly from the event loop would stall every agent, the
  websocket reader and the API for the duration of an HTTP round trip. That is
  the single most important implementation detail in that package.
* **`SimulatedBroker` / `SimulatedMarketData`** — fully offline, seeded and
  deterministic. ATLAS runs and the whole test suite passes with no credentials
  and no network.

#### Capability detection

ATLAS never hard-codes what an account can do. Margin, shorting, crypto,
options and fractional shares depend on the account type, the broker and the
operator's country of residence, and that set changes over time. A German
retail account does not get the same products as a US one.

`get_capabilities()` asks the account and reports what is actually available.
The UI shows `UNSUPPORTED BY CURRENT BROKER` for the rest rather than offering
something that will fail at order time. Detection failure falls back to the
*conservative* answer, never the permissive one.

#### Streaming

The SDK's `run()` calls `asyncio.run()` internally, creating its own event loop,
so it cannot be awaited from ATLAS's loop. Two options existed: call the private
`_run_forever()` coroutine in our loop, or run the public `run()` in a worker
thread and bridge messages back.

ATLAS takes the second. It uses only public SDK API, so an upgrade cannot
silently break streaming, and the cost is one `loop.call_soon_threadsafe` hop
per message — negligible against network latency.

The consequence is a hard rule: **SDK handlers execute on the stream thread, not
the event loop.** `asyncio.Queue.put_nowait` is not thread-safe, so handlers
never touch the bus directly; they marshal through `call_soon_threadsafe`.

Reconnection is layered: the SDK retries internally, and a supervisor restarts
the stream with exponential backoff if `run()` returns at all.

### 3.5 Market data (`app/market_data`)

`MarketDataService` is the sole owner of streaming. The brief required one
connection rather than one per agent, and Alpaca enforces it anyway — the free
plan permits one concurrent connection per account, so a per-agent design would
fail outright.

It warms its bar cache from REST before opening the socket. Without that, agents
starting together would see empty history and every indicator would be `None`
until enough live bars accumulated — several hours on a 5-minute timeframe.

**Staleness detection** watches for the dangerous failure: a socket that is
*connected but silent*. Every price in the system ages while the light stays
green. Only checked when a stream is actually expected, so a weekend produces no
false alarms.

#### Indicators

Implemented from scratch in pure Python. TA-Lib needs a per-platform C library,
which is a real obstacle on Windows and a bad first experience. More
importantly, these feed trading decisions, so being able to read and test every
line matters more than microseconds.

Conventions throughout: input is **oldest first**; every function returns `None`
when there is not enough data (a number computed from insufficient data is worse
than no number, because it looks trustworthy); RSI and ATR use **Wilder
smoothing**, matching charting platforms — a simple moving average of gains
gives visibly different values and a strategy tuned on one misbehaves on the
other.

`vwap()` resets per session by default, because VWAP is a session statistic and
running it across days produces a number that drifts further from the real
intraday VWAP with every day included.

### 3.6 Agents (`app/agents`)

An agent is a supervised, introspectable worker with a declared contract.
Two shapes, and an agent may use both:

* **Periodic** — set `cycle_interval_seconds`, implement `run_cycle()`.
* **Event-driven** — declare `subscriptions`, implement `handle_event()`.

Each exposes everything the brief asked for: id, name, role, status, current
task, last activity, confidence, inputs, outputs, tools, subscriptions, logs and
statistics.

**Failure policy.** An exception in `run_cycle()` is logged, counted, and the
loop continues after a backoff. One bad cycle — a transient API error, a symbol
with no data — must not silently kill an agent while the dashboard shows a green
light. Five consecutive failures move it to ERROR, which is visible.

**`__init_subclass__` guards against name collisions.** This exists because of a
real bug caught during development: a subclass defined `async def _record(...)`,
which shadowed the base class's `_record(level, message)` log helper. Every
`self.warn(...)` in that agent then created an un-awaited coroutine and the log
line vanished — risk veto messages were being silently dropped. The guard now
rejects such a subclass at import time, and only the five documented hooks are
overridable.

The ten agents:

| Agent | Role | Protected |
|---|---|---|
| Orchestrator | Coordinates, watches health, aggregates. Never trades. | |
| Market Data | Owns the websocket, publishes normalised events. | |
| Portfolio | Mirrors the broker; broker state always wins. | |
| Scanner | Ranks the universe. Never trades. | |
| Technical | Multi-timeframe analysis, produces signals. | |
| Strategy | Runs enabled strategies, publishes proposals. Never orders. | |
| **Risk** | **Veto power. Owns the kill switch.** | ✅ |
| **Execution** | **The only component that may order.** | ✅ |
| Mentor | Explains decisions and writes trade reviews. | |
| Assistant | Answers questions from read-only state. Holds nothing it could act with. | |

Risk and Execution are *protected*: the manager refuses to stop them while ATLAS
runs. Stopping Risk would leave Execution without veto authority — and
Execution independently fails closed if Risk is absent. Two mechanisms for one
invariant, because this is the one that must not break.

The agent network graph is **derived** from declared outputs and subscriptions,
so the diagram cannot drift from the real wiring.

### 3.7 Risk (`app/risk`)

The most important code in the system.

`RiskEngine.evaluate(proposal, context)` is a pure function. No IO, no
randomness, no clock reads beyond an injected `now`. The agent does the IO
(gathering account, quote, clock, capabilities) and records the verdict; the
engine does the judging.

**All rules run even after the first failure.** A partial audit trail would make
the Mentor's explanation misleading ("rejected for X" when Y and Z also failed),
and the extra work is a few dozen comparisons.

Rule groups: blocking conditions (kill switch, mode, account blocks), proposal
hygiene (required fields, freshness, expiry, stop distance, reward/risk), market
conditions (session open, data freshness, spread, liquidity), capability (asset
supported, shorting, fractional), sizing (risk per trade, position size,
notional bounds, buying power), portfolio limits (position count, total
exposure, correlated exposure, leverage), rate limits (per day, per symbol, per
strategy) and account health (daily loss, drawdown, reconciliation).

**Hard ceilings.** `ABSOLUTE_MAX_RISK_PER_TRADE_PCT = 5.0` and friends are
compiled in. A config value above a ceiling is clamped and logged as an error.

**No AI anywhere near it.** An LLM may propose a trade and explain a rejection,
but it cannot evaluate one. There is no prompt, no model call and no override
path in that module.

#### The kill switch

Two independent representations:

1. **A file** (`data/KILL_SWITCH`). Works when the API is down, the UI is
   broken, or the process is wedged. Checked live on every call, never cached —
   the whole point is that it works without ATLAS's cooperation.
2. **An in-process flag**, for programmatic triggers and the dashboard.

Either being set blocks trading. Engaging blocks new orders (always), cancels
resting orders (default on), and flattens positions (default **off**).

Flattening defaults off deliberately: market-selling a whole book during a data
outage or a flash dislocation converts a paper problem into a realised loss.

### 3.8 Execution (`app/execution`, `app/agents/execution_agent.py`)

**The duplicate-order problem.** Submitting over HTTP has three outcomes, not
two: success, failure, and *unknown*. A timeout leaves the caller unable to tell
whether the order was received. A naive retry opens a second position — the most
expensive bug an execution system can have.

The fix:

* every submission carries a deterministic `client_order_id`
* a retry reuses **the same** id, so the broker rejects the duplicate
* after an ambiguous failure the agent asks the broker "do you have an order
  with this id?", turning *unknown* into a definite answer

Retry policy distinguishes failure kinds: connection errors are retried (after a
lookup), while order rejections and auth errors are not — retrying would just
repeat the rejection. Five consecutive rejections engage the kill switch, since
that means something systematic is wrong.

Daily counters reset on the **US trading day**, not UTC midnight, which would
fall mid-session.

### 3.9 Portfolio (`app/portfolio`)

**Broker state always wins.** If ATLAS believes it holds 100 shares and Alpaca
says 50, Alpaca is right — it is the system of record. ATLAS adopts the broker's
numbers, raises a loud mismatch flag, and the `broker_reconciled` risk rule
blocks new orders until it clears, because position sizing computed from a wrong
position is worse than no decision.

A position ATLAS never opened is not necessarily a bug — a manual trade in the
Alpaca dashboard looks exactly like that — but it is worth surfacing either way.

The high water mark only ever rises; letting it fall would mask the drawdown it
exists to reveal.

### 3.10 Strategies (`app/strategies`)

A strategy is a near-pure function from market data to zero or more
`TradeProposal`s. It may not call the broker and may not bypass risk.

The same `generate()` is what the backtesting engine will call. That is how
ATLAS avoids the classic trap of a "backtest version" and a "live version"
drifting apart until the backtest is meaningless.

Three gates, all defaulting closed: the global switch, the strategy's own flag,
and the trading mode plus kill switch. Enabling from the API is session-only and
never written back to `strategies.yaml`, so an experiment cannot quietly become
permanent.

### 3.11 Database (`app/database`)

SQLAlchemy 2.0, async, SQLite by default and PostgreSQL-ready — only the URL
changes.

The SQLite PRAGMAs matter more than they look. `journal_mode=WAL` lets the API
read while an agent writes; without it, SQLite's default locking throws
"database is locked" under exactly the load ATLAS generates.

The **Recorder** subscribes to the bus rather than being called by agents. That
inversion means persistence can never slow down or break a trading decision: a
failed write is logged and dropped. Losing a journal row is acceptable; stalling
the execution path is not.

It uses **one subscription covering every topic**, not one per topic. A
subscription owns a queue and a worker, so separate subscriptions would process
concurrently — and the same order arrives on both `execution.submitted` and
`order.update`, so two workers raced to insert it and tripped the unique
constraint on `client_order_id`. One subscription serialises the writes.

No tick storage in the MVP: bars and notable events only. Retention policies
purge high-volume, low-value rows; orders, proposals, risk decisions and journal
entries are kept indefinitely, because they are the record of what ATLAS did and
why.

Money is stored as `Float`. For a personal journal of what a broker reported
that is fine; if ATLAS ever grows a tax ledger that must reconcile to the cent,
those columns become `Numeric(18, 8)`. Flagged in the roadmap rather than solved
prematurely.

### 3.12 API and dashboard

FastAPI, with the runtime on `app.state` and read through a dependency — no
module-level singleton, so tests mount the same routers against a simulated
runtime.

The browser talks only to the ATLAS backend; the backend talks to Alpaca.
Credentials never reach frontend JavaScript. A test asserts that no response
body contains a credential-shaped key.

The dashboard is React + TypeScript on Vite, with plain CSS and no component
framework. Seven flat pages behind a `useState` tab plus a `hashchange`
listener, rather than a router dependency. Polling for numbers, websocket for
events.

---

## 4. Decision traces

One `trace_id` threads through a whole chain via a `contextvar`, so it
propagates through `await` without being passed as an argument everywhere.

```
scanner      found NVDA, rank 3, relative volume 2.1
technical    score 84, EMA stack bullish, above VWAP
strategy     proposal: long 4 @ 178.20, stop 175.10, target 184.40
risk         APPROVED, risk 0.21% of equity
execution    submitted market buy, client_order_id atlas-…
broker       acknowledged, filled 4 @ 178.23
```

`GET /api/trace/{trace_id}` returns the whole chain as one timeline.

---

## 5. Failure handling

| Failure | Behaviour |
|---|---|
| Alpaca outage | Agents log and retry; risk context unavailable ⇒ reject |
| Dropped websocket | Supervisor reconnects with exponential backoff |
| Connected but silent | Staleness monitor ⇒ risk event ⇒ kill switch |
| Rate limit (429) | Treated as retryable, backed off |
| Malformed data | Conversion falls back to `None`/`UNKNOWN`, never a guess |
| Database failure | Logged; trading continues, journal degrades |
| Broker disagreement | Broker wins, mismatch flagged, trading blocked |
| Repeated rejections | Kill switch after 5 |
| Computer restart | Kill switch file persists, so trading does not silently resume |

The consistent rule is **fail closed**.

---

## 6. What is deliberately not built yet

* **Backtesting.** The interfaces are shaped for it; the engine is Phase 2.
  Building strategy logic before it can be tested out-of-sample would be
  building on sand.
* **LLM integration.** The provider interface is designed but no model is
  wired in. Phase 1 needed the deterministic core correct first.
* **News, fundamentals, sentiment, macro.** Event types, topics and database
  tables exist. The agents come once there is a backtester to evaluate whether
  their signals add anything.
* **The Agent Office visualisation.** Explicitly after the functional system,
  as the brief required.
* **Multiple brokers.** The adapter interface exists; only Alpaca is
  implemented, as instructed.
