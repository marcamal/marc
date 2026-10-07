"""Assistant Agent - the conversational front door to ATLAS.

This is the agent behind the dashboard's chat and behind OpenClaw. You ask it
something in plain language; it assembles the read-only ATLAS context, hands it
to a language model with the question, and returns the answer.

What makes it safe to point a language model at a brokerage account:

*   **It owns no capability.** It cannot place an order, change a limit,
    enable a strategy or release the kill switch, because it never receives a
    broker, a risk evaluator or a strategy registry it could call. Compare the
    Execution Agent, which does. Grep this file for `broker` - there is
    nothing.
*   **The model gets facts, not controls.** `app.ai.context` builds a text
    snapshot. No tool definitions are sent, so there is nothing for a prompt
    injection in a news headline or a symbol name to invoke.
*   **Its answers are text.** Nothing in ATLAS parses the model's output. A
    reply saying "I have sold your position" would be a lie the UI displays,
    not an action - and the Mentor Agent's deterministic explanations, drawn
    from the same events, are what the operator is told to trust.

Where the money is: each answer costs tokens. The agent tracks spend per
session against `ATLAS_AI_SESSION_BUDGET_USD` and refuses once the budget is
gone, rather than quietly running up a bill.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agents.base import Agent
from app.ai.base import (
    AIError,
    AIMessage,
    AIProvider,
    AIRequestFailed,
    AIUnavailable,
)
from app.ai.context import ContextBuilder
from app.ai.factory import build_provider
from app.events.types import Topics

if TYPE_CHECKING:
    from app.runtime import AtlasRuntime

#: How many turns of history to send. A trading question rarely needs more,
#: and every extra turn is tokens on every subsequent request.
MAX_HISTORY_TURNS = 12

#: Hard cap on one question, before it reaches the model. Stops an accidental
#: paste of a 200KB log from becoming an expensive request.
MAX_QUESTION_CHARS = 4_000

#: One question at a time per process. The assistant is a single operator
#: talking to their own machine; serialising means a stuck request cannot be
#: multiplied by an impatient refresh.
_REQUEST_TIMEOUT_SECONDS = 120.0

#: Starter questions for an empty chat. Chosen to teach the system's shape:
#: each one lands on a different part of ATLAS.
SUGGESTED_QUESTIONS: tuple[str, ...] = (
    "What is in my account right now, and what is it worth?",
    "Explain my risk limits as if I were new to this. What is each one protecting me from?",
    "Has anything been blocked by the Risk Officer, and why?",
    "What did the scanner rank today, and what would make one of those a trade?",
    "Which agents are running, and what is each one doing?",
    "Am I in paper mode? How would I know if I were not?",
    "If I add 100 euro a week for ten years, what does that realistically become?",
    "What should I learn next to get better at this?",
)


@dataclass
class Conversation:
    """One chat thread, kept in memory only.

    Not persisted on purpose: the journal is for decisions and their reasons,
    and a chat log full of half-formed questions would dilute it. A restart
    starts a fresh conversation.
    """

    id: str
    messages: list[AIMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def add(self, role: str, content: str) -> None:
        self.messages.append(AIMessage(role=role, content=content))  # type: ignore[arg-type]
        self.updated_at = datetime.now(UTC)
        # Trim from the front, keeping whole turns, so the first message
        # remains a user message - which the API requires.
        excess = len(self.messages) - MAX_HISTORY_TURNS * 2
        if excess > 0:
            del self.messages[:excess]
            while self.messages and self.messages[0].role != "user":
                del self.messages[0]


@dataclass(frozen=True, slots=True)
class Answer:
    """What `ask()` returns. A superset of `AIResponse` with ATLAS metadata."""

    text: str
    provider: str
    model: str
    grounded: bool
    conversation_id: str
    degraded: bool = False
    cost_usd: float | None = None
    session_cost_usd: float = 0.0
    budget_remaining_usd: float | None = None
    context_chars: int = 0
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.text,
            "provider": self.provider,
            "model": self.model,
            "grounded": self.grounded,
            "degraded": self.degraded,
            "conversation_id": self.conversation_id,
            "cost_usd": self.cost_usd,
            "session_cost_usd": round(self.session_cost_usd, 4),
            "budget_remaining_usd": (
                round(self.budget_remaining_usd, 4)
                if self.budget_remaining_usd is not None
                else None
            ),
            "context_chars": self.context_chars,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


class AssistantAgent(Agent):
    """Answers the operator's questions from ATLAS's own state."""

    agent_type = "assistant"
    inputs = [Topics.PORTFOLIO_SNAPSHOT, Topics.RISK_DECISION, Topics.SCANNER_RESULTS]
    outputs: list[str] = []  # Text to a human. Nothing on the bus acts on it.
    tools = ["ai_provider", "atlas_context"]
    subscriptions: list[str] = []  # Pull-based: it reads state when asked.

    def __init__(
        self,
        runtime: AtlasRuntime,
        provider: AIProvider | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.runtime = runtime
        # Injectable so tests can pass a stub and never touch the network.
        self.provider: AIProvider = provider or build_provider(runtime.settings)
        self.context_builder = ContextBuilder(runtime)

        self.conversations: dict[str, Conversation] = {}
        self.questions_answered = 0
        self.refusals = 0
        self.session_cost_usd = 0.0
        #: Subtracted from the provider's running total, so resetting the
        #: budget here does not require mutating the provider.
        self._cost_offset = 0.0
        self.last_question: str | None = None
        self.last_answered_at: datetime | None = None
        self.last_error: str | None = None

        self._lock = asyncio.Lock()

    async def on_start(self) -> None:
        if self.provider.available and self.provider.name != "deterministic":
            self.info(f"assistant ready: {self.provider.model} via {self.provider.name}")
        else:
            self.info(
                "assistant ready with deterministic answers (no AI model configured). "
                "Add ANTHROPIC_API_KEY to trading/.env for conversational answers."
            )

    # ------------------------------------------------------------------ #
    # budget
    # ------------------------------------------------------------------ #

    @property
    def budget_usd(self) -> float:
        return float(self.runtime.settings.ai_session_budget_usd)

    @property
    def budget_remaining_usd(self) -> float | None:
        """None means unlimited (budget set to 0)."""
        if self.budget_usd <= 0:
            return None
        return max(0.0, self.budget_usd - self.session_cost_usd)

    @property
    def budget_exhausted(self) -> bool:
        remaining = self.budget_remaining_usd
        return remaining is not None and remaining <= 0

    def reset_budget(self) -> None:
        """Clear the session spend counter. Called from the dashboard."""
        self.session_cost_usd = 0.0
        self._cost_offset = self._provider_total_usd()
        self.info("assistant session cost reset to zero")

    def _provider_total_usd(self) -> float | None:
        """What the provider says it has spent in total, if it tracks that."""
        total = self.provider.status().get("session_cost_usd")
        return float(total) if isinstance(total, (int, float)) else None

    def _sync_cost(self, response_cost: float | None) -> None:
        """Update the session total from the single authoritative source.

        A provider that counts its own spend is trusted over adding up
        per-response costs, because the streaming path never sees a per-call
        figure. `_cost_offset` makes "reset" work against that running total
        without reaching into the provider's counters.
        """
        total = self._provider_total_usd()
        if total is not None:
            self.session_cost_usd = round(max(0.0, total - self._cost_offset), 6)
        elif response_cost:
            self.session_cost_usd = round(self.session_cost_usd + response_cost, 6)

    # ------------------------------------------------------------------ #
    # conversations
    # ------------------------------------------------------------------ #

    def conversation(self, conversation_id: str) -> Conversation:
        existing = self.conversations.get(conversation_id)
        if existing is not None:
            return existing
        created = Conversation(id=conversation_id)
        self.conversations[conversation_id] = created
        # A personal dashboard does not need unbounded chat threads, and an
        # unbounded dict is a slow leak in a process that runs for days.
        if len(self.conversations) > 20:
            oldest = min(self.conversations.values(), key=lambda c: c.updated_at)
            del self.conversations[oldest.id]
        return created

    def reset(self, conversation_id: str) -> None:
        self.conversations.pop(conversation_id, None)

    # ------------------------------------------------------------------ #
    # the one public operation
    # ------------------------------------------------------------------ #

    async def ask(
        self,
        question: str,
        *,
        conversation_id: str = "default",
        sections: set[str] | None = None,
    ) -> Answer:
        """Answer one question. Never raises for an expected failure.

        An unavailable model, a rate limit or an exhausted budget comes back as
        a `degraded` answer that explains itself, because the chat should say
        what went wrong rather than render a red box.
        """
        cleaned = question.strip()
        if not cleaned:
            raise ValueError("the question is empty")
        if len(cleaned) > MAX_QUESTION_CHARS:
            cleaned = cleaned[:MAX_QUESTION_CHARS] + "\n[truncated]"

        self.last_question = cleaned
        self.set_task(f"answering: {cleaned[:60]}")

        if self.budget_exhausted:
            self.refusals += 1
            return self._degraded(
                conversation_id,
                "The AI budget for this session is spent "
                f"(${self.session_cost_usd:.2f} of ${self.budget_usd:.2f}).\n\n"
                "Raise ATLAS_AI_SESSION_BUDGET_USD in trading/.env, or reset the "
                "counter from the Assistant page. Nothing else in ATLAS is "
                "affected - the dashboard, the agents and the risk checks do not "
                "use the AI at all.",
            )

        conversation = self.conversation(conversation_id)
        # The question is NOT added to the conversation yet. A failed turn must
        # not leave a user message with no reply behind it: the next request
        # would then send two consecutive user turns, so the model would answer
        # the two questions merged together, and every later request would pay
        # to resend a question that was never answered. The history is only
        # updated once an answer comes back - the same rule `ask_stream` uses.
        history = [*conversation.messages, AIMessage(role="user", content=cleaned)]
        context = self.context_builder.build(include=sections)
        started = datetime.now(UTC)

        async with self._lock:
            try:
                response = await asyncio.wait_for(
                    self.provider.complete(
                        history,
                        context=context,
                        max_tokens=self.runtime.settings.ai_max_tokens,
                        effort=self.runtime.settings.effective_ai_effort,
                    ),
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                self.last_error = "the model did not answer within 120 seconds"
                self.warn(f"assistant request timed out: {cleaned[:60]}")
                return self._degraded(
                    conversation_id,
                    "The model did not answer within two minutes. That is usually a "
                    "slow network rather than a problem with ATLAS. Try again, or "
                    "ask something shorter.",
                )
            except AIUnavailable as exc:
                self.last_error = str(exc)
                self.warn(f"AI provider unavailable: {exc}")
                return self._degraded(conversation_id, str(exc))
            except (AIRequestFailed, AIError) as exc:
                self.last_error = str(exc)
                self.warn(f"AI request failed: {exc}")
                return self._degraded(conversation_id, str(exc))

        conversation.add("user", cleaned)
        conversation.add("assistant", response.text)
        self.questions_answered += 1
        self.last_answered_at = datetime.now(UTC)
        self.last_error = None
        self.confidence = 1.0 if response.grounded else 0.3
        self.heartbeat()
        self.set_task("idle")

        cost = response.usage.cost_usd
        self._sync_cost(cost)

        return Answer(
            text=response.text,
            provider=response.provider,
            model=response.model,
            grounded=response.grounded,
            conversation_id=conversation_id,
            cost_usd=cost,
            session_cost_usd=self.session_cost_usd,
            budget_remaining_usd=self.budget_remaining_usd,
            context_chars=len(context),
            elapsed_seconds=(datetime.now(UTC) - started).total_seconds(),
        )

    def _degraded(self, conversation_id: str, reason: str) -> Answer:
        """An answer that says why there is no answer.

        Marked `grounded=False` so the UI can label it, and the text is the
        reason rather than an apology.
        """
        return Answer(
            text=reason,
            provider=self.provider.name,
            model=self.provider.model,
            grounded=False,
            degraded=True,
            conversation_id=conversation_id,
            session_cost_usd=self.session_cost_usd,
            budget_remaining_usd=self.budget_remaining_usd,
        )

    # ------------------------------------------------------------------ #
    # the briefing
    # ------------------------------------------------------------------ #

    async def daily_briefing(self) -> Answer:
        """A short written summary of where things stand.

        Used by the Command Center's insight panel and by OpenClaw's
        `atlas_briefing` tool, so one implementation serves both.
        """
        return await self.ask(
            "Give me a short briefing on where ATLAS and my account stand right "
            "now. Cover: the trading mode, what the account holds and what it is "
            "worth, anything the Risk Officer blocked, whether any agent is "
            "unhealthy, and the single most useful thing for me to look at next. "
            "Use the figures from the context. Six sentences at most.",
            conversation_id="briefing",
        )

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def detail(self) -> dict[str, Any]:
        return {
            "provider": self.provider.status(),
            "questions_answered": self.questions_answered,
            "refusals": self.refusals,
            "conversations": len(self.conversations),
            "last_question": self.last_question,
            "last_answered_at": (
                self.last_answered_at.isoformat() if self.last_answered_at else None
            ),
            "last_error": self.last_error,
            "session_cost_usd": round(self.session_cost_usd, 4),
            "budget_usd": self.budget_usd,
            "budget_remaining_usd": self.budget_remaining_usd,
            "budget_exhausted": self.budget_exhausted,
            "suggested_questions": list(SUGGESTED_QUESTIONS),
            "capabilities": {
                "can_read_account": True,
                "can_explain_risk": True,
                # Spelled out rather than implied. This dict is rendered on the
                # Assistant page so the operator can see the boundary.
                "can_place_orders": False,
                "can_change_risk_limits": False,
                "can_enable_strategies": False,
                "can_release_kill_switch": False,
                "holds_broker_credentials": False,
            },
        }

    # ------------------------------------------------------------------ #
    # streaming
    # ------------------------------------------------------------------ #

    async def ask_stream(
        self,
        question: str,
        *,
        conversation_id: str = "default",
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield `(kind, payload)` pairs as the answer is produced.

        `kind` is one of `chunk`, `error` or `done`. The route turns these
        into server-sent events. Streaming matters here because a thorough
        answer at high effort can take half a minute, and a chat that shows
        nothing for thirty seconds looks broken.

        The conversation is only updated once the stream completes, so an
        interrupted answer does not leave a truncated assistant turn in the
        history that every later request would pay to resend.
        """
        cleaned = question.strip()
        if not cleaned:
            raise ValueError("the question is empty")
        if len(cleaned) > MAX_QUESTION_CHARS:
            cleaned = cleaned[:MAX_QUESTION_CHARS] + "\n[truncated]"

        if self.budget_exhausted:
            self.refusals += 1
            yield (
                "error",
                f"The AI budget for this session is spent "
                f"(${self.session_cost_usd:.2f} of ${self.budget_usd:.2f}). "
                f"Reset it on the Assistant page or raise "
                f"ATLAS_AI_SESSION_BUDGET_USD.",
            )
            return

        self.last_question = cleaned
        self.set_task(f"answering: {cleaned[:60]}")

        conversation = self.conversation(conversation_id)
        history = [*conversation.messages, AIMessage(role="user", content=cleaned)]
        context = self.context_builder.build()
        collected: list[str] = []

        async with self._lock:
            try:
                async for chunk in self.provider.stream(
                    history,
                    context=context,
                    max_tokens=self.runtime.settings.ai_max_tokens,
                    effort=self.runtime.settings.effective_ai_effort,
                ):
                    collected.append(chunk)
                    yield ("chunk", chunk)
            except (AIUnavailable, AIRequestFailed, AIError) as exc:
                self.last_error = str(exc)
                self.warn(f"assistant stream failed: {exc}")
                yield ("error", str(exc))
                return

        text = "".join(collected).strip()
        if not text:
            yield ("error", "the model returned nothing")
            return

        conversation.add("user", cleaned)
        conversation.add("assistant", text)
        self.questions_answered += 1
        self.last_answered_at = datetime.now(UTC)
        self.last_error = None
        self.heartbeat()
        self.set_task("idle")

        # The provider accumulated its own usage during the stream; read the
        # total back from it rather than guessing from the text length.
        self._sync_cost(None)

        yield ("done", f"{self.session_cost_usd:.4f}")
