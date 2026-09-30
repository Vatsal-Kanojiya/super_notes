"""The chat boundary.

complete() is the only thing the rest of the app calls. It returns a plain
ChatResult, or raises ChatError (give up) or TransientChatError (retry) --
no vendor type or HTTP detail ever crosses this line. Mirrors the
reference's expenses/extraction/.
"""

from django.conf import settings

from .errors import ChatError, TransientChatError
from .registry import get_provider
from .types import ChatResult

__all__ = ["ChatError", "ChatResult", "TransientChatError", "complete"]


def complete(system: str, user: str) -> ChatResult:
    """Send one system prompt and one user message to the configured provider."""
    provider_name = settings.CHAT_PROVIDER
    provider = get_provider(provider_name)
    # .get(), not [] -- "fake" has no entry in CHAT_MODELS (it has no real
    # model to name), and must not KeyError on the everyday default.
    model = settings.CHAT_MODELS.get(provider_name, "")

    return provider.complete(system, user, model, settings.CHAT_MAX_OUTPUT_TOKENS)
