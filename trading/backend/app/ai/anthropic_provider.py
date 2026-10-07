"""Claude as the ATLAS assistant's language model.

Design notes that are easy to get wrong:

*   **The SDK is imported lazily.** ATLAS must start, run and pass its tests
    with no `anthropic` package and no API key. A missing dependency becomes
    `available = False` and a clear message on the Assistant page, never an
    ImportError at startup.

*   **The stable instructions and the volatile facts are separate system
    blocks.** Prompt caching is a prefix match, so the long ATLAS system
    prompt is cached and the portfolio snapshot - which changes every call -
    goes after it. Putting them in one string would invalidate the cache on
    every single request.

*   **Thinking is adaptive, never budgeted.** On this model family a
    `budget_tokens` value is rejected with a 400, and `{"type": "disabled"}`
    is rejected too. Depth is controlled through `output_config.effort`
    instead, which this provider always sets explicitly rather than relying on
    the model default.

*   **No tools are ever declared.** Not an omission: the assistant is
    text-in, text-out by design, which is what makes it safe to feed it
    account data. See `app.ai.base`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.ai.base import (
    ATLAS_SYSTEM_PROMPT,
    AIMessage,
    AIProvider,
    AIRequestFailed,
    AIResponse,
    AIUnavailable,
    AIUsage,
)
from app.core.logging import get_logger

log = get_logger(__name__)

#: The default model. Opus 5.5: strongest reasoning, 1M context, and the
#: explanations are what this feature is for.
DEFAULT_MODEL = "claude-opus-5-5"

#: Effort levels the API accepts, cheapest first.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: USD per million tokens, for the cost figure on the System page.
#: Input / output for the models ATLAS is likely to be pointed at. A model that
#: is not in this table simply reports no cost rather than a wrong one.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _estimate_cost(model: str, usage: Any) -> float | None:
    """Convert a usage object into dollars, or None if the price is unknown."""
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        # Try the family prefix: "claude-opus-5-5-20260101" prices like its base.
        for known, known_price in PRICES_PER_MTOK.items():
            if model.startswith(known):
                price = known_price
                break
    if price is None:
        return None

    input_price, output_price = price
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    fresh = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0

    # Cache reads bill at ~0.1x input, cache writes at ~1.25x.
    dollars = (
        fresh * input_price + read * input_price * 0.1 + write * input_price * 1.25
    ) / 1_000_000
    dollars += out * output_price / 1_000_000
    return round(dollars, 6)


def _usage_from(model: str, usage: Any) -> AIUsage:
    if usage is None:
        return AIUsage()
    return AIUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cost_usd=_estimate_cost(model, usage),
    )


class AnthropicProvider(AIProvider):
    """Claude via the official Anthropic SDK."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        timeout_seconds: float = 90.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip() or DEFAULT_MODEL
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client: Any | None = None
        self._sdk_error: str | None = None

        #: Running totals, surfaced on the System page so the operator can see
        #: what the assistant has cost this session.
        self.calls = 0
        self.failures = 0
        self.total_cost_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    # availability
    # ------------------------------------------------------------------ #

    @property
    def model(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return bool(self._api_key) and self._sdk() is not None

    def _sdk(self) -> Any | None:
        """Import `anthropic` on first use, remembering a failure."""
        if self._sdk_error is not None:
            return None
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the install
            self._sdk_error = (
                f"the 'anthropic' package is not installed ({exc}). "
                f"Install it with: pip install anthropic"
            )
            log.info("Anthropic provider disabled: %s", self._sdk_error)
            return None
        return anthropic

    def _ensure_client(self) -> Any:
        """The lazily built async client, or an `AIUnavailable` explaining why not."""
        if not self._api_key:
            raise AIUnavailable(
                "No ANTHROPIC_API_KEY. Add one to trading/.env to enable the AI "
                "assistant. ATLAS works without it - the assistant falls back to "
                "deterministic summaries."
            )
        sdk = self._sdk()
        if sdk is None:
            raise AIUnavailable(self._sdk_error or "the anthropic SDK is unavailable")

        if self._client is None:
            self._client = sdk.AsyncAnthropic(
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        return self._client

    # ------------------------------------------------------------------ #
    # request building
    # ------------------------------------------------------------------ #

    def _system_blocks(self, system: str, context: str) -> list[dict[str, Any]]:
        """Stable instructions first (cached), volatile facts second.

        The order matters: caching keys on the prefix, so anything that
        changes between calls must come after everything that does not.
        """
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if context.strip():
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        "=== ATLAS CONTEXT (live, read-only) ===\n"
                        f"{context}\n"
                        "=== END ATLAS CONTEXT ===\n"
                        "Every figure you state must come from the block above."
                    ),
                }
            )
        return blocks

    def _request_kwargs(
        self,
        messages: Sequence[AIMessage],
        system: str,
        context: str,
        max_tokens: int,
        effort: str,
    ) -> dict[str, Any]:
        if not messages:
            raise AIRequestFailed("no messages to send")
        if messages[0].role != "user":
            raise AIRequestFailed("the first message must be from the user")

        level = effort if effort in EFFORT_LEVELS else "medium"

        return {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": self._system_blocks(system, context),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # Adaptive thinking, with no budget: a budget is rejected on this
            # model family, and the effort knob below is the supported control.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": level},
        }

    # ------------------------------------------------------------------ #
    # calls
    # ------------------------------------------------------------------ #

    async def complete(
        self,
        messages: Sequence[AIMessage],
        *,
        system: str = ATLAS_SYSTEM_PROMPT,
        context: str = "",
        max_tokens: int = 2000,
        effort: str = "medium",
    ) -> AIResponse:
        client = self._ensure_client()
        kwargs = self._request_kwargs(messages, system, context, max_tokens, effort)

        self.calls += 1
        try:
            message = await client.messages.create(**kwargs)
        except Exception as exc:
            self.failures += 1
            raise self._translate(exc) from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        ).strip()
        thinking = "".join(
            getattr(block, "thinking", "") or ""
            for block in message.content
            if getattr(block, "type", "") == "thinking"
        ).strip()

        usage = _usage_from(self._model, getattr(message, "usage", None))
        self._accumulate(usage)

        if not text:
            # A refusal or a response that spent everything on thinking. Say
            # so rather than returning an empty bubble.
            stop = getattr(message, "stop_reason", None)
            raise AIRequestFailed(
                f"the model returned no text (stop_reason={stop}). "
                f"Try a shorter question or a higher max_tokens."
            )

        return AIResponse(
            text=text,
            model=getattr(message, "model", self._model),
            provider=self.name,
            usage=usage,
            grounded=True,
            truncated=getattr(message, "stop_reason", None) == "max_tokens",
            thinking=thinking or None,
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
        """Yield text as it is generated.

        Used by the dashboard's chat so a long explanation appears immediately
        instead of after a silent half minute.
        """
        client = self._ensure_client()
        kwargs = self._request_kwargs(messages, system, context, max_tokens, effort)

        self.calls += 1
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for chunk in stream.text_stream:
                    if chunk:
                        yield chunk
                final = await stream.get_final_message()
        except Exception as exc:
            self.failures += 1
            raise self._translate(exc) from exc

        self._accumulate(_usage_from(self._model, getattr(final, "usage", None)))

    # ------------------------------------------------------------------ #
    # bookkeeping
    # ------------------------------------------------------------------ #

    def _accumulate(self, usage: AIUsage) -> None:
        self.total_input_tokens += usage.input_tokens + usage.cache_read_tokens
        self.total_output_tokens += usage.output_tokens
        if usage.cost_usd:
            self.total_cost_usd = round(self.total_cost_usd + usage.cost_usd, 6)

    def _translate(self, exc: Exception) -> Exception:
        """Turn an SDK exception into one of ours, with an actionable message.

        Matched on class *name* rather than on imported classes, so this works
        across SDK versions and needs no import at module scope.
        """
        kind = type(exc).__name__
        detail = str(exc)
        self.last_error = f"{kind}: {detail}"[:400]

        if kind in ("AuthenticationError", "PermissionDeniedError"):
            return AIUnavailable(
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in "
                "trading/.env - the assistant is disabled until it is valid."
            )
        if kind == "NotFoundError":
            return AIUnavailable(
                f"The model '{self._model}' is not available on this API key. "
                f"Set ATLAS_AI_MODEL in trading/.env to one your account can use."
            )
        if kind == "RateLimitError":
            return AIRequestFailed("Rate limited by Anthropic. Wait a moment and ask again.")
        if kind == "APIConnectionError" or kind == "APITimeoutError":
            return AIRequestFailed(
                "Could not reach the Anthropic API. Check your internet connection."
            )
        if kind == "BadRequestError":
            # Usually a request this code built wrong, so keep the detail.
            return AIRequestFailed(f"Anthropic rejected the request: {detail}")
        log.exception("unexpected Anthropic SDK error")
        return AIRequestFailed(f"{kind}: {detail}")

    def status(self) -> dict[str, object]:
        base = super().status()
        base.update(
            {
                "has_api_key": bool(self._api_key),
                "sdk_installed": self._sdk() is not None,
                "sdk_error": self._sdk_error,
                "calls": self.calls,
                "failures": self.failures,
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "session_cost_usd": round(self.total_cost_usd, 4),
                "last_error": self.last_error,
            }
        )
        return base
