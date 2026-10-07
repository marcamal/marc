# ATLAS

**A personal, local-first AI trading and investment operating system.**

ATLAS is a modular system of specialised agents — a scanner, a technical
analyst, a risk officer, an execution desk, a portfolio manager and a mentor —
that research markets, propose trades, check them against hard risk limits, and
place them on **Alpaca paper trading**.

It runs entirely on your own PC. Nothing is hosted, nothing is shared.

> **Paper trading only.** Live trading is deliberately hard to switch on and
> cannot be enabled from the user interface. See
> [Switching modes](#switching-modes).
>
> This is a research and learning tool, not financial advice. Most retail day
> traders lose money. Nothing in this repository has been proven profitable.

---

## Table of contents

1. [What it does today](#what-it-does-today)
2. [How it is put together](#how-it-is-put-together)
3. [Install — Windows, step by step](#install--windows-step-by-step)
4. [Getting your Alpaca paper keys](#getting-your-alpaca-paper-keys)
5. [Running ATLAS](#running-atlas)
6. [Using the dashboard](#using-the-dashboard)
7. [Configuration](#configuration)
8. [Safety](#safety)
9. [Switching modes](#switching-modes)
10. [Running the tests](#running-the-tests)
11. [Troubleshooting](#troubleshooting)
12. [Roadmap](#roadmap)

---

## What it does today

Phase 1 is complete and working:

| Capability | Status |
|---|---|
| Alpaca paper trading connection (account, positions, orders, clock) | ✅ |
| Historical bars, latest quotes, snapshots | ✅ |
| Single websocket market-data service with auto-reconnect | ✅ |
| Trade-update stream (fills, cancels, rejections) | ✅ |
| Internal async event bus | ✅ |
| Agent framework + supervisor with health monitoring | ✅ |
| Market scanner with ranked candidates | ✅ |
| Multi-timeframe technical analysis | ✅ |
| Risk officer with veto power over every order | ✅ |
| Execution agent with duplicate-order protection | ✅ |
| Portfolio agent with broker reconciliation | ✅ |
| Mentor agent that explains what happened and why | ✅ |
| Global kill switch (file-based **and** in the UI) | ✅ |
| One example strategy (disabled by default) | ✅ |
| SQLite database + decision audit trail | ✅ |
| React/TypeScript dashboard | ✅ |
| 419 automated tests | ✅ |
| Backtesting engine | Phase 2 |
| News / fundamentals / sentiment / macro agents | Phase 3 |
| Live trading | Only after months of paper results |

**It can also run with no Alpaca account at all.** Without credentials it uses a
built-in simulator, so you can explore the whole system today while your broker
account is being verified.

---

## How it is put together

```
                      ┌──────────────────────────┐
   Alpaca WebSocket ──▶│   Market Data Service    │   ONE connection, not one
                      └────────────┬─────────────┘   per agent
                                   │ normalised events
                      ┌────────────▼─────────────┐
                      │   Internal Event Bus     │
                      └────────────┬─────────────┘
         ┌──────────────┬──────────┼──────────┬──────────────┐
         ▼              ▼          ▼          ▼              ▼
     Scanner       Technical   Portfolio   Database       Dashboard
         │              │                                (websocket)
         └──────┬───────┘
                ▼
          Strategy Agent  ── produces ──▶  TradeProposal
                                                │
                                                ▼
                                        ┌───────────────┐
                                        │  RISK OFFICER │  ◀── VETO POWER
                                        └───────┬───────┘
                                                │ approved only
                                                ▼
                                        ┌───────────────┐
                                        │   EXECUTION   │  ◀── the ONLY component
                                        └───────┬───────┘      that may place orders
                                                ▼
                                             ALPACA
```

The important rules, enforced in code and covered by tests:

* A strategy **may never** call the broker. It returns proposals, nothing else.
* Nothing reaches Alpaca without a current approval from the Risk Officer.
* If the Risk Officer is not running, the Execution agent refuses to act.
* Risk limits are deterministic Python. **No AI model can evaluate or override
  a risk rule** — an LLM may propose a trade and explain a rejection, but it has
  no path to the order API.
* Every submission carries a deterministic `client_order_id`, so a network
  retry can never open a second position.

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### Project layout

```
trading/
├── backend/
│   ├── app/
│   │   ├── agents/        the agent framework and the nine agents
│   │   ├── api/           FastAPI routes
│   │   ├── brokers/       Alpaca adapter + offline simulator
│   │   ├── config/        settings (.env) and YAML validation
│   │   ├── core/          logging and decision tracing
│   │   ├── database/      SQLAlchemy models, recorder, queries
│   │   ├── events/        the internal event bus
│   │   ├── execution/     order tracking and idempotency
│   │   ├── market_data/   streaming service, indicators, scanner maths
│   │   ├── models/        the shared domain vocabulary
│   │   ├── portfolio/     snapshots and reconciliation
│   │   ├── risk/          the rules engine and the kill switch
│   │   ├── strategies/    strategy framework + one example
│   │   └── main.py        the entry point
│   └── tests/             419 tests
├── frontend/              React + TypeScript dashboard
├── config/                risk.yaml, scanner.yaml, strategies.yaml, agents.yaml
├── scripts/               Windows and make helpers
├── docs/                  ARCHITECTURE.md, ROADMAP.md
├── data/                  the SQLite database and the kill switch file
└── logs/                  structured logs
```

---

## Install — Windows, step by step

You need two things installed once: **Python** and **Node.js**.

### 1. Install Python 3.12 or newer

1. Go to <https://www.python.org/downloads/>
2. Download the Windows installer.
3. Run it. **Tick "Add python.exe to PATH"** on the first screen — this matters,
   and it is easy to miss.
4. Click *Install Now*.

Check it worked. Open **PowerShell** (press Windows, type `powershell`, Enter):

```powershell
python --version
```

You should see `Python 3.12.x` or higher.

### 2. Install Node.js 20 or newer

1. Go to <https://nodejs.org/> and download the **LTS** version.
2. Run the installer and accept the defaults.

Check it:

```powershell
node --version
```

### 3. Set up ATLAS

In PowerShell, go to the `trading` folder and run the setup script:

```powershell
cd path\to\trading
.\scripts\setup.ps1
```

That one command:

* creates a Python virtual environment in `backend\.venv`
* installs the Python packages
* installs the frontend packages
* creates your `.env` file from `.env.example`

If PowerShell refuses to run the script, it is blocking unsigned scripts. Allow
them for your user with:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

then run `.\scripts\setup.ps1` again.

> **Prefer not to use scripts?** `docs/MANUAL_SETUP.md` has the individual
> commands.

---

## Getting your Alpaca paper keys

ATLAS works without them (on the simulator), but this is how you connect for
real.

1. Sign up at <https://alpaca.markets/> and verify your email.
2. Log in to <https://app.alpaca.markets/>.
3. **Switch the dashboard to Paper.** There is a toggle in the top-left of the
   sidebar, marked *Live* / *Paper*. Make sure it reads **Paper**.
   This is the single most common mistake — paper keys and live keys are
   different keys, and generating them in the wrong mode gives you keys ATLAS
   will reject.
4. In the right-hand panel find **API Keys** and click **Generate New Key**.
5. You will see:
   * **Key ID** — something like `PK7XXXXXXXXXXXXXXXXX`
   * **Secret Key** — a longer string

   **The secret is shown once.** Copy both now.

6. Open `trading\.env` in Notepad and paste them in:

   ```ini
   ALPACA_API_KEY=PK7XXXXXXXXXXXXXXXXX
   ALPACA_SECRET_KEY=your_secret_here
   ```

7. Save the file and restart the backend.

The header of the dashboard will change from `SIMULATED` to `PAPER MODE` and
the **ALPACA** light will turn green.

> `.env` is already in `.gitignore`. Never commit it, never paste those keys
> into a chat, an issue, or a screenshot.

### A note on the free market-data plan

Alpaca's free plan gives you:

* real-time data from **IEX only** (about 2.5% of US volume)
* **30 symbols** on one websocket connection
* full-market (SIP) history, but **not** the most recent 15 minutes

ATLAS is built around those limits rather than pretending they do not exist: it
defaults to the IEX feed, caps streaming at 30 symbols, and tells you on the
System page when symbols have been dropped. The default watchlist is 30 symbols
for exactly this reason.

If you later subscribe to Algo Trader Plus ($99/month), set
`ATLAS_HAS_PAID_DATA_PLAN=true` and `ATLAS_STOCK_DATA_FEED=sip` in `.env`.
You almost certainly do not need this yet.

---

## Running ATLAS

### The easy way — one icon

1. Open the `trading` folder in Explorer.
2. Double-click **`Create Desktop Icon.bat`** — once, ever.
3. An **ATLAS** icon appears on your Desktop.

From now on: **double-click the ATLAS icon.**

That one icon does everything — the first-time setup if it has not run yet,
starts the backend, starts the dashboard, waits until both are really
responding, and opens your browser at the right page.

A black window stays open while ATLAS runs. It shows:

```
   ATLAS IS RUNNING

   Dashboard :  http://localhost:5173
   API docs  :  http://127.0.0.1:8000/docs
   Mode      :  PAPER  (simulated data — no Alpaca keys yet)

   Press  Q  to stop ATLAS.
   Press  D  to reopen the dashboard.
```

Press **Q**, or just close that window, and everything stops cleanly.

> Don't want a Desktop icon? Double-click **`Start ATLAS.bat`** inside the
> `trading` folder instead. It is the same thing.
>
> Want it on the taskbar? Right-click the Desktop icon → *Pin to Taskbar*.

**The very first start takes a few minutes** — it is downloading Python and
Node packages. Every start after that takes a few seconds.

### The manual way — two windows

If you prefer to see the backend and the dashboard separately:

```powershell
# Window 1
cd path\to\trading
.\scripts\start-backend.ps1

# Window 2
cd path\to\trading
.\scripts\start-frontend.ps1
```

Then open <http://localhost:5173>. `Ctrl+C` stops each one.

### The equivalent raw commands

```powershell
# backend
cd trading\backend
.venv\Scripts\activate
uvicorn app.main:app --host 127.0.0.1 --port 8000

# frontend
cd trading\frontend
npm run dev
```

The backend's interactive API documentation is at
<http://127.0.0.1:8000/docs> — a good way to explore what ATLAS exposes.

---

## Using the dashboard

| Page | What it is for |
|---|---|
| **Command Center** | The page to leave open. Portfolio value, P&L, buying power, positions, top candidates, watchlist, agent activity, orders and live events. |
| **Scanner** | The ranked universe with every metric, and what was filtered out and why. |
| **Agents** | The pipeline diagram and a card per agent: what it is doing, its inputs and outputs, its logs. Start and stop agents here. |
| **Strategies** | Enable or disable a strategy for this session, and inspect its parameters. |
| **Portfolio** | Positions, exposure by correlation group, equity curve, full order history. |
| **Risk** | Every active limit, the hard ceilings, recent verdicts — and the **Trade Checker**. |
| **Journal** | Every lesson and explanation, searchable. |
| **System** | Connection health, detected broker capabilities, logs, live event feed. |

### Start here: the Trade Checker

On the **Risk** page there is a form that runs a hypothetical trade past the
real Risk Officer and shows every rule with its limit and the measured value.
It places no order, ever.

It is the fastest way to build intuition for what the limits actually permit.
Try entering a position ten times too large and read what comes back.

---

## Configuration

All trading behaviour lives in `config/`, not in the code.

| File | Controls |
|---|---|
| `risk.yaml` | Every risk limit, the correlation groups, the kill switch |
| `scanner.yaml` | The symbol universes, metrics, filters and ranking weights |
| `strategies.yaml` | Which strategies exist and whether they are enabled |
| `agents.yaml` | Which agents run and which start automatically |

Edit with Notepad, then restart the backend.

ATLAS validates these files at startup. A typo — `max_risk_per_trade_pc`
instead of `..._pct` — is a clear error on the first line of output, not a
silent fallback to a default you did not choose.

### The defaults are deliberately small

```yaml
max_risk_per_trade_pct: 0.5     # lose at most 0.5% of the account per trade
max_daily_loss_pct: 2.0         # stop for the day after -2%
max_position_pct: 5.0           # no position larger than 5% of equity
max_orders_per_day: 20          # circuit breaker against a runaway loop
max_leverage: 1.0               # cash account behaviour, no margin
```

Why 0.5%: ten losses in a row costs about 5% of the account, which is
recoverable. The same streak at 5% per trade costs roughly 40%, which usually
is not.

---

## Safety

ATLAS has several independent layers. They are independent on purpose — any one
of them failing does not open the gate.

**1. Paper by default.** Live trading needs three separate environment flags
*and* a restart. There is no API route and no button that can enable it.

**2. The Risk Officer has veto power.** Every proposal passes roughly 25
deterministic checks. The rules are plain Python with no model in the loop.

**3. Hard ceilings above the config.** Even if `risk.yaml` is edited to
something reckless, compiled-in constants clamp it — `max_risk_per_trade_pct`
can never exceed 5%, leverage can never exceed 2.0. Config is input, not
authority.

**4. The kill switch.** Two ways to stop everything:

* the red button in the dashboard header, or
* create an empty file called `KILL_SWITCH` in the `data` folder.

The file works even if the API is down or the UI is broken:
right-click in `data` → New → Text Document → name it `KILL_SWITCH`.
Delete it to resume.

It engages automatically on a daily-loss breach, a drawdown breach, a dead data
feed, a broker reconciliation mismatch, or five consecutive order rejections.

By default it blocks new orders and cancels resting ones. It does **not** sell
your positions unless you explicitly set `flatten_positions: true` — market-
selling the whole book during an outage turns a paper problem into a realised
loss.

**5. Duplicate-order protection.** Every order carries a deterministic id. After
an ambiguous network failure ATLAS asks the broker "did my order land?" rather
than retrying blindly.

**6. Fail closed.** Stale quote, missing market clock, unreachable broker,
disagreement with the broker about positions — all of these block trading rather
than proceeding on a guess.

**7. Your keys stay on your machine.** The browser talks to the ATLAS backend,
and the backend talks to Alpaca. Credentials never reach frontend JavaScript.

---

## Switching modes

ATLAS runs in one of four modes: `paper`, `live`, `backtest`, `replay`.

**Paper (the default)** — orders go to Alpaca's simulator. No real money.

**Live** requires all three of these in `.env`, and a restart:

```ini
ATLAS_TRADING_MODE=live
ATLAS_LIVE_TRADING_ENABLED=true
ATLAS_MANUAL_LIVE_CONFIRMATION=I_UNDERSTAND_THE_RISK
```

Set two of the three and ATLAS runs in paper mode and tells you which gate is
closed. The confirmation phrase must match exactly.

Do not do this until a strategy has months of paper results that beat simply
holding the core ETFs. The System page shows the state of each gate.

---

## Running the tests

```powershell
.\scripts\test.ps1
```

or:

```powershell
cd trading\backend
.venv\Scripts\activate
python -m pytest -q
```

419 tests, roughly 20 seconds. They never touch the network and never place a
brokerage order — everything runs against the built-in simulator, and a fixture
strips Alpaca credentials from the environment so the suite cannot reach a
broker even by accident.

`backend/tests/test_risk_bypass.py` is worth reading. It deliberately tries to
smuggle bad trades past the Risk Officer — oversized positions, stops on the
wrong side of entry, stale quotes, naked shorts, expired proposals — and asserts
that every one is refused.

---

## Troubleshooting

**Start here: double-click `Check Setup.bat`.**

It verifies Python, Node, the project files, the installed packages, your
Alpaca key, the kill switch and the ports — and tells you exactly what is
wrong and how to fix it. Most problems below are diagnosed by it automatically.

**`python` is not recognised**
Python was installed without "Add to PATH". Re-run the installer, choose
*Modify*, and tick it.

**PowerShell says running scripts is disabled**
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**Nothing happens when I double-click the ATLAS icon**
The window may have opened and closed too fast to read. Open the `trading`
folder and double-click `Start ATLAS.bat` directly — it pauses on an error so
you can read the message.

**The dashboard says BACKEND UNREACHABLE**
The backend is not running, or crashed. Look for the minimised window in your
taskbar, or just close the ATLAS window and start it again.

**`Alpaca rejected the credentials`**
Almost always live keys in a paper config. Go back to the Alpaca dashboard,
switch the toggle to **Paper**, generate new keys, and update `.env`.

**`your market data plan does not cover this request`**
You asked for the `sip` feed on the free plan. Set
`ATLAS_STOCK_DATA_FEED=iex` in `.env`.

**Trading is blocked and I do not know why**
Check the dashboard header. If the kill switch is engaged, the banner says why.
Also check whether `data\KILL_SWITCH` exists and delete it.

**Nothing is trading**
Expected. Every strategy ships disabled, and there are two switches:
`strategies_globally_enabled` in `config/strategies.yaml`, and the individual
strategy's own flag. Both must be on.

**Port 8000 or 5173 already in use**
Another copy is running. Close the other window, or change `ATLAS_PORT` in
`.env`.

---

## Roadmap

Phase 1 (this release) is the foundation. Next:

1. **Backtesting engine** — replay historical bars through the same strategy
   code the live system runs, with costs, spread and slippage.
2. **Replay mode** — feed recorded market data through the real event bus so
   agents behave exactly as they would live.
3. **News and fundamentals agents** — with sources and timestamps on every claim.
4. **The AI research layer** — a provider-agnostic interface so research and
   explanation can use a model, while risk and execution stay deterministic.
5. **The Agent Office** — the animated visualisation, once the system beneath it
   is proven.

Full detail and reasoning: [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Honest notes

* No strategy here has been backtested. The one example exists to prove the
  pipeline works end to end, not to make money.
* Most retail day traders lose money over time. The realistic goal of a system
  like this is to make you a better, more disciplined investor — and to stop you
  making the expensive mistakes — not to beat the market.
* Germany taxes realised capital gains at roughly 26.4% including
  Solidaritätszuschlag. Frequent trading is expensive in a way that backtests
  ignore. Buying and holding broad ETFs is a hard benchmark to beat.
* Paper results are optimistic. Real fills are worse than simulated ones.

---

*Not financial advice. Past performance does not guarantee future results.*
