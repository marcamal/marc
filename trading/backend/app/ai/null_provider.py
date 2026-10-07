"""The provider that needs no API key, no network and no money.

This is not a stub. It is the reason ATLAS ships with an assistant that works
out of the box: it answers a question by finding the part of the live ATLAS
context that the question is about and presenting it, with a short note on how
to read it.

Three things it will not do, all deliberate:

*   It will not paraphrase. Every number it prints was computed by ATLAS.
*   It will not pretend to understand a question it cannot route. It says so
    and lists what it *can* answer.
*   It will not mention an API key as the only option without saying that
    everything in the dashboard works without one.

`grounded` is True for its answers, because they are literally the system's
own figures. That matters for the UI, which labels ungrounded output.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ai.base import ATLAS_SYSTEM_PROMPT, AIMessage, AIProvider, AIResponse

#: Question keywords mapped onto the context sections that answer them.
#: Section names must match the headings `app.ai.context` emits.
#:
#: Ordered longest-phrase-first inside each entry so "day trade" does not win
#: over "daytrade count" by accident; routing takes the best-scoring entry.
_ROUTES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        ("position", "holding", "own", "shares", "long", "short"),
        ("POSITIONS", "ACCOUNT"),
        "These are the positions ATLAS last reconciled against the broker.",
    ),
    (
        ("p&l", "pnl", "profit", "loss", "up", "down", "made", "lost", "performance"),
        ("ACCOUNT", "POSITIONS"),
        "Unrealised P&L moves with the price; realised P&L is what you actually "
        "banked. Only realised gains are taxable.",
    ),
    (
        ("account", "equity", "cash", "balance", "buying power", "how much"),
        ("ACCOUNT", "MODE"),
        "Equity is cash plus the market value of what you hold.",
    ),
    (
        ("risk", "limit", "veto", "blocked", "rejected", "ceiling", "drawdown"),
        ("RISK", "RECENT RISK DECISIONS"),
        "Risk limits are deterministic code. Nothing - including the AI - can "
        "relax them at runtime; the hard ceilings sit above your own config.",
    ),
    (
        ("kill switch", "stop", "emergency", "halt"),
        ("RISK", "MODE"),
        "The kill switch blocks every new order and cancels resting ones. It is "
        "released from the dashboard only.",
    ),
    (
        ("scan", "scanner", "opportunit", "candidate", "watchlist", "interesting"),
        ("SCANNER", "MARKET"),
        "A scanner ranking is an observation, not a trade. It becomes a trade only "
        "after a strategy defines an entry, a stop and a size, and the Risk "
        "Officer approves it.",
    ),
    (
        ("agent", "running", "health", "unhealthy", "status"),
        ("AGENTS", "MODE"),
        "Each agent is a supervised worker with a declared contract. The Agents "
        "page shows what each one is doing right now.",
    ),
    (
        ("strateg", "signal", "setup", "enabled"),
        ("STRATEGIES", "RECENT RISK DECISIONS"),
        "Strategies are disabled by default, and nothing can generate a trade "
        "proposal until you enable one.",
    ),
    (
        ("market", "open", "closed", "clock", "session", "price", "quote"),
        ("MARKET", "MODE"),
        "Outside regular hours spreads widen and volume thins, so a fill can be "
        "far from the price on screen.",
    ),
    (
        ("mode", "paper", "live", "real money", "safe", "broker", "alpaca"),
        ("MODE",),
        "Live trading needs three independent environment flags. Any ambiguity resolves to paper.",
    ),
    (
        ("order", "fill", "trade", "executed", "submitted"),
        ("RECENT ORDERS", "RECENT RISK DECISIONS"),
        "Only the Execution Agent can send an order, and only on an approved risk decision.",
    ),
)

#: What to say when nothing routes.
_FALLBACK = """\
I can answer that only from what ATLAS has measured, and I could not work out
which part of the system your question is about.

Without an AI model configured I answer by looking up live figures rather than
by reasoning, so the question has to point at something ATLAS tracks. Try one
of these:

  - What positions do I have, and what are they worth?
  - How much equity and buying power is in the account?
  - What are my risk limits, and has anything been blocked?
  - What did the scanner rank today?
  - Which agents are running?
  - Are we in paper mode?
  - Is the market open?

To get real conversational answers - explanations, follow-up questions, "why
did that happen" - add an Anthropic API key to trading/.env as
ANTHROPIC_API_KEY and restart. Everything else in ATLAS works without one."""


def _split_sections(context: str) -> dict[str, str]:
    """Parse the context block into `{heading: body}`.

    The context format is the contract between `app.ai.context` and this
    provider: lines beginning `## ` open a section. Anything before the first
    heading is ignored.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None

    for line in context.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().upper()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    return {name: "\n".join(body).strip() for name, body in sections.items()}


def _score(question: str, keywords: tuple[str, ...]) -> int:
    """How strongly a question matches a route.

    Longer keyword matches count more, so "buying power" beats a stray "how
    much" in a question that contains both.
    """
    return sum(len(word) for word in keywords if word in question)


class NullProvider(AIProvider):
    """Deterministic lookups over the ATLAS context. No model, no network."""

    name = "deterministic"

    @property
    def model(self) -> str:
        return "atlas-lookup"

    @property
    def available(self) -> bool:
        # Always. That is the point of it.
        return True

    async def complete(
        self,
        messages: Sequence[AIMessage],
        *,
        system: str = ATLAS_SYSTEM_PROMPT,
        context: str = "",
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> AIResponse:
        question = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            "",
        ).lower()

        sections = _split_sections(context)
        best: tuple[int, tuple[str, ...], str] | None = None

        for keywords, wanted, note in _ROUTES:
            score = _score(question, keywords)
            if score > 0 and (best is None or score > best[0]):
                best = (score, wanted, note)

        if best is None or not sections:
            return AIResponse(
                text=_FALLBACK,
                model=self.model,
                provider=self.name,
                grounded=True,
            )

        _, wanted, note = best
        parts: list[str] = []
        for heading in wanted:
            body = sections.get(heading)
            if body:
                parts.append(f"{heading.title()}\n{'-' * len(heading)}\n{body}")

        if not parts:
            return AIResponse(
                text=_FALLBACK,
                model=self.model,
                provider=self.name,
                grounded=True,
            )

        answer = "\n\n".join(parts)
        answer += f"\n\n{note}"
        answer += (
            "\n\n(Deterministic lookup - these are ATLAS's own figures, not a "
            "model's summary. Add ANTHROPIC_API_KEY to trading/.env for "
            "conversational answers.)"
        )

        return AIResponse(
            text=answer,
            model=self.model,
            provider=self.name,
            grounded=True,
        )

    def status(self) -> dict[str, object]:
        base = super().status()
        base["note"] = (
            "No AI model configured. The assistant answers by looking up live "
            "ATLAS figures. Everything else in ATLAS is unaffected."
        )
        return base
