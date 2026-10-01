class ChatError(Exception):
    """A question could not be answered, and asking again will not help.

    A bad key, an unknown model, a request the vendor rejects, a refusal, a
    cut-off answer. The ask task marks the AskQuery failed on this -- and a
    failed ask does not count against the quota.

    A plain ChatError means the vendor rejected the call before generating
    anything (a bad key, a bad request, a body that could not be read): it
    cost nothing, so a system-only use consumed for it is refunded. An
    answer the vendor did generate and charges for, but that cannot be
    used, is a BilledChatError (DECISIONS D500).
    """


class BilledChatError(ChatError):
    """The vendor generated an answer, and bills for it, but it cannot be used.

    A refusal, a cut-off answer, an answer with no text, a stop the
    provider did not expect. Still a ChatError, so a caller that only knows
    "the call failed for good" handles it as before; a caller that consumed
    a use for the call keeps it and records what the call cost, with the
    token counts carried here (0 when the vendor did not report them).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def cost(self) -> dict:
        """The fields limits.describe_where records on a usage event."""
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class TransientChatError(Exception):
    """The provider could not be reached or was busy; the same call may work later.

    Rate limits (429), the vendor's 5xx and overloaded answers, timeouts and
    dropped connections. The ask task lists this in ``autoretry_for``.

    Deliberately *not* a subclass of ChatError (DECISIONS D54): a task that
    writes ``except ChatError`` to mark the ask failed must not swallow a
    failure that Celery was meant to retry.
    """


class ImageTextNotSupported(ChatError):
    """The configured provider has no way to read an image's text.

    A ChatError, so a caller that only knows "the call failed for good"
    handles it; notes/extraction.py tells it apart to say so to the user.
    """
