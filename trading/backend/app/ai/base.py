"""The AI provider interface, and the boundary the AI is not allowed to cross.

ATLAS treats a language model as a *narrator and tutor*, never as a decision
maker. That is not a stylistic preference; it is the architecture:

*   Risk rules are deterministic code in `app.risk.rules`. An LLM cannot
    change them, relax them, or be consulted about them.
*   Only the Execution Agent may send an order, and it only acts on a
    `RiskDecision` published by the Risk Agent. There is no code path from
    this module to a broker call.
*   Nothing here receives brokerage credentials. The provider gets a block of
    plain text assembled by `app.ai.context`, which is built from facts ATLAS
    already computed.

So the worst a compromised, hallucinating or prompt-injected model can do is
say something wrong on the Assistant page. It cannot trade, cannot size a
position, and cannot turn a strategy on.

The interface is deliberately small - `complete()` and `stream()` over a list
of messages - so a provider can be a hosted API, a local model, or the
deterministic `NullProvider` that keeps ATLAS fully usable with no API key at
all.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

#: The system prompt every ATLAS provider is given. It states the facts the
#: model must not invent and the role it must not take.
#:
#: Kept here rather than in the agent so that *every* caller of *every*
#: provider inherits it, including future ones.
ATLAS_SYSTEM_PROMPT = """\
You are the ATLAS assistant, part of a personal trading and investment
operating system that its owner runs on their own machine. They live in
Germany and trade a US brokerage (Alpaca) paper account. They are relearning
markets deliberately and want to understand the reasoning, not just the
conclusion.

WHAT YOU ARE
    A teacher and an analyst over data ATLAS has already computed. You explain
    what the system did and why, what the numbers mean, and what a beginner
    should look at next.

WHAT YOU ARE NOT
    You are not the risk manager and not the execution system. ATLAS's risk
    rules are deterministic code with hard-coded ceilings; your opinion has no
    bearing on them. You cannot place, modify or cancel an order, enable a
    strategy, or change a limit. If asked to, say plainly that this is by
    design and point at the dashboard control that does it.

HARD RULES
    1.  Never invent a number, a price, a position, a news story or a source.
        Every figure you state must appear in the ATLAS CONTEXT below. If the
        context does not contain what you were asked about, say so.
    2.  Never give personalised financial advice or a recommendation to buy or
        sell a specific instrument. You may explain what a setup is, what the
        risk looks like, and what would invalidate it. The decision is the
        operator's.
    3.  Never suggest a way around a risk limit, the kill switch, or the
        paper-trading gate. If a trade was blocked, explain the limit and what
        it protects against.
    4.  Non-public information is off limits. Legal, public signals only.
    5.  Be concrete and short. Prefer the actual number from the context over
        a general principle. Say "I don't know" rather than guessing.
    6.  Mention tax only as a plain fact where it is relevant: in Germany,
        realised gains are taxed at roughly 26.4% including Soli, so frequent
        round trips cost money a held position does not.

TONE
    Direct, calm, no hype, no emoji. Write for someone intelligent who is new
    to this. Short paragraphs. No disclaimers beyond what the rules above
    require - the dashboard already carries them.
"""


class AIError(RuntimeError):
    """Base class for provider failures."""


class AIUnavailable(AIError):
    """The provider cannot serve this request at all.

    Raised for a missing API key, a missing SDK, or an exhausted quota -
    anything where retrying the same call immediately is pointless. Callers
    treat this as "fall back to the deterministic path", not as a crash.
    """


class AIRequestFailed(AIError):
    """The provider was reachable but this particular request failed."""


Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class AIMessage:
    """One turn of conversation.

    Only `user` and `assistant` are modelled. System instructions are not
    messages here: they are passed separately so that no caller can smuggle a
    second, competing system prompt into the history.
    """

    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {self.role!r}")


@dataclass(frozen=True, slots=True)
class AIUsage:
    """Token counts and the cost they imply, for the System page.

    Shown to the operator because an assistant that quietly costs money is a
    bad surprise. `cost_usd` is computed by the provider from its own price
    table; a provider that does not know its prices reports None.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class AIResponse:
    """What a provider returns.

    `grounded` is the honest flag: True only when the answer was produced from
    the ATLAS context by a real model. The `NullProvider`'s deterministic
    summaries set it True as well, because they are generated directly from
    the same facts. A degraded "I could not reach the model" answer sets it
    False, and the UI labels it.
    """

    text: str
    model: str
    provider: str
    usage: AIUsage = field(default_factory=AIUsage)
    grounded: bool = True
    truncated: bool = False
    #: Populated when the model's thinking was requested and returned.
    thinking: str | None = None


class AIProvider(abc.ABC):
    """A text-in, text-out language model, with no tools and no side effects.

    Implementations must not perform any action other than producing text.
    No tool use, no file access, no network calls beyond their own inference
    endpoint. This is what makes the security argument in the module docstring
    hold: there is nothing for a prompt injection to reach.
    """

    #: Short stable identifier, shown in the UI and the logs.
    name: str = "base"

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """The model identifier this provider will use."""

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """True when a `complete()` call has a realistic chance of working.

        Checked before every call so the UI can say "no API key" instead of
        showing an error after a round trip.
        """

    @abc.abstractmethod
    async def complete(
        self,
        messages: Sequence[AIMessage],
        *,
        system: str = ATLAS_SYSTEM_PROMPT,
        context: str = "",
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> AIResponse:
        """Answer the conversation in one shot.

        `context` is the read-only ATLAS snapshot. It is passed separately from
        `system` so providers can cache the stable instructions and refresh
        only the volatile facts.
        """

    async def stream(
        self,
        messages: Sequence[AIMessage],
        *,
        system: str = ATLAS_SYSTEM_PROMPT,
        context: str = "",
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> AsyncIterator[str]:
        """Yield the answer in chunks.

        The default implementation calls `complete()` and yields once, so a
        provider only overrides this if it genuinely streams.
        """
        response = await self.complete(
            messages,
            system=system,
            context=context,
            max_tokens=max_tokens,
            effort=effort,
        )
        yield response.text

    def status(self) -> dict[str, object]:
        """Provider state for `/api/assistant/status`. No secrets."""
        return {
            "provider": self.name,
            "model": self.model,
            "available": self.available,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} model={self.model} available={self.available}>"
