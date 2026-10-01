"""Web push: whether it is on, and sending one notification (D87, D170).

Push is on only when both VAPID keys are set. ``send_to_user`` is the one
sender; notes/delivery.py calls it for the ``push`` channel. The payload is
built by the caller and must never carry note content.
"""

import base64
import binascii
import json
import logging
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

from .models import PushSubscription

logger = logging.getLogger(__name__)

# The push service answers these when the subscription is gone for good.
GONE_STATUSES = (404, 410)
TTL_SECONDS = 12 * 60 * 60


ENDPOINT_MAX_CHARS = 1000
_B64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
# What a browser sends: a P-256 point (65 bytes) and a 16-byte secret.
P256DH_BYTES = 65
AUTH_BYTES = (16, 32)


def endpoint_allowed(endpoint: str) -> bool:
    """Is this URL one we may POST to? The server calls it, so it must be a push service.

    https only, no userinfo, no port but 443, and the host on
    ``PUSH_ENDPOINT_HOSTS`` (exact, or ``*.suffix`` for any subdomain). An IP
    literal never matches. Guards against SSRF (D173).
    """
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > ENDPOINT_MAX_CHARS:
        return False
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    for pattern in settings.PUSH_ENDPOINT_HOSTS:
        pattern = pattern.lower()
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and len(host) > len(pattern) - 1:
                return True
        elif host == pattern:
            return True
    return False


def valid_key(value: str, sizes) -> bool:
    """Base64url (padding optional) that decodes to one of ``sizes`` bytes."""
    if not isinstance(value, str) or not _B64URL.match(value) or len(value) > 200:
        return False
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return False
    return len(raw) in sizes if isinstance(sizes, tuple) else len(raw) == sizes


def push_enabled() -> bool:
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def send_to_user(user, payload: dict) -> str:
    """Send ``payload`` to each of the user's subscriptions; the outcome to record.

    ``unavailable`` (push off), ``no_subscriptions``, ``sent`` (all),
    ``partial`` (some) or ``failed`` (none). A 404/410 deletes the
    subscription and does not count as a failure.
    """
    if not push_enabled():
        return "unavailable"
    subscriptions = list(PushSubscription.objects.filter(user=user))
    if not subscriptions:
        return "no_subscriptions"
    data = json.dumps(payload)
    sent = failed = 0
    for sub in subscriptions:
        if not endpoint_allowed(sub.endpoint):
            # Written before the check existed, or the allowlist shrank.
            logger.warning("Push subscription %s has a disallowed endpoint; deleted", sub.pk)
            sub.delete()
            continue
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=data,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=TTL_SECONDS,
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in GONE_STATUSES:
                sub.delete()
                continue
            failed += 1
            logger.warning("Push to subscription %s failed (status %s)", sub.pk, status)
        except Exception:
            failed += 1
            logger.exception("Push to subscription %s failed", sub.pk)
        else:
            sent += 1
            PushSubscription.objects.filter(pk=sub.pk).update(last_success_at=timezone.now())
    if failed == 0:
        return "sent" if sent else "no_subscriptions"
    return "partial" if sent else "failed"
