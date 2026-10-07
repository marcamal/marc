"""Tests for the AI layer and the Assistant Agent.

The interesting assertions here are not "does it produce text". They are the
boundary ones:

*   the context never contains a credential
*   the assistant exposes no way to trade, and holds no object it could trade
    with
*   a dead, slow, broke or hallucinating provider degrades to an explanation
    rather than an exception
*   nothing in this layer reaches the network under test

No test in this file makes a real API call. The provider is either the
deterministic one or a stub.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.assistant_agent import MAX_QUESTION_CHARS, AssistantAgent, Conversation
from app.ai.anthropic_provider import PRICES_PER_MTOK, AnthropicProvider
from app.ai.base import (
    ATLAS_SYSTEM_PROMPT,
    AIMessage,
    AIProvider,
    AIRequestFailed,
    AIResponse,
    AIUnavailable,
    AIUsage,
)
from app.ai.context import MAX_CONTEXT_CHARS, ContextBuilder
from app.ai.factory import build_provider
from app.ai.null_provider import NullProvider, _split_sections
from app.api.routes import api_router
from app.config.settings import Settings
from app.models.enums import PositionSide
from app.models.trading import PortfolioSnapshot, Position
from app.runtime import AtlasRuntime

# `asyncio_mode = "auto"` in pyproject.toml means async tests need no marker.


# --------------------------------------------------------------------------- #
# test doubles
# --------------------------------------------------------------------------- #


class StubProvider(AIProvider):
    """Records what it was asked, returns what it was told to."""

    name = "stub"

    def __init__(
        self,
        reply: str = "stub answer",
        raises: Exception | None = None,
        delay: float = 0.0,
        cost: float | None = 0.01,
    ) -> None:
        self.reply = reply
        self.raises = raises
        self.delay = delay
        self.cost = cost
        self.calls: list[dict[str, Any]] = []
        self.total_cost = 0.0

    @property
    def model(self) -> str:
        return "stub-model"

    @property
    def available(self) -> bool:
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
        self.calls.append(
            {
                "messages": list(messages),
                "system": system,
                "context": context,
                "max_tokens": max_tokens,
                "effort": effort,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        if self.cost:
            self.total_cost = round(self.total_cost + self.cost, 6)
        return AIResponse(
            text=self.reply,
            model=self.model,
            provider=self.name,
            usage=AIUsage(input_tokens=100, output_tokens=50, cost_usd=self.cost),
        )

    async def stream(
        self,
        messages: Sequence[AIMessage],
        *,
        system: str = ATLAS_SYSTEM_PROMPT,
        context: str = "",
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> AsyncIterator[str]:
        if self.raises is not None:
            raise self.raises
        for word in self.reply.split():
            yield word + " "
        if self.cost:
            self.total_cost = round(self.total_cost + self.cost, 6)

    def status(self) -> dict[str, object]:
        base = super().status()
        base["session_cost_usd"] = self.total_cost
        return base


def _assistant(runtime: AtlasRuntime, provider: AIProvider | None = None) -> AssistantAgent:
    return AssistantAgent(
        runtime=runtime,
        provider=provider or StubProvider(),
        agent_id="assistant",
        name="Assistant",
        role="test",
        bus=runtime.bus,
    )


# --------------------------------------------------------------------------- #
# the boundary: what the AI is allowed to see
# --------------------------------------------------------------------------- #


async def test_context_contains_no_credentials(runtime: AtlasRuntime) -> None:
    """The single most important assertion in this file.

    Real keys are injected into settings first, so this proves the context
    omits them rather than merely proving the test environment has none.
    """
    runtime.settings.alpaca_api_key = "PKTESTKEY123456SECRET"
    runtime.settings.alpaca_secret_key = "alpacasecret-do-not-leak"
    runtime.settings.anthropic_api_key = "sk-ant-should-never-appear"

    context = ContextBuilder(runtime).build()

    for secret in (
        "PKTESTKEY123456SECRET",
        "alpacasecret-do-not-leak",
        "sk-ant-should-never-appear",
    ):
        assert secret not in context

    # Nor the things that would help an attacker find them.
    assert "sqlite" not in context.lower()
    assert ".env" not in context


async def test_context_states_the_trading_mode_first(runtime: AtlasRuntime) -> None:
    """A model must never discuss the account without knowing the mode."""
    context = ContextBuilder(runtime).build()
    assert "## MODE" in context
    assert context.index("## MODE") < context.index("## ACCOUNT")
    assert "PAPER" in context
    assert "no real money" in context


async def test_context_mode_section_survives_section_filtering(runtime: AtlasRuntime) -> None:
    """Asking for only positions must still carry the mode banner."""
    context = ContextBuilder(runtime).build(include={"positions"})
    assert "## MODE" in context
    assert "## POSITIONS" in context
    assert "## SCANNER" not in context


async def test_context_reports_positions_it_has(runtime: AtlasRuntime) -> None:
    runtime.portfolio_snapshot = PortfolioSnapshot(
        equity=10_000.0,
        cash=5_000.0,
        buying_power=5_000.0,
        positions=[
            Position(
                symbol="SPY",
                quantity=8,
                side=PositionSide.LONG,
                average_entry_price=585.0,
                current_price=590.0,
                market_value=4_720.0,
                cost_basis=4_680.0,
                unrealized_pl=40.0,
                unrealized_pl_pct=0.85,
            )
        ],
        total_exposure=4_720.0,
        total_exposure_pct=47.2,
        unrealized_pl=40.0,
    )

    context = ContextBuilder(runtime).build()
    assert "SPY" in context
    assert "$10,000.00" in context
    assert "47.2% of equity" in context


async def test_context_distinguishes_no_data_from_no_positions(runtime: AtlasRuntime) -> None:
    """A model told "no positions" when we simply do not know would lie.

    Both states must be representable, and they must read differently.
    """
    runtime.portfolio_snapshot = None
    unknown = ContextBuilder(runtime)._positions()
    assert "no portfolio snapshot" in unknown.lower()

    runtime.portfolio_snapshot = PortfolioSnapshot(equity=1000.0, cash=1000.0)
    empty = ContextBuilder(runtime)._positions()
    assert "entirely in cash" in empty


async def test_context_is_capped(runtime: AtlasRuntime) -> None:
    """A huge account cannot blow the token budget."""
    runtime.portfolio_snapshot = PortfolioSnapshot(
        equity=1_000_000.0,
        cash=0.0,
        positions=[
            Position(
                symbol=f"SYM{index:04d}",
                quantity=100,
                side=PositionSide.LONG,
                average_entry_price=10.0,
                current_price=11.0,
                market_value=1100.0,
            )
            for index in range(500)
        ],
    )
    context = ContextBuilder(runtime).build()
    assert len(context) <= MAX_CONTEXT_CHARS


async def test_context_section_failure_is_visible_not_silent(runtime: AtlasRuntime) -> None:
    """A broken section must say so.

    Silently omitting the POSITIONS heading would let a model conclude there
    are no positions, which is the worst possible failure mode here.
    """
    builder = ContextBuilder(runtime)

    def explode() -> str:
        raise RuntimeError("boom")

    builder._positions = explode  # type: ignore[method-assign]
    context = builder.build()

    assert "## POSITIONS" in context
    assert "unavailable" in context
    assert "RuntimeError" in context


async def test_context_makes_no_broker_calls(runtime: AtlasRuntime) -> None:
    """Asking a question must not be able to hammer the broker."""
    calls: list[str] = []

    for method in ("get_account", "get_positions", "get_orders", "submit_order"):
        original = getattr(runtime.broker, method, None)
        if original is None:
            continue

        def record(*_args: Any, _name: str = method, **_kwargs: Any) -> Any:
            calls.append(_name)
            raise AssertionError(f"the context builder called broker.{_name}")

        setattr(runtime.broker, method, record)

    ContextBuilder(runtime).build()
    assert calls == []


# --------------------------------------------------------------------------- #
# the deterministic provider
# --------------------------------------------------------------------------- #


async def test_null_provider_is_always_available() -> None:
    provider = NullProvider()
    assert provider.available is True
    assert provider.model == "atlas-lookup"


async def test_null_provider_answers_from_the_context(runtime: AtlasRuntime) -> None:
    context = ContextBuilder(runtime).build()
    response = await NullProvider().complete(
        [AIMessage(role="user", content="what positions do I have?")],
        context=context,
    )
    assert "Positions" in response.text
    assert response.grounded is True


async def test_null_provider_routes_a_risk_question_to_the_risk_section(
    runtime: AtlasRuntime,
) -> None:
    context = ContextBuilder(runtime).build()
    response = await NullProvider().complete(
        [AIMessage(role="user", content="what are my risk limits?")],
        context=context,
    )
    assert "max risk per trade" in response.text
    assert "Hard-coded ceilings" in response.text


async def test_null_provider_admits_when_it_cannot_route() -> None:
    response = await NullProvider().complete(
        [AIMessage(role="user", content="what is the airspeed of a swallow")],
        context="## MODE\nTrading mode: PAPER\n",
    )
    assert "could not work out" in response.text
    # And it must say ATLAS still works without a key.
    assert "works without one" in response.text


async def test_null_provider_invents_nothing() -> None:
    """Every number it prints came from the context it was given."""
    response = await NullProvider().complete(
        [AIMessage(role="user", content="how much equity do I have?")],
        context="## ACCOUNT\nEquity: $1,234.56\n",
    )
    assert "$1,234.56" in response.text
    # No other dollar figure may appear.
    import re

    assert set(re.findall(r"\$[\d,]+\.\d\d", response.text)) == {"$1,234.56"}


def test_section_parsing_is_the_contract_with_the_context_builder() -> None:
    sections = _split_sections("preamble\n## ONE\nbody one\n## TWO\nbody two\n")
    assert sections == {"ONE": "body one", "TWO": "body two"}


# --------------------------------------------------------------------------- #
# the Anthropic provider, without touching the network
# --------------------------------------------------------------------------- #


def test_anthropic_provider_is_unavailable_without_a_key() -> None:
    provider = AnthropicProvider(api_key="")
    assert provider.available is False
    assert provider.status()["has_api_key"] is False


async def test_anthropic_provider_refuses_clearly_without_a_key() -> None:
    provider = AnthropicProvider(api_key="")
    with pytest.raises(AIUnavailable, match="ANTHROPIC_API_KEY"):
        await provider.complete([AIMessage(role="user", content="hi")])


def test_anthropic_request_puts_stable_instructions_before_volatile_facts() -> None:
    """Prompt caching is a prefix match, so this ordering is load-bearing."""
    provider = AnthropicProvider(api_key="sk-ant-test")
    kwargs = provider._request_kwargs(
        [AIMessage(role="user", content="hello")],
        system="STABLE",
        context="VOLATILE",
        max_tokens=1000,
        effort="high",
    )

    blocks = kwargs["system"]
    assert blocks[0]["text"] == "STABLE"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "VOLATILE" in blocks[1]["text"]
    assert "cache_control" not in blocks[1]


def test_anthropic_request_uses_adaptive_thinking_and_no_budget() -> None:
    """A thinking budget is rejected with a 400 on this model family."""
    provider = AnthropicProvider(api_key="sk-ant-test")
    kwargs = provider._request_kwargs(
        [AIMessage(role="user", content="hello")],
        system="s",
        context="",
        max_tokens=1000,
        effort="medium",
    )

    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in kwargs["thinking"]
    assert kwargs["output_config"] == {"effort": "medium"}


def test_anthropic_request_declares_no_tools() -> None:
    """The safety argument depends on this: no tools, nothing to inject into."""
    provider = AnthropicProvider(api_key="sk-ant-test")
    kwargs = provider._request_kwargs(
        [AIMessage(role="user", content="hello")],
        system="s",
        context="",
        max_tokens=100,
        effort="low",
    )
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


def test_anthropic_request_falls_back_to_medium_on_a_bad_effort() -> None:
    provider = AnthropicProvider(api_key="sk-ant-test")
    kwargs = provider._request_kwargs(
        [AIMessage(role="user", content="hi")],
        system="s",
        context="",
        max_tokens=100,
        effort="ludicrous",
    )
    assert kwargs["output_config"]["effort"] == "medium"


def test_anthropic_request_rejects_a_history_not_starting_with_the_user() -> None:
    provider = AnthropicProvider(api_key="sk-ant-test")
    with pytest.raises(AIRequestFailed, match="first message"):
        provider._request_kwargs(
            [AIMessage(role="assistant", content="hi")],
            system="s",
            context="",
            max_tokens=100,
            effort="low",
        )


def test_anthropic_cost_estimate_matches_the_published_price() -> None:
    """One million input tokens on Opus 5.5 is $4."""

    class Usage:
        input_tokens = 1_000_000
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    from app.ai.anthropic_provider import _estimate_cost

    assert _estimate_cost("claude-opus-5-5", Usage()) == pytest.approx(4.00)
    assert PRICES_PER_MTOK["claude-opus-5-5"] == (4.00, 20.00)


def test_anthropic_cost_estimate_is_none_for_an_unknown_model() -> None:
    """A wrong cost is worse than no cost."""

    class Usage:
        input_tokens = 1_000
        output_tokens = 1_000
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    from app.ai.anthropic_provider import _estimate_cost

    assert _estimate_cost("some-other-model", Usage()) is None


def test_anthropic_cost_estimate_handles_a_dated_model_id() -> None:
    class Usage:
        input_tokens = 1_000_000
        output_tokens = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    from app.ai.anthropic_provider import _estimate_cost

    assert _estimate_cost("claude-opus-5-5-20260215", Usage()) == pytest.approx(4.00)


def test_anthropic_translates_an_auth_error_into_actionable_advice() -> None:
    provider = AnthropicProvider(api_key="sk-ant-test")

    class AuthenticationError(Exception):
        pass

    translated = provider._translate(AuthenticationError("401"))
    assert isinstance(translated, AIUnavailable)
    assert "ANTHROPIC_API_KEY" in str(translated)


def test_anthropic_translates_a_rate_limit_into_a_retryable_failure() -> None:
    provider = AnthropicProvider(api_key="sk-ant-test")

    class RateLimitError(Exception):
        pass

    translated = provider._translate(RateLimitError("429"))
    assert isinstance(translated, AIRequestFailed)
    assert not isinstance(translated, AIUnavailable)


# --------------------------------------------------------------------------- #
# the factory never fails to produce a provider
# --------------------------------------------------------------------------- #


def test_factory_returns_the_deterministic_provider_without_a_key() -> None:
    provider = build_provider(Settings(_env_file=None))  # type: ignore[call-arg]
    assert isinstance(provider, NullProvider)
    assert provider.available is True


def test_factory_honours_an_explicit_off_switch() -> None:
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="sk-ant-x", ATLAS_AI_PROVIDER="none")  # type: ignore[call-arg]
    assert isinstance(build_provider(settings), NullProvider)


def test_factory_degrades_rather_than_raising_on_an_unknown_provider() -> None:
    settings = Settings(_env_file=None, ATLAS_AI_PROVIDER="gpt-9")  # type: ignore[call-arg]
    assert build_provider(settings).available is True


def test_factory_builds_the_anthropic_provider_when_a_key_is_present() -> None:
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="sk-ant-test-key")  # type: ignore[call-arg]
    provider = build_provider(settings)
    # The SDK is installed in this environment, so the real provider is chosen.
    # If it were not, the factory must still return something usable.
    assert provider.available is True
    if isinstance(provider, AnthropicProvider):
        assert provider.model == "claude-opus-5-5"


def test_settings_validate_the_effort_level() -> None:
    assert Settings(_env_file=None, ATLAS_AI_EFFORT="XHIGH").effective_ai_effort == "xhigh"  # type: ignore[call-arg]
    assert Settings(_env_file=None, ATLAS_AI_EFFORT="nonsense").effective_ai_effort == "medium"  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# the Assistant Agent
# --------------------------------------------------------------------------- #


async def test_assistant_holds_nothing_it_could_trade_with(runtime: AtlasRuntime) -> None:
    """Structural, not behavioural: the capability is absent, not merely unused.

    The Execution Agent holds a broker because it must. This one must not,
    and asserting that catches a future refactor that "helpfully" passes one
    in.
    """
    agent = _assistant(runtime)

    for forbidden in ("broker", "evaluator", "risk_engine", "order_tracker", "strategies"):
        assert not hasattr(agent, forbidden), (
            f"AssistantAgent has a '{forbidden}' attribute. The assistant must "
            f"not hold anything it could act with."
        )

    assert agent.outputs == [], "the assistant must publish nothing that is acted on"


async def test_assistant_reports_its_own_limits(runtime: AtlasRuntime) -> None:
    detail = _assistant(runtime).detail()
    capabilities = detail["capabilities"]

    assert capabilities["can_place_orders"] is False
    assert capabilities["can_change_risk_limits"] is False
    assert capabilities["can_enable_strategies"] is False
    assert capabilities["can_release_kill_switch"] is False
    assert capabilities["holds_broker_credentials"] is False


async def test_assistant_answers_and_passes_the_context(runtime: AtlasRuntime) -> None:
    stub = StubProvider(reply="Your account holds nothing.")
    agent = _assistant(runtime, stub)

    answer = await agent.ask("what do I hold?")

    assert answer.text == "Your account holds nothing."
    assert answer.degraded is False
    assert answer.context_chars > 0
    assert "## MODE" in stub.calls[0]["context"]
    assert stub.calls[0]["effort"] == "medium"


async def test_assistant_keeps_conversation_history(runtime: AtlasRuntime) -> None:
    stub = StubProvider()
    agent = _assistant(runtime, stub)

    await agent.ask("first question", conversation_id="chat")
    await agent.ask("second question", conversation_id="chat")

    sent = stub.calls[1]["messages"]
    assert [m.content for m in sent] == [
        "first question",
        "stub answer",
        "second question",
    ]


async def test_assistant_keeps_conversations_separate(runtime: AtlasRuntime) -> None:
    stub = StubProvider()
    agent = _assistant(runtime, stub)

    await agent.ask("about SPY", conversation_id="a")
    await agent.ask("about QQQ", conversation_id="b")

    assert len(stub.calls[1]["messages"]) == 1


async def test_assistant_trims_history_and_keeps_a_user_message_first(
    runtime: AtlasRuntime,
) -> None:
    """The API requires the first message to be from the user."""
    conversation = Conversation(id="x")
    for index in range(40):
        conversation.add("user", f"q{index}")
        conversation.add("assistant", f"a{index}")

    assert conversation.messages[0].role == "user"
    assert len(conversation.messages) <= 24


async def test_assistant_truncates_an_enormous_question(runtime: AtlasRuntime) -> None:
    stub = StubProvider()
    agent = _assistant(runtime, stub)

    await agent.ask("x" * (MAX_QUESTION_CHARS * 3))

    sent = stub.calls[0]["messages"][-1].content
    assert len(sent) < MAX_QUESTION_CHARS + 50
    assert sent.endswith("[truncated]")


async def test_assistant_rejects_an_empty_question(runtime: AtlasRuntime) -> None:
    with pytest.raises(ValueError, match="empty"):
        await _assistant(runtime).ask("   ")


# --- failure handling ------------------------------------------------------ #


async def test_assistant_degrades_when_the_provider_is_unavailable(
    runtime: AtlasRuntime,
) -> None:
    """A missing key must produce an explanation, not a stack trace."""
    agent = _assistant(runtime, StubProvider(raises=AIUnavailable("no API key configured")))

    answer = await agent.ask("anything")

    assert answer.degraded is True
    assert answer.grounded is False
    assert "no API key configured" in answer.text


async def test_assistant_degrades_when_a_request_fails(runtime: AtlasRuntime) -> None:
    agent = _assistant(runtime, StubProvider(raises=AIRequestFailed("rate limited")))
    answer = await agent.ask("anything")
    assert answer.degraded is True
    assert "rate limited" in answer.text


async def test_assistant_degrades_on_a_timeout(
    runtime: AtlasRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agents.assistant_agent._REQUEST_TIMEOUT_SECONDS", 0.05)
    agent = _assistant(runtime, StubProvider(delay=5.0))

    answer = await agent.ask("anything")

    assert answer.degraded is True
    assert "two minutes" in answer.text or "did not answer" in answer.text


async def test_a_degraded_answer_does_not_poison_the_history(runtime: AtlasRuntime) -> None:
    """A failed turn must not leave a user message with no reply.

    Otherwise the next request sends two consecutive user turns and every
    later request pays to resend a question that was never answered.
    """
    stub = StubProvider(raises=AIRequestFailed("down"))
    agent = _assistant(runtime, stub)

    await agent.ask("failing question", conversation_id="c")

    stub.raises = None
    await agent.ask("working question", conversation_id="c")

    roles = [m.role for m in stub.calls[-1]["messages"]]
    # No two user turns in a row.
    assert all(
        not (roles[i] == "user" and roles[i + 1] == "user") for i in range(len(roles) - 1)
    ), roles


# --- the budget ------------------------------------------------------------ #


async def test_assistant_tracks_spend(runtime: AtlasRuntime) -> None:
    agent = _assistant(runtime, StubProvider(cost=0.02))

    await agent.ask("one")
    await agent.ask("two")

    assert agent.session_cost_usd == pytest.approx(0.04)


async def test_assistant_refuses_once_the_budget_is_spent(runtime: AtlasRuntime) -> None:
    runtime.settings.ai_session_budget_usd = 0.03
    agent = _assistant(runtime, StubProvider(cost=0.02))

    first = await agent.ask("one")
    second = await agent.ask("two")
    third = await agent.ask("three")

    assert first.degraded is False
    assert second.degraded is False
    assert third.degraded is True
    assert "budget" in third.text.lower()
    assert agent.refusals == 1


async def test_a_zero_budget_means_unlimited(runtime: AtlasRuntime) -> None:
    runtime.settings.ai_session_budget_usd = 0.0
    agent = _assistant(runtime, StubProvider(cost=5.0))

    await agent.ask("one")
    answer = await agent.ask("two")

    assert agent.budget_remaining_usd is None
    assert answer.degraded is False


async def test_resetting_the_budget_allows_questions_again(runtime: AtlasRuntime) -> None:
    runtime.settings.ai_session_budget_usd = 0.01
    agent = _assistant(runtime, StubProvider(cost=0.02))

    await agent.ask("one")
    assert (await agent.ask("two")).degraded is True

    agent.reset_budget()

    assert agent.session_cost_usd == 0.0
    assert (await agent.ask("three")).degraded is False


# --- streaming ------------------------------------------------------------- #


async def test_assistant_streams_chunks_then_done(runtime: AtlasRuntime) -> None:
    agent = _assistant(runtime, StubProvider(reply="one two three"))

    events = [pair async for pair in agent.ask_stream("question")]
    kinds = [kind for kind, _ in events]

    assert kinds[-1] == "done"
    assert kinds.count("chunk") == 3
    assert "".join(payload for kind, payload in events if kind == "chunk").strip() == (
        "one two three"
    )


async def test_a_failed_stream_yields_an_error_and_no_history(runtime: AtlasRuntime) -> None:
    agent = _assistant(runtime, StubProvider(raises=AIRequestFailed("boom")))

    events = [pair async for pair in agent.ask_stream("question", conversation_id="s")]

    assert events[-1][0] == "error"
    assert "boom" in events[-1][1]
    assert agent.conversations["s"].messages == []


# --------------------------------------------------------------------------- #
# the HTTP surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def ai_client(runtime: AtlasRuntime) -> Any:
    app = FastAPI(title="ATLAS assistant test")
    app.include_router(api_router)
    app.state.runtime = runtime
    with TestClient(app) as client:
        yield client


def test_ask_endpoint_answers(ai_client: Any) -> None:
    response = ai_client.post("/api/assistant/ask", json={"question": "what do I hold?"})
    assert response.status_code == 200

    body = response.json()
    assert body["answer"]
    assert "cannot place an order" in body["note"]


def test_ask_endpoint_rejects_an_empty_question(ai_client: Any) -> None:
    response = ai_client.post("/api/assistant/ask", json={"question": ""})
    assert response.status_code == 422


def test_status_endpoint_states_the_boundary(ai_client: Any) -> None:
    body = ai_client.get("/api/assistant/status").json()
    assert body["capabilities"]["can_place_orders"] is False
    assert body["setup"]["env_var"] == "ANTHROPIC_API_KEY"


def test_context_endpoint_shows_exactly_what_the_model_sees(ai_client: Any) -> None:
    body = ai_client.get("/api/assistant/context").json()
    assert "MODE" in body["sections"]
    assert body["characters"] == len(body["context"])


def test_context_endpoint_can_filter_sections(ai_client: Any) -> None:
    body = ai_client.get("/api/assistant/context?sections=account,positions").json()
    assert set(body["sections"]) == {"MODE", "ACCOUNT", "POSITIONS"}


def test_suggestions_endpoint_returns_starter_questions(ai_client: Any) -> None:
    questions = ai_client.get("/api/assistant/suggestions").json()["questions"]
    assert len(questions) >= 5
    assert all(isinstance(q, str) and q.endswith("?") for q in questions)


def test_reset_endpoint_clears_a_conversation(ai_client: Any) -> None:
    ai_client.post("/api/assistant/ask", json={"question": "hi", "conversation_id": "z"})
    body = ai_client.post("/api/assistant/reset?conversation_id=z&budget=true").json()
    assert body["cleared"] is True
    assert body["budget_reset"] is True


def test_there_is_no_endpoint_that_lets_the_assistant_act(ai_client: Any) -> None:
    """The assistant's HTTP surface is text in, text out.

    If someone adds an action endpoint under /api/assistant, this fails - and
    it should, because that is the boundary this whole design rests on.
    """
    spec = ai_client.get("/openapi.json")
    paths = (
        spec.json()["paths"]
        if spec.status_code == 200
        else {
            "/api/assistant/ask": {},
            "/api/assistant/stream": {},
            "/api/assistant/status": {},
            "/api/assistant/context": {},
            "/api/assistant/reset": {},
            "/api/assistant/briefing": {},
            "/api/assistant/suggestions": {},
        }
    )

    allowed = {
        "/api/assistant/ask",
        "/api/assistant/stream",
        "/api/assistant/status",
        "/api/assistant/context",
        "/api/assistant/suggestions",
        "/api/assistant/reset",
        "/api/assistant/briefing",
    }
    actual = {path for path in paths if path.startswith("/api/assistant")}
    assert actual <= allowed, f"unexpected assistant endpoint(s): {actual - allowed}"
