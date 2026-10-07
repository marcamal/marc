# atlas-mcp — ATLAS from anywhere

This is a small bridge that lets an AI assistant read your ATLAS trading
system. Point OpenClaw at it and you can ask about your account from WhatsApp,
Telegram, Signal, Discord or a terminal:

> **you:** how is my account doing?
> **assistant:** Paper mode, simulated broker. Equity $100,000, all cash, no
> open positions. Nothing blocked, 7 of 10 agents running, market open.

> **you:** would 10 shares of SPY with a stop at 580 pass my rules?
> **assistant:** Rejected — max_order_notional: order notional $5,850 exceeds
> the $2,000 limit. Everything else passed. No order was placed.

> **you:** stop everything
> **assistant:** KILL SWITCH ENGAGED. No new order can be placed. It can only
> be released from the ATLAS dashboard, not from here.

---

## The one thing to understand

The bridge is **deliberately asymmetric**:

> **You can stop trading from your phone. You cannot start it.**

| | |
|---|---|
| Read the account, positions, P&L, orders, equity curve | ✅ |
| Read the system: mode, agents, risk limits, kill switch | ✅ |
| Read research: scanner, journal, mentor explanations, quotes | ✅ |
| Check a hypothetical trade against every risk rule | ✅ |
| Ask the ATLAS assistant a question | ✅ |
| **Engage the kill switch** (emergency stop) | ✅ |
| Place, modify or cancel an order | ❌ |
| Change a risk limit | ❌ |
| Enable or disable a strategy | ❌ |
| **Release** the kill switch | ❌ |
| See your Alpaca API keys | ❌ |

Engaging the kill switch is allowed because the cost of stopping
unnecessarily is a halted paper account, while the cost of being unable to
stop is unbounded. Releasing it is not, because that decision deserves a
human in front of the dashboard who can see the whole system state.

### How it is enforced

Not by a prompt asking the model to behave. By three layers of code:

1. **`atlas_mcp/permissions.py`** is an allowlist of `(method, path)` pairs.
   Anything not on it is refused before a socket is opened. There is also a
   `FORBIDDEN` list checked *first*, so adding something to the allowlist by
   mistake still cannot enable an order.
2. **The bridge is a separate process** that talks to ATLAS over localhost
   HTTP. It never imports the backend, so it has no reference to the broker,
   the risk engine or the event bus — there is nothing to reach past the API
   into.
3. **ATLAS itself has no order endpoint.** Only the Execution Agent may send
   an order, and only on a decision published by the Risk Officer. The bridge
   could not place a trade even if the allowlist let it try.

`atlas-mcp/tests/test_permissions.py` asserts all of this, including that the
allowlist has exactly one mutating entry. If someone adds a second, the test
names it.

---

## Install

The bridge gets its **own** virtual environment, separate from the backend's.
It is meant to be a small isolated process, and keeping the environments apart
means it cannot accidentally import ATLAS internals.

```bash
cd trading/atlas-mcp
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

Check it works — this needs the ATLAS backend running:

```bash
# Windows
.venv\Scripts\python -m pytest
# macOS / Linux
.venv/bin/python -m pytest
```

92 tests, no network needed.

---

## Wire it into OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) is a self-hosted personal
assistant that speaks MCP. Add ATLAS as a server in its config:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "C:\\Users\\marca\\Desktop\\1ATLAS-trading-system\\trading\\atlas-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "atlas_mcp"],
      "cwd": "C:\\Users\\marca\\Desktop\\1ATLAS-trading-system\\trading\\atlas-mcp",
      "env": {
        "ATLAS_MCP_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

On macOS or Linux the same thing with forward slashes:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "/path/to/trading/atlas-mcp/.venv/bin/python",
      "args": ["-m", "atlas_mcp"],
      "cwd": "/path/to/trading/atlas-mcp"
    }
  }
}
```

`python -m atlas_mcp` rather than a console script: it works without guessing
where pip put the executable.

The same config block works in **Claude Desktop** (`claude_desktop_config.json`)
and in any other MCP client.

### Sanity order

1. Start ATLAS (`trading/scripts/Start-ATLAS.ps1`). The bridge is useless
   without it, and will say so clearly rather than hanging.
2. Start OpenClaw.
3. Ask it: *"use the atlas_capabilities tool and tell me what it can do."*

---

## The tools

| Tool | What it does |
|---|---|
| `atlas_capabilities` | What this bridge can and cannot do. Ask for this first. |
| `atlas_status` | Mode, broker, agents, kill switch, market — the whole picture |
| `atlas_account` | Equity, cash, buying power |
| `atlas_positions` | Open positions with unrealised P&L |
| `atlas_portfolio` | Exposure, realised and unrealised P&L |
| `atlas_orders` | Recent orders (`open` / `closed` / `all`) |
| `atlas_scanner` | The scanner's current ranking |
| `atlas_agents` | Every agent and what it is doing |
| `atlas_risk_limits` | Configured limits and the hard-coded ceilings above them |
| `atlas_risk_decisions` | What was blocked recently, and by which rule |
| `atlas_market` | Market open/closed and the next bell |
| `atlas_quote` | Latest quote for one symbol, with its age |
| `atlas_watchlist` | The watchlist with prices and changes |
| `atlas_journal` | The trade journal |
| `atlas_explanations` | The Mentor Agent's explanations of recent events |
| `atlas_risk_check` | Evaluate a hypothetical trade. **Places nothing.** |
| `atlas_ask` | Ask the ATLAS assistant in plain language |
| `atlas_briefing` | A short written check-in on where things stand |
| `atlas_engage_kill_switch` | **Emergency stop.** The only state change. |

Annotations are set honestly — `read_only_hint` on the reads,
`destructive_hint` on the kill switch — so a well-behaved client can ask you
before firing it.

---

## Why it only talks to 127.0.0.1

`ATLAS_MCP_BASE_URL` is checked at startup and refuses any non-loopback host.
A bridge that a model could point at an arbitrary URL would be an open proxy
sitting in front of a brokerage dashboard. There is no legitimate reason for
it to be remote — ATLAS runs on the same machine — so the check is a hard
failure, not a warning.

If you genuinely want ATLAS reachable from your phone over the internet, do it
with a VPN (Tailscale or WireGuard) between your phone and your PC, and leave
this bridge talking to localhost. That way the tunnel is authenticated and the
bridge's threat model does not change.

---

## What this does not give you

It does not make ATLAS trade better, and it does not make the AI a trader. The
model on the other end can read numbers and explain them. Every decision that
risks money still happens in deterministic code, behind the Risk Officer's
veto, with you at the dashboard.

That is the trade being made here: a genuinely useful read-only window, in
exchange for giving up nothing.
