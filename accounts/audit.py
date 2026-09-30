"""A structured trail of security-relevant events, for after-the-fact review.

Carried over from the reference (expense_management/accounts/audit.py).
Sign-ins and their failures, sign-ups, sign-outs, devices ended by the
limit and attempts refused by the rate limit land in one table
(``accounts.SecurityEvent``) with who, when, from where and under which
request id -- so "what happened to this account last Tuesday" is a query,
and ``grep <request id>`` finds the matching log lines.

**Never raises.** Recording an event happens on the side of a request that
is trying to do something else -- sign a user in, end a device. A bug here,
or the database being briefly unavailable, must never turn into a 500 for
the thing the user actually asked for, so every failure is caught and
logged instead of propagated.

**Never a secret.** ``detail`` is free-form JSON a caller can enrich an
event with, and it is tempting to reach for "just log the token so we can
see what happened" -- don't. ID tokens, access and refresh tokens, token
ids and Google's claim set never belong in a table admins can browse.
"""

import ipaddress
import logging

from django.db import transaction

from .models import SecurityEvent
from .ratelimit import client_ip

logger = logging.getLogger(__name__)


def record(event, request=None, user=None, email="", **detail):
    """Append one row to the security event trail.

    ``email`` is a snapshot: left out, it is filled from ``user`` when one
    is given. ``**detail`` becomes the event's ``detail`` JSON -- small,
    structured, and never anything secret.
    """
    try:
        if not email and user is not None:
            email = user.get_username()

        ip = None
        request_id = ""
        if request is not None:
            # Imported here: config.middleware needs nothing from accounts,
            # but keeping it lazy lets this module be used from a shell or a
            # Celery task without pulling in the middleware stack.
            from config.middleware import get_request_id

            ip = _valid_ip(client_ip(request))
            request_id = get_request_id()

        # Its own savepoint: on Postgres a failed statement aborts the whole
        # surrounding transaction (the device limit runs inside one), so
        # catching the error is only enough if the failure is rolled back to
        # here first.
        with transaction.atomic():
            SecurityEvent.objects.create(
                event=event,
                user=user,
                email=email[:254],
                ip=ip,
                request_id=request_id[:64],
                detail=detail,
            )
    except Exception:
        logger.warning("Failed to record security event %r", event, exc_info=True)


def _valid_ip(value):
    """``value`` if it is an IP address, else ``None``.

    client_ip() answers "unknown" when a request has no address, and a
    forwarded header can carry anything. The column is a real ``inet`` on
    Postgres, which refuses such a value -- and the whole event with it.
    """
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None
