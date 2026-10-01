"""The chat boundary.

complete() and stream() are the only things the rest of the app calls.
complete() returns a plain ChatResult; stream() yields the answer's text as
it is written and then the same ChatResult. Both raise ChatError (give up)
or TransientChatError (retry) -- no vendor type or HTTP detail ever crosses
this line. Mirrors the reference's expenses/extraction/.
"""

from collections.abc import Iterator

from django.conf import settings

from .errors import ChatError, TransientChatError
from .registry import get_provider
from .types import ChatResult

__all__ = ["ChatError", "ChatResult", "TransientChatError", "complete", "stream"]


def _configured(max_output_tokens: int | None):
    """The configured provider, its model and the output ceiling for one call."""
    provider_name = settings.CHAT_PROVIDER
    provider = get_provider(provider_name)
    # .get(), not [] -- "fake" has no entry in CHAT_MODELS (it has no real
    # model to name), and must not KeyError on the everyday default.
    model = settings.CHAT_MODELS.get(provider_name, "")
    ceiling = settings.CHAT_MAX_OUTPUT_TOKENS if max_output_tokens is None else max_output_tokens
    return provider, model, ceiling


def complete(system: str, user: str, max_output_tokens: int | None = None) -> ChatResult:
    """Send one system prompt and one user message to the configured provider.

    ``max_output_tokens`` overrides CHAT_MAX_OUTPUT_TOKENS for a caller whose answer
    is long by nature (a whole formatted note), or short (the condenser: a
    question, not an answer).
    """
    provider, model, ceiling = _configured(max_output_tokens)
    return provider.complete(system, user, model, ceiling)


def stream(
    system: str, user: str, max_output_tokens: int | None = None
) -> Iterator[str | ChatResult]:
    """As complete(), but yield the answer as it is written (DECISIONS D360).

    Yields non-empty text deltas, then exactly one ChatResult, last; its text
    is the deltas joined and stripped, as complete() would have returned it.
    A provider without ``stream`` is called through ``complete`` and yields
    its whole answer as one delta. The errors are complete()'s, raised before
    the first delta or after some: a caller that shows deltas must be ready
    to take them back.

    A generator: nothing is sent until the first ``next()``, and closing it
    early closes the provider's connection.
    """
    provider, model, ceiling = _configured(max_output_tokens)
    native = getattr(provider, "stream", None)
    if native is None:
        result = provider.complete(system, user, model, ceiling)
        if result.text:
            yield result.text
        yield result
        return

    result = None
    items = iter(native(system, user, model, ceiling))
    try:
        for item in items:
            if isinstance(item, ChatResult):
                result = item
            elif item:
                yield item
    finally:
        close = getattr(items, "close", None)
        if close is not None:
            close()
    if result is None:
        raise ChatError(f"Chat provider {provider.name!r} ended its stream without a result")
    yield result
