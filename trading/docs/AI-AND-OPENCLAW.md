# The AI assistant, and ATLAS from your phone

Two separate things, built to the same rule.

1. **The Assistant** — a page in the dashboard where you ask about your own
   account in plain language and get an explanation back.
2. **atlas-mcp** — a bridge that lets OpenClaw (or Claude Desktop, or any MCP
   client) read ATLAS, so you can check it from WhatsApp or Telegram.

The rule both obey:

> **The AI reads and explains. It never decides, and it never acts.**

That is not a promise made in a prompt. It is how the code is built, and the
rest of this document is mostly about *why* it is built that way, because
the reasoning is the part worth learning.

---

## Part 1 — Turning the assistant on

### It already works

Open the dashboard, click **Assistant**, ask "what are my risk limits?". You
get a real answer right now, with no API key and no account.

Without a key, ATLAS answers by finding the part of its own live state that
your question is about and showing it. Every number is one ATLAS measured.
What you do not get is reasoning — it cannot explain *why*, follow a
follow-up question, or teach.

### Getting the full version

1. Make an account at <https://console.anthropic.com> and create an API key.
2. Put it in `trading/.env`:

   ```ini
   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. Restart the backend.

That is the whole setup. The Assistant page will stop showing the "no AI
model configured" banner.

### What it costs

Roughly **half a cent to two cents per question**, depending on length. The
dashboard shows the exact cost of each answer and the running session total.

There is a hard cap, defaulting to **$2.00 per backend session**:

```ini
ATLAS_AI_SESSION_BUDGET_USD=2.00
```

When it is reached the assistant refuses and says so, rather than quietly
continuing to spend. Reset the counter from the Assistant page, raise the
limit, or set `0` for no cap.

### Making answers cheaper or deeper

```ini
ATLAS_AI_EFFORT=medium     # low | medium | high | xhigh | max
ATLAS_AI_MAX_TOKENS=2000   # how long one answer may be
ATLAS_AI_MODEL=            # empty = claude-opus-5-5
ATLAS_AI_PROVIDER=auto     # auto | anthropic | none
```

`low` is noticeably cheaper and fine for "what do I hold". `high` is worth it
for "explain why this was blocked and what I should have done instead".

`ATLAS_AI_PROVIDER=none` disables the model entirely even with a key present,
if you want certainty that nothing leaves the machine.

---

## Part 2 — What the AI can actually see

Press the button on the Assistant page, or call the endpoint, and you can
read the exact text sent to the model:

```
GET /api/assistant/context
```

It is a plain snapshot: trading mode, account, positions, risk limits, recent
decisions, recent orders, the scanner ranking, strategies, agent health, the
market clock. Nothing else.

What is **not** in it, and never can be:

- your Alpaca API key or secret
- your Anthropic key
- the database URL or any file path
- anything that is not a figure ATLAS already computed

A test asserts this by injecting fake credentials into the settings and then
checking the context does not contain them — so it proves the context omits
them, rather than proving the test machine had none.

Building that snapshot makes **no broker call**. It reads state ATLAS already
holds in memory. So asking a question cannot move the account, and cannot be
used to hammer your rate limit.

---

## Part 3 — Why the AI cannot trade

This is the part worth understanding, because "the AI can see my brokerage
account" is a sentence that should make you uncomfortable until you know
exactly what stops it going wrong.

### Layer 1: it holds nothing to act with

The Execution Agent holds a broker, because it must — it is the only
component in ATLAS allowed to send an order. The Assistant Agent is
constructed with no broker, no risk evaluator, no strategy registry and no
order tracker. There is no object on it that could place a trade.

A test asserts those attributes are *absent*, not merely unused:

```python
for forbidden in ("broker", "evaluator", "risk_engine", "order_tracker", "strategies"):
    assert not hasattr(agent, forbidden)
