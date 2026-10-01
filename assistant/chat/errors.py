class ChatError(Exception):
    """A question could not be answered, and asking again will not help.

    A bad key, an unknown model, a request the vendor rejects, a refusal, a
    cut-off answer. The ask task marks the AskQuery failed on this -- and a
    failed ask does not count against the quota.
    """


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
