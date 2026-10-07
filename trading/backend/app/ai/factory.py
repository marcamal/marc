"""Picks the AI provider from settings, and never fails to pick one.

The guarantee this module makes: `build_provider()` always returns a working
`AIProvider`. No API key, no SDK, a typo in the provider name - all of them
land on `NullProvider`, which answers from ATLAS's own figures. The Assistant
page is therefore never broken, only less conversational.
"""

from __future__ import annotations

from app.ai.anthropic_provider import DEFAULT_MODEL, AnthropicProvider
from app.ai.base import AIProvider
from app.ai.null_provider import NullProvider
from app.config.settings import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


def build_provider(settings: Settings) -> AIProvider:
    """The provider ATLAS will use this session.

    `ATLAS_AI_PROVIDER` selects it:

    *   `auto` (default) - Anthropic when a key is present, otherwise the
        deterministic provider.
    *   `anthropic` - force Claude. Still degrades to deterministic if the key
        or the SDK is missing, with a warning, because refusing to start the
        backend over an optional feature would be the wrong trade.
    *   `none` / `off` - the deterministic provider, even with a key present.
        Use this to be certain nothing leaves the machine.
    """
    choice = (settings.ai_provider or "auto").strip().lower()

    if choice in ("none", "off", "null", "deterministic"):
        log.info("AI provider disabled by configuration; using deterministic answers")
        return NullProvider()

    if choice not in ("auto", "anthropic", "claude"):
        log.warning(
            "ATLAS_AI_PROVIDER=%r is not recognised. Known values: auto, anthropic, none. "
            "Falling back to auto.",
            choice,
        )
        choice = "auto"

    provider = AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=settings.ai_model or DEFAULT_MODEL,
    )

    if provider.available:
        log.info("AI assistant enabled: %s via %s", provider.model, provider.name)
        return provider

    if choice in ("anthropic", "claude"):
        log.warning(
            "ATLAS_AI_PROVIDER=anthropic was requested but the provider is not "
            "usable (no ANTHROPIC_API_KEY, or the 'anthropic' package is not "
            "installed). Using deterministic answers instead."
        )
    else:
        log.info(
            "no ANTHROPIC_API_KEY found; the assistant will answer from ATLAS's "
            "own figures. Everything else works as normal."
        )
    return NullProvider()
