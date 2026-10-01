"""App open: what ``POST session/open/`` does, apart from HTTP (D88, D90, D93, D94, D106-D111).

* :func:`build_stamp` / :func:`is_older` -- build ids (``YYYYMMDDHHMM-<shortsha>``)
  compare by their timestamp prefix.
* :func:`open_app` -- count the open (throttled per device), send ``app_opened``,
  and return the notices the client should show.
"""

import re
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from . import signals
from .models import SignedInDevice, User

_BUILD = re.compile(r"^(\d{12})(?:-[0-9A-Za-z]+)?$")


def build_stamp(build):
    """The timestamp prefix of a build id, or ``None`` if it is not one."""
    match = _BUILD.match(build or "")
    return match.group(1) if match else None


def is_older(build, other):
    """Whether ``build`` is strictly older than ``other``.

    False when either is empty or not a build id: an unknown build (a dev
    server, a typo) is never told to update, and an unset limit never
    announces anything.
    """
    a, b = build_stamp(build), build_stamp(other)
    return a is not None and b is not None and a < b


def update_notice(app_version):
    latest, minimum = settings.CLIENT_LATEST_VERSION, settings.CLIENT_MIN_VERSION
    required = is_older(app_version, minimum)
    if required or is_older(app_version, latest):
        return {"kind": "update", "required": required}
    return None


def memory_notice_due(user):
    """D94: the first open, then every ``MEMORY_NOTICE_EVERY_OPENS`` since it was last seen."""
    seen = user.memory_notice_seen_at_open
    if seen is None:
        return user.app_open_count >= 1
    return user.app_open_count - seen >= max(1, settings.MEMORY_NOTICE_EVERY_OPENS)


def memory_notice(user):
    """D88: prominent while on and never chosen by the user; otherwise subtle."""
    prominent = user.memory_enabled and not user.memory_choice_explicit
    return {
        "kind": "memory",
        "style": "prominent" if prominent else "subtle",
        "state": "on" if user.memory_enabled else "off",
    }


def device_for(user, device_id):
    """The user's own device with this id, else ``None``.

    Another account's device id (a forged or stale claim) finds nothing, so
    it can never be touched through this user's request.
    """
    if device_id is None:
        return None
    return SignedInDevice.objects.filter(pk=device_id, user=user).first()


def _claim_open(device, now):
    """Atomically take this device's open slot; False inside the throttle window."""
    cutoff = now - timedelta(seconds=settings.APP_OPEN_MIN_INTERVAL_SECONDS)
    due = SignedInDevice.objects.filter(pk=device.pk).filter(
        Q(last_app_open_at__isnull=True) | Q(last_app_open_at__lte=cutoff)
    )
    return due.update(last_app_open_at=now) == 1


def open_app(user, device, platform, app_version, reason, request=None):
    """Record an app open and return ``(notices, server_time)``.

    ``device`` is the caller's own :class:`SignedInDevice` or ``None``. Its
    ``last_seen_at`` always moves. The open is counted and ``app_opened``
    sent unless this device already opened within
    ``APP_OPEN_MIN_INTERVAL_SECONDS``; notices are returned either way. A
    caller with no device row cannot be throttled, so each call counts.
    """
    now = timezone.now()
    if device is not None:
        SignedInDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
        counted = _claim_open(device, now)
    else:
        counted = True

    if counted:
        User.objects.filter(pk=user.pk).update(app_open_count=F("app_open_count") + 1)
        signals.send(
            signals.app_opened,
            user=user,
            device=device,
            platform=platform,
            app_version=app_version,
            reason=reason,
        )
    user.refresh_from_db(fields=["app_open_count", "memory_notice_seen_at_open"])

    notices = []
    update = update_notice(app_version)
    if update:
        notices.append(update)
    if memory_notice_due(user):
        notices.append(memory_notice(user))
    return notices, now
