"""The Claude provider: Anthropic's Messages API over plain HTTP.

Written against the claude-api skill's raw-HTTP reference (curl/examples.md:
endpoint, the three headers, ``content`` as a list of typed blocks, ``usage``,
``stop_reason``) and its error table (429, 5xx and 529 "overloaded" are the
retryable ones) -- nothing here is guessed.

No ``thinking`` parameter is sent. The default model, Claude Haiku 4.5, runs
without thinking when it is omitted; a newer model configured through
CHAT_CLAUDE_MODEL may think by default, which only adds ``thinking`` blocks
that the text filter below skips.
"""

from ..errors import ChatError
from ..types import ChatResult
from ._http import api_key, post_json

URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Stop reasons after which the text is a complete answer.
COMPLETE = {"end_turn", "stop_sequence"}


class ClaudeProvider:
    name = "claude"

    def complete(self, system: str, user: str, model: str, max_output_tokens: int) -> ChatResult:
        headers = {
            "x-api-key": api_key("Claude", "ANTHROPIC_API_KEY"),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": model,
            "max_tokens": max_output_tokens,
            # Top-level, not a message: the system prompt carries the rules,
            # and the user turn carries only data and the question.
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = post_json("Claude", URL, headers, body)

        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            raise ChatError("Claude declined to answer this question")
        if stop_reason == "max_tokens":
            # A cut-off answer may end mid-citation. Failing it (and not
            # counting it against the quota) beats storing half a claim.
            raise ChatError("Claude's answer was cut off before finishing")
        if stop_reason not in COMPLETE:
            raise ChatError(f"Claude stopped unexpectedly: {stop_reason!r}")

        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if block.get("type") == "text"
        ).strip()
        if not text:
            raise ChatError("Claude returned no text")

        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            provider=self.name,
            # The model that answered, as the API reports it; the configured
            # alias if it does not.
            model=data.get("model") or model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
