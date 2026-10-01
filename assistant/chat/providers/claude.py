"""The Claude provider: Anthropic's Messages API over plain HTTP.

Written against the claude-api skill's raw-HTTP reference (curl/examples.md:
endpoint, the three headers, ``content`` as a list of typed blocks, ``usage``,
``stop_reason``, and the streamed events) and its error table (429, 5xx and
529 "overloaded" are the retryable ones) -- nothing here is guessed.

No ``thinking`` parameter is sent. The default model, Claude Haiku 4.5, runs
without thinking when it is omitted; a newer model configured through
CHAT_CLAUDE_MODEL may think by default, which only adds ``thinking`` blocks
(and, streamed, ``thinking_delta`` events) that the text filters below skip.

Streamed (``"stream": true``), the answer arrives as Server-Sent Events:
``message_start`` (the model, the input tokens), ``content_block_delta``
with a ``text_delta`` per piece of text, ``message_delta`` (the stop reason
and the output tokens so far), ``message_stop``; ``ping`` keeps the
connection alive, and an ``error`` event (``overloaded_error``...) can
arrive at any point, even after a 200.
"""

import base64
from contextlib import closing

from ..errors import BilledChatError, ChatError, TransientChatError
from ..types import ChatResult
from ._http import api_key, excerpt, post_json, post_stream

URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Stop reasons after which the text is a complete answer.
COMPLETE = {"end_turn", "stop_sequence"}

# The error types worth retrying: 429, 500 and 529 as statuses.
TRANSIENT_ERRORS = {"rate_limit_error", "api_error", "overloaded_error"}


class ClaudeProvider:
    name = "claude"

    def complete(self, system: str, user: str, model: str, max_output_tokens: int) -> ChatResult:
        data = post_json("Claude", URL, _headers(), _body(system, user, model, max_output_tokens))
        return self._result(data, model)

    def read_image(
        self,
        system: str,
        user: str,
        image: bytes,
        mime_type: str,
        model: str,
        max_output_tokens: int,
    ) -> ChatResult:
        """``complete`` with the image as a base64 ``image`` block before the text (D343)."""
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": base64.standard_b64encode(image).decode("ascii"),
                },
            },
            {"type": "text", "text": user},
        ]
        data = post_json(
            "Claude", URL, _headers(), _body(system, content, model, max_output_tokens)
        )
        return self._result(data, model)

    def _result(self, data: dict, model: str) -> ChatResult:
        usage = data.get("usage") or {}
        cost = {
            "provider": self.name,
            # The model that answered, as the API reports it; the configured
            # alias if it does not.
            "model": data.get("model") or model,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }
        # Past this point the vendor generated an answer: an unusable one is
        # still billed (DECISIONS D500).
        check_stop_reason(data.get("stop_reason"), **cost)

        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if block.get("type") == "text"
        ).strip()
        if not text:
            raise BilledChatError("Claude returned no text", **cost)
        return ChatResult(text=text, **cost)

    def stream(self, system: str, user: str, model: str, max_output_tokens: int):
        body = {**_body(system, user, model, max_output_tokens), "stream": True}
        answered_by = model
        input_tokens = output_tokens = 0
        stop_reason = None
        parts = []
        finished = False

        with closing(post_stream("Claude", URL, _headers(), body)) as events:
            for event in events:
                data = event.json("Claude")
                kind = data.get("type") or event.event
                if kind == "message_start":
                    message = data.get("message") or {}
                    answered_by = message.get("model") or model
                    usage = message.get("usage") or {}
                    input_tokens = int(usage.get("input_tokens") or 0)
                    output_tokens = int(usage.get("output_tokens") or 0)
                elif kind == "content_block_delta":
                    delta = data.get("delta") or {}
                    # Text only: thinking_delta, signature_delta and
                    # input_json_delta are not the answer.
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        parts.append(delta["text"])
                        yield delta["text"]
                elif kind == "message_delta":
                    stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason
                    usage = data.get("usage") or {}
                    # Cumulative counts: the last one is the total.
                    if usage.get("output_tokens") is not None:
                        output_tokens = int(usage["output_tokens"])
                    if usage.get("input_tokens"):
                        input_tokens = int(usage["input_tokens"])
                elif kind == "message_stop":
                    finished = True
                    break
                elif kind == "error":
                    raise stream_error(data.get("error") or {})
                # ping, content_block_start/stop, and any event type added
                # later: nothing to do.

        if not finished:
            raise TransientChatError("Claude's stream ended before the answer was finished")
        cost = {
            "provider": self.name,
            "model": answered_by,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        check_stop_reason(stop_reason, **cost)
        text = "".join(parts).strip()
        if not text:
            raise BilledChatError("Claude returned no text", **cost)
        yield ChatResult(text=text, **cost)


def _headers() -> dict:
    return {
        "x-api-key": api_key("Claude", "ANTHROPIC_API_KEY"),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }


def _body(system: str, user: str | list, model: str, max_output_tokens: int) -> dict:
    return {
        "model": model,
        "max_tokens": max_output_tokens,
        # Top-level, not a message: the system prompt carries the rules,
        # and the user turn carries only data and the question.
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }


def check_stop_reason(stop_reason, **cost) -> None:
    """BilledChatError unless the answer ended the way a complete answer does.

    ``cost`` (provider, model, tokens) rides on the error: the vendor
    generated this answer and bills for it (DECISIONS D500).
    """
    if stop_reason == "refusal":
        raise BilledChatError("Claude declined to answer this question", **cost)
    if stop_reason == "max_tokens":
        # A cut-off answer may end mid-citation. Failing it (and not
        # counting it against the user's quota) beats storing half a claim.
        raise BilledChatError("Claude's answer was cut off before finishing", **cost)
    if stop_reason not in COMPLETE:
        raise BilledChatError(f"Claude stopped unexpectedly: {stop_reason!r}", **cost)


def stream_error(error: dict) -> Exception:
    """An ``error`` event, translated as its HTTP status would have been."""
    kind = error.get("type") or "unknown_error"
    message = f"Claude stream error ({kind}): {excerpt(error.get('message', ''))}"
    if kind in TRANSIENT_ERRORS:
        return TransientChatError(message)
    return ChatError(message)