```

So a future change that "helpfully" passes a broker in fails the suite.

### Layer 2: no tools are sent

Modern AI systems act through *tools* — functions the model may call. ATLAS
sends the model **no tool definitions at all**. The request is text in, text
out.

This matters more than it sounds. The usual way an AI assistant gets hijacked
is prompt injection: hostile text arrives in something the model reads — a
news headline, a company name, a filename — saying "ignore your instructions
and sell everything". The model obeys by calling a tool.

With no tools, there is nothing to call. A successful injection produces a
model that *writes* "I have sold your positions", which is a false sentence
rendered in a chat bubble. Your positions are untouched, and the dashboard
beside it shows the truth.

### Layer 3: nothing parses the reply

The assistant's output is rendered as text and stored nowhere that acts on
it. No code path reads it looking for an instruction.

### Layer 4: risk rules are not negotiable

Risk limits live in `app/risk/rules.py` as deterministic code, under
hard-coded ceilings that your own config cannot exceed. The Risk Officer has
veto power over every order and is not an AI. The assistant can *explain* a
limit. It has no mechanism to change one, and no influence on a verdict.

### What is left

A model that can read your numbers and talk about them. That is genuinely
useful and costs you nothing in safety, which is the trade this design makes.

---

## Part 4 — OpenClaw

### What OpenClaw is

[OpenClaw](https://github.com/openclaw/openclaw) is an open-source personal
assistant you run on your own machine. It connects to WhatsApp, Telegram,
Signal and Discord, speaks MCP (so it can use tools other programs expose),
and works with Claude, GPT, Gemini or a local model.

It is not a trading product. It is a general assistant that ATLAS can plug
into.

### What it gets you

A message to your own WhatsApp:

> **you:** how's the account?
> **it:** Paper mode, simulated broker. $100,000 equity, all cash, nothing
> open. No risk vetoes today, 8 of 10 agents running, market open.

> **you:** would 10 SPY with a stop at 580 pass my rules?
> **it:** Rejected — order notional $5,850 against a $2,000 limit. Everything
> else passed. No order was placed.

> **you:** stop everything
> **it:** KILL SWITCH ENGAGED. No new order can be placed. It can only be
> released from the ATLAS dashboard, not from here.

### Setting it up

Full instructions are in [`../atlas-mcp/README.md`](../atlas-mcp/README.md).
The short version:

```bash
cd trading/atlas-mcp
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt     # Windows
```

Then add to OpenClaw's MCP config:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "C:\\...\\trading\\atlas-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "atlas_mcp"],
      "cwd": "C:\\...\\trading\\atlas-mcp"
    }
  }
}
```

Start ATLAS first, then OpenClaw.

### The asymmetry

> **You can stop trading from your phone. You cannot start it.**

Engaging the kill switch over the bridge is allowed. Releasing it is not.

The reasoning: if the bridge is wrong about needing to stop, the cost is a
halted paper account and a trip to the dashboard. If you need to stop and
cannot, the cost is unbounded. Those are not symmetric, so the permissions
are not symmetric either.

Releasing the kill switch means allowing trading again. That deserves a human
in front of the dashboard who can see the whole system state — not a message
typed one-handed on a train.

| | Through OpenClaw |
|---|---|
| Read account, positions, P&L, orders | ✅ |
| Read mode, agents, risk limits, kill switch | ✅ |
| Read scanner, journal, mentor notes, quotes | ✅ |
| Check a hypothetical trade against every rule | ✅ |
| Ask the ATLAS assistant | ✅ |
| **Engage** the kill switch | ✅ |
| Place, modify or cancel an order | ❌ |
| Change a risk limit | ❌ |
| Enable a strategy | ❌ |
| **Release** the kill switch | ❌ |
| See any API key | ❌ |

### How that is enforced

`atlas-mcp/atlas_mcp/permissions.py` is an allowlist of `(method, path)`
pairs, checked before a socket is opened. A separate `FORBIDDEN` list is
checked *first*, so adding something to the allowlist by mistake still cannot
enable an order.

