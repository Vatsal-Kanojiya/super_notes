"""The chat boundary.

complete(), stream() and extract_image_text() are the only things the rest
of the app calls.
complete() returns a plain ChatResult; stream() yields the answer's text as
it is written and then the same ChatResult. Both raise ChatError (give up)
or TransientChatError (retry) -- no vendor type or HTTP detail ever crosses
this line. Mirrors the reference's expenses/extraction/.
"""

import dataclasses
from collections.abc import Iterator

from django.conf import settings

from .errors import BilledChatError, ChatError, ImageTextNotSupported, TransientChatError
from .registry import get_provider
from .types import ChatResult

__all__ = [
    "BilledChatError",
    "ChatError",
    "ChatResult",
    "ImageTextNotSupported",
    "TransientChatError",
    "complete",
    "extract_image_text",
    "stream",
]

# What prompts/image_text.md tells the model to answer for an image with no
# text: a provider's empty answer is an error, so "nothing" needs a word.
NO_TEXT = "[no text]"
IMAGE_TEXT_INSTRUCTION = "Transcribe the text in this image."


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


def extract_image_text(image_bytes: bytes, mime_type: str) -> ChatResult:
    """The text written in an image, read by the configured provider (DECISIONS D343).

    ``mime_type`` is the type sniffed from the bytes (JPEG, PNG or WebP).
    The system prompt is prompts/image_text.md; the result's text is the
    transcription, stripped, and empty when the model found none. Raises
    ImageTextNotSupported (a ChatError) when the provider cannot read
    images, and otherwise exactly what complete() raises.
    """
    provider, model, ceiling = _configured(settings.ATTACHMENT_IMAGE_TEXT_MAX_OUTPUT_TOKENS)
    read_image = getattr(provider, "read_image", None)
    if read_image is None:
        raise ImageTextNotSupported(f"Chat provider {provider.name!r} cannot read images")
    # Imported here: the prompt loader is the assistant's, and this package
    # must stay importable on its own.
    from ..prompt import load_prompt

    _, system = load_prompt("image_text")
    result = read_image(system, IMAGE_TEXT_INSTRUCTION, image_bytes, mime_type, model, ceiling)
    text = result.text.strip()
    if text == NO_TEXT:
        text = ""
    return dataclasses.replace(result, text=text)
