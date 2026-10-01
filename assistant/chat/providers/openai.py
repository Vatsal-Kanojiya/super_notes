"""The OpenAI provider: the Responses API over plain HTTP.

The same API the reference's bill scanner uses, so its lessons carry over:
``status`` is "completed", "incomplete" (with ``incomplete_details.reason``:
the token ceiling, or the content filter) or "failed" (with ``error``); a
refusal arrives as a ``refusal`` content block rather than an error; and
``max_output_tokens`` counts the reasoning tokens as well as the answer.
Without the SDK there is no ``output_text`` convenience, so the text is
collected from the ``output_text`` blocks of the ``message`` items.

Streamed (``"stream": true``), every event's data carries its ``type``:
``response.output_text.delta`` holds a piece of the answer (``delta``),
``response.refusal.delta`` a refusal, and the stream ends with one of
``response.completed``, ``response.incomplete`` or ``response.failed``,
each carrying the whole response object, usage included. An ``error``
event can arrive instead, at any point.
"""

from contextlib import closing

from ..errors import ChatError, TransientChatError
from ..types import ChatResult
from ._http import api_key, excerpt, post_json, post_stream

URL = "https://api.openai.com/v1/responses"

# The default gpt-5-mini is a reasoning model. Answering from excerpts is
# reading, not problem solving, so low effort keeps the hidden reasoning --
# which is billed, and eats max_output_tokens -- short. A non-reasoning
# model configured through CHAT_OPENAI_MODEL would reject this field.
REASONING_EFFORT = "low"

# The events that end a streamed response.
TERMINAL = {"response.completed", "response.incomplete", "response.failed"}

# Error codes (or types) in an ``error`` event worth retrying: what a 429
# or a 5xx would have been, had the stream not already started.
TRANSIENT_ERRORS = {
    "rate_limit_exceeded",
    "rate_limit_error",
    "server_error",
    "server_is_overloaded",
    "overloaded",
    "internal_error",
    "service_unavailable",
}


class OpenAIProvider:
    name = "openai"

    def complete(self, system: str, user: str, model: str, max_output_tokens: int) -> ChatResult:
        data = post_json("OpenAI", URL, _headers(), _body(system, user, model, max_output_tokens))

        check_status(data)

        parts = []
        for item in data.get("output") or []:
            if item.get("type") != "message":
                continue  # reasoning items carry no answer text
            for block in item.get("content") or []:
                if block.get("type") == "refusal":
                    raise ChatError("OpenAI declined to answer this question")
                if block.get("type") == "output_text":
                    parts.append(block.get("text", ""))
        text = "".join(parts).strip()
        if not text:
            raise ChatError("OpenAI returned no text")

        return self._result(text, data, model)

    def stream(self, system: str, user: str, model: str, max_output_tokens: int):
        body = {**_body(system, user, model, max_output_tokens), "stream": True}
        parts = []
        final = None

        with closing(post_stream("OpenAI", URL, _headers(), body)) as events:
            for event in events:
                data = event.json("OpenAI")
                kind = data.get("type") or event.event
                if kind == "response.output_text.delta":
                    delta = data.get("delta")
                    if isinstance(delta, str) and delta:
                        parts.append(delta)
                        yield delta
                elif kind == "response.refusal.delta":
                    raise ChatError("OpenAI declined to answer this question")
                elif kind in TERMINAL:
                    final = data.get("response") or {}
                    break
                elif kind == "error":
                    raise stream_error(data)
                # response.created, .in_progress, .output_item.*, reasoning
                # summaries, .output_text.done...: nothing to do.

        if final is None:
            raise TransientChatError("OpenAI's stream ended before the answer was finished")
        check_status(final)
        text = "".join(parts).strip()
        if not text:
            raise ChatError("OpenAI returned no text")
        yield self._result(text, final, model)

    def _result(self, text: str, data: dict, model: str) -> ChatResult:
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            provider=self.name,
            model=data.get("model") or model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {api_key('OpenAI', 'OPENAI_API_KEY')}",
        "Content-Type": "application/json",
    }


def _body(system: str, user: str, model: str, max_output_tokens: int) -> dict:
    return {
        "model": model,
        "instructions": system,
        "input": user,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": REASONING_EFFORT},
        # The prompt is the user's own notes. Nothing needs the response
        # kept on OpenAI's side for later retrieval.
        "store": False,
    }


def check_status(data: dict) -> None:
    """ChatError unless the response's status is a finished answer."""
    status = data.get("status")
    if status == "failed":
        message = (data.get("error") or {}).get("message", "unknown error")
        raise ChatError(f"OpenAI failed to answer: {message}")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        if reason == "content_filter":
            raise ChatError("OpenAI declined to answer this question")
        raise ChatError("OpenAI's answer was cut off before finishing")


def stream_error(data: dict) -> Exception:
    """An ``error`` event: its fields at the top level, or under ``error``."""
    error = data.get("error") if isinstance(data.get("error"), dict) else data
    code = error.get("code") or error.get("type") or "unknown_error"
    message = f"OpenAI stream error ({code}): {excerpt(error.get('message', ''))}"
    if code in TRANSIENT_ERRORS or error.get("type") in TRANSIENT_ERRORS:
        return TransientChatError(message)
    return ChatError(message)