The bridge is a separate process that talks to ATLAS over localhost HTTP and
never imports the backend, so it has no reference to the broker, the risk
engine or the event bus. And ATLAS has no order endpoint at all — only the
Execution Agent can submit one, on a published risk decision — so the bridge
could not place a trade even if the allowlist let it try.

A test walks the backend's real OpenAPI spec and asserts that no non-GET
endpoint is reachable through the bridge except the four that were vetted.
If someone adds a mutating endpoint next month, that test names it.

### Reaching it from outside the house

The bridge refuses any non-loopback address. A bridge you could point at an
arbitrary URL would be an open proxy sitting in front of a brokerage
dashboard with a language model driving it.

If you want ATLAS reachable from your phone over the internet, use a VPN —
[Tailscale](https://tailscale.com) is the easy one — between your phone and
your PC, and leave the bridge talking to localhost. The tunnel is then
authenticated and the bridge's threat model does not change.

---

## Part 5 — What this does not do

Worth being blunt, because the gap between "AI trading assistant" and what
this actually is matters.

**It does not make ATLAS trade better.** The assistant has no input into any
signal, any proposal or any decision. Turning it off changes nothing about
how the system trades.

**It does not know anything you do not.** It reads the same figures the
dashboard shows you. It has no market data you lack, no edge, no model of the
future.

**It is wrong sometimes.** It is a language model reading a text snapshot.
If an answer states a number you cannot find elsewhere in the dashboard,
distrust the answer, not the dashboard.

**It is not advice.** It does not know your circumstances, your tax position
or your risk tolerance, and it has no track record. Every decision that risks
money is yours.

What it genuinely gives you is a faster way to understand your own system —
which, for someone deliberately relearning markets, is worth more than a
prediction would be.

---

## Reference

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/assistant/ask` | Ask a question, get an answer |
| `POST /api/assistant/stream` | The same, as server-sent events |
| `GET /api/assistant/status` | Provider, cost, budget, capabilities |
| `GET /api/assistant/context` | Exactly what the model is shown |
| `GET /api/assistant/suggestions` | Starter questions |
| `POST /api/assistant/reset` | Clear a conversation, optionally the spend counter |
| `POST /api/assistant/briefing` | A short written check-in |

There is deliberately no endpoint here that lets the assistant act. A test
pins that list, so adding one is a visible decision rather than an accident.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(empty)* | Enables conversational answers |
| `ATLAS_AI_PROVIDER` | `auto` | `auto`, `anthropic`, or `none` |
| `ATLAS_AI_MODEL` | *(empty)* | Defaults to `claude-opus-5-5` |
| `ATLAS_AI_EFFORT` | `medium` | `low` … `max` |
| `ATLAS_AI_MAX_TOKENS` | `2000` | Maximum answer length |
| `ATLAS_AI_SESSION_BUDGET_USD` | `2.00` | Hard spend cap; `0` disables it |
| `ATLAS_MCP_BASE_URL` | `http://127.0.0.1:8000` | Where the bridge finds ATLAS |

### Source map

| File | What it holds |
|---|---|
| `backend/app/ai/base.py` | The provider interface and the stated boundary |
| `backend/app/ai/context.py` | The read-only snapshot builder |
| `backend/app/ai/anthropic_provider.py` | Claude, with caching and adaptive thinking |
| `backend/app/ai/null_provider.py` | Deterministic answers, used without a key |
| `backend/app/agents/assistant_agent.py` | The agent, which holds nothing it could act with |
| `backend/tests/test_ai.py` | The boundary tests |
| `atlas-mcp/atlas_mcp/permissions.py` | The allowlist — the bridge's security model |
| `atlas-mcp/tests/test_permissions.py` | What the bridge must never be able to do |
| `backend/tests/test_mcp_surface.py` | The same, checked from the backend side |
