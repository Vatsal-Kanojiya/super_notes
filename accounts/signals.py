"""Lifecycle signals (D90). Other apps listen; accounts sends.

* ``user_signed_in(user, request, device, created)`` -- every successful Google
  sign-in, from ``accounts.api.issue_tokens``. ``created`` is true for a new
  account, so registration needs no separate hook.
* ``app_opened(user, device, platform, app_version, reason)`` -- sent by
  ``POST session/open/`` (a later part of this branch).

Receivers are called with ``send_robust``: one that raises is logged and never
breaks the sign-in or the open that triggered it.
"""

import logging

from django.dispatch import Signal

logger = logging.getLogger(__name__)

user_signed_in = Signal()
app_opened = Signal()


def send(signal, **kwargs):
    """``signal.send_robust``, logging (by exception type only) any receiver that raised."""
    for receiver, result in signal.send_robust(sender=None, **kwargs):
        if isinstance(result, Exception):
            logger.error("Receiver %r failed: %s", receiver, type(result).__name__, exc_info=result)
