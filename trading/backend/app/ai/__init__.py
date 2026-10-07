"""The AI layer: a language model as narrator, never as decision maker.

Read `app.ai.base` before changing anything here. The short version:

    The model receives text. The model returns text. That is all.

No tools, no credentials, no path to an order, no influence on a risk rule.
`app.ai.context` builds the read-only snapshot it is allowed to see;
`app.ai.factory` picks a provider and always succeeds, falling back to
deterministic answers built from ATLAS's own figures when no model is
configured.
"""

from app.ai.base import (
    ATLAS_SYSTEM_PROMPT,
    AIError,
    AIMessage,
    AIProvider,
    AIRequestFailed,
    AIResponse,
    AIUnavailable,
    AIUsage,
)
from app.ai.context import ContextBuilder
from app.ai.factory import build_provider
from app.ai.null_provider import NullProvider

__all__ = [
    "ATLAS_SYSTEM_PROMPT",
    "AIError",
    "AIMessage",
    "AIProvider",
    "AIRequestFailed",
    "AIResponse",
    "AIUnavailable",
    "AIUsage",
    "ContextBuilder",
    "NullProvider",
    "build_provider",
]
