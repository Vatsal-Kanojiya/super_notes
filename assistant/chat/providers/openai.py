"""The OpenAI provider: the Responses API over plain HTTP.

The same API the reference's bill scanner uses, so its lessons carry over:
``status`` is "completed", "incomplete" (with ``incomplete_details.reason``:
the token ceiling, or the content filter) or "failed" (with ``error``); a
refusal arrives as a ``refusal`` content block rather than an error; and
``max_output_tokens`` counts the reasoning tokens as well as the answer.
Without the SDK there is no ``output_text`` convenience, so the text is
collected from the ``output_text`` blocks of the ``message`` items.
"""

from ..errors import ChatError
from ..types import ChatResult
from ._http import api_key, post_json

URL = "https://api.openai.com/v1/responses"

# The default gpt-5-mini is a reasoning model. Answering from excerpts is
# reading, not problem solving, so low effort keeps the hidden reasoning --
# which is billed, and eats max_output_tokens -- short. A non-reasoning
# model configured through CHAT_OPENAI_MODEL would reject this field.
REASONING_EFFORT = "low"


class OpenAIProvider:
    name = "openai"

    def complete(self, system: str, user: str, model: str, max_output_tokens: int) -> ChatResult:
        headers = {
            "Authorization": f"Bearer {api_key('OpenAI', 'OPENAI_API_KEY')}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_output_tokens,
            "reasoning": {"effort": REASONING_EFFORT},
            # The prompt is the user's own notes. Nothing needs the response
            # kept on OpenAI's side for later retrieval.
            "store": False,
        }
        data = post_json("OpenAI", URL, headers, body)

        status = data.get("status")
        if status == "failed":
            message = (data.get("error") or {}).get("message", "unknown error")
            raise ChatError(f"OpenAI failed to answer: {message}")
        if status == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason == "content_filter":
                raise ChatError("OpenAI declined to answer this question")
            raise ChatError("OpenAI's answer was cut off before finishing")

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

        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            provider=self.name,
            model=data.get("model") or model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
