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
"""

from urllib.parse import quote

from ..errors import ChatError
from ..types import ChatResult
from ._http import api_key, post_json

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider:
    name = "gemini"

    def complete(self, system: str, user: str, model: str, max_output_tokens: int) -> ChatResult:
        headers = {
            # GEMINI_API_KEY is what Google's own SDK reads first;
            # GOOGLE_API_KEY is its older fallback.
            "x-goog-api-key": api_key("Gemini", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "Content-Type": "application/json",
        }
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens},
        }
        # quote(): the model name comes from settings, but it is still text
        # going into a URL path.
        data = post_json("Gemini", URL.format(model=quote(model, safe="-._")), headers, body)

        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise ChatError(f"Gemini declined to answer this question ({block_reason})")

        candidates = data.get("candidates") or []
        if not candidates:
            raise ChatError("Gemini returned no answer")
        candidate = candidates[0]

        finish_reason = candidate.get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise ChatError("Gemini's answer was cut off before finishing")
        if finish_reason != "STOP":
            # SAFETY, RECITATION, BLOCKLIST, PROHIBITED_CONTENT, SPII, OTHER...
            raise ChatError(f"Gemini declined to answer this question ({finish_reason})")

        text = "".join(
            part.get("text", "")
            for part in (candidate.get("content") or {}).get("parts") or []
            # Thought summaries, if a thinking model was asked for them,
            # are not the answer.
            if not part.get("thought")
        ).strip()
        if not text:
            raise ChatError("Gemini returned no text")

        usage = data.get("usageMetadata") or {}
        return ChatResult(
            text=text,
            provider=self.name,
            model=data.get("modelVersion") or model,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0)
            + int(usage.get("thoughtsTokenCount") or 0),
        )
