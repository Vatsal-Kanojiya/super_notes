"""The Gemini provider: generateContent over plain HTTP.

Gemini's REST shape differs from the other two in ways worth flagging:

* The key goes in an ``x-goog-api-key`` header (kept out of the URL, where
  it would end up in proxy and error logs).
* A blocked *prompt* is not an error status: the response has no
  candidates and a ``promptFeedback.blockReason`` instead.
* A blocked or truncated *answer* is a candidate with a ``finishReason``
  other than STOP. Only STOP is a complete answer.
* Usage is ``usageMetadata.promptTokenCount`` / ``candidatesTokenCount``.
  A thinking model's thoughts are counted separately
  (``thoughtsTokenCount``) and added to the output here, since they are
  billed as output.

Streamed (``:streamGenerateContent?alt=sse``), every event is an unnamed
``data:`` line holding a whole GenerateContentResponse with the *new* text
only; the last one carries the ``finishReason``, and ``usageMetadata`` is
cumulative. An error after the stream has started arrives as an event with
an ``error`` object (``code``, ``status``, ``message``) instead.
"""

import base64
from contextlib import closing
from urllib.parse import quote

from ..errors import ChatError, TransientChatError
from ..types import ChatResult
from ._http import api_key, excerpt, post_json, post_stream, status_error

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
STREAM_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
)

# Google's status names for "try again later", in an error event that has
# no usable numeric code.
TRANSIENT_ERRORS = {"RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"}


class GeminiProvider:
    name = "gemini"

    def complete(self, system: str, user: str, model: str, max_output_tokens: int) -> ChatResult:
        data = post_json(
            "Gemini", _url(URL, model), _headers(), _body(system, user, max_output_tokens)
        )
        return self._answer(data, model)

    def read_image(
        self,
        system: str,
        user: str,
        image: bytes,
        mime_type: str,
        model: str,
        max_output_tokens: int,
    ) -> ChatResult:
        """``complete`` with the image as an ``inline_data`` part before the text (D343)."""
        body = _body(system, user, max_output_tokens)
        inline = {
            "inline_data": {
                "mime_type": mime_type,
                "data": base64.standard_b64encode(image).decode("ascii"),
            }
        }
        body["contents"][0]["parts"].insert(0, inline)
        data = post_json("Gemini", _url(URL, model), _headers(), body)
        return self._answer(data, model)

    def _answer(self, data: dict, model: str) -> ChatResult:
        check_prompt(data)
        candidates = data.get("candidates") or []
        if not candidates:
            raise ChatError("Gemini returned no answer")
        candidate = candidates[0]
        check_finish_reason(candidate.get("finishReason"))

        text = _text(candidate).strip()
        if not text:
            raise ChatError("Gemini returned no text")
        return self._result(text, data, model)

    def stream(self, system: str, user: str, model: str, max_output_tokens: int):
        parts = []
        finish_reason = None
        last = {}

        events = post_stream(
            "Gemini", _url(STREAM_URL, model), _headers(), _body(system, user, max_output_tokens)
        )
        with closing(events):
            for event in events:
                data = event.json("Gemini")
                if isinstance(data.get("error"), dict):
                    raise stream_error(data["error"])
                check_prompt(data)
                for key in ("usageMetadata", "modelVersion"):
                    if key in data:
                        last[key] = data[key]
                candidates = data.get("candidates") or []
                if not candidates:
                    continue  # a usage-only chunk
                candidate = candidates[0]
                text = _text(candidate)
                if text:
                    parts.append(text)
                    yield text
                if candidate.get("finishReason"):
                    finish_reason = candidate["finishReason"]
                    # Usage may follow in a later chunk: read to the end.

        if finish_reason is None:
            raise TransientChatError("Gemini's stream ended before the answer was finished")
        check_finish_reason(finish_reason)
        text = "".join(parts).strip()
        if not text:
            raise ChatError("Gemini returned no text")
        yield self._result(text, last, model)

    def _result(self, text: str, data: dict, model: str) -> ChatResult:
        usage = data.get("usageMetadata") or {}
        return ChatResult(
            text=text,
            provider=self.name,
            model=data.get("modelVersion") or model,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0)
            + int(usage.get("thoughtsTokenCount") or 0),
        )


def _url(template: str, model: str) -> str:
    # quote(): the model name comes from settings, but it is still text
    # going into a URL path.
    return template.format(model=quote(model, safe="-._"))


def _headers() -> dict:
    return {
        # GEMINI_API_KEY is what Google's own SDK reads first;
        # GOOGLE_API_KEY is its older fallback.
        "x-goog-api-key": api_key("Gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "Content-Type": "application/json",
    }


def _body(system: str, user: str, max_output_tokens: int) -> dict:
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_output_tokens},
    }


def _text(candidate: dict) -> str:
    return "".join(
        part.get("text", "")
        for part in (candidate.get("content") or {}).get("parts") or []
        # Thought summaries, if a thinking model was asked for them,
        # are not the answer.
        if not part.get("thought")
    )


def check_prompt(data: dict) -> None:
    block_reason = (data.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise ChatError(f"Gemini declined to answer this question ({block_reason})")


def check_finish_reason(finish_reason) -> None:
    if finish_reason == "MAX_TOKENS":
        raise ChatError("Gemini's answer was cut off before finishing")
    if finish_reason != "STOP":
        # SAFETY, RECITATION, BLOCKLIST, PROHIBITED_CONTENT, SPII, OTHER...
        raise ChatError(f"Gemini declined to answer this question ({finish_reason})")


def stream_error(error: dict) -> Exception:
    """An error event, translated as its HTTP status would have been."""
    detail = excerpt(error.get("message", ""))
    try:
        code = int(error.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    if code >= 400:
        return status_error("Gemini", code, detail)
    if error.get("status") in TRANSIENT_ERRORS:
        return TransientChatError(f"Gemini stream error ({error['status']}): {detail}")
    return ChatError(f"Gemini stream error ({error.get('status') or 'unknown'}): {detail}")
