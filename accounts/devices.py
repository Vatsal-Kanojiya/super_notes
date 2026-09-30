"""Signed-in devices: at most ``MAX_SIGNED_IN_DEVICES`` per account (D6).

Carried over from the reference (expense_management/accounts/devices.py),
API half only: here a "device" is always one refresh-token chain, with one
``SignedInDevice`` row. A sign-in registers its row and, if the account is
then over the limit, ends the oldest devices (by ``last_seen_at``) until it
is not.

Ending a device means ending the real thing the row stands for --
blacklisting its ``OutstandingToken``, so its next refresh is a 401 -- and
recording a ``device_signed_out`` event. Its current access token keeps
working until it expires (``JWT_ACCESS_MINUTES``, default 30): what ends at
once is the ability to refresh (D19).

Every function here is safe for an account whose rows are stale: ``prune``
runs before every count, so a device that simply went away (its token
expired, was blacklisted by a logout, or was flushed) never pushes out a
live one.
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from . import audit
from .models import SignedInDevice, User

LABEL_LENGTH = SignedInDevice._meta.get_field("label").max_length


def limit():
    """How many devices an account may be signed in on. Never below one."""
    return max(1, int(settings.MAX_SIGNED_IN_DEVICES))


def label_for(request):
    """The User-Agent, truncated -- what the devices list calls a device."""
    if request is None:
        return ""
    return request.META.get("HTTP_USER_AGENT", "")[:LABEL_LENGTH]


def register(user, jti, request=None):
    """Record a new refresh-token chain, then enforce the limit. Returns the row."""
    device = SignedInDevice.objects.create(user=user, refresh_jti=jti, label=label_for(request))
    enforce(user, keep=device, request=request)
    return device


def rotate(old_jti, new_jti, user, request=None):
    """A refresh replaced ``old_jti`` with ``new_jti``: one device, one row.

    A token with no row is one issued outside ``issue_tokens`` (nothing else
    that can still refresh lacks one: ending a device blacklists its token).
    It is registered now, so no chain can go on outside the limit for ever.
    """
    moved = SignedInDevice.objects.filter(user=user, refresh_jti=old_jti).update(
        refresh_jti=new_jti, last_seen_at=timezone.now()
    )
    if not moved:
        register(user, new_jti, request)


def forget(jti):
    """A normal sign-out: the token is already blacklisted; drop its row."""
    SignedInDevice.objects.filter(refresh_jti=jti).delete()


def prune(user, keep=None):
    """Drop ``user``'s rows whose refresh token is already dead.

    Dead means expired, blacklisted, or gone from ``OutstandingToken``
    altogether (``flushexpiredtokens``). ``keep`` -- a device being
    registered right now -- is never judged.
    """
    devices = SignedInDevice.objects.filter(user=user)
    if keep is not None:
        devices = devices.exclude(pk=keep.pk)
    devices = list(devices)

    live = set(
        OutstandingToken.objects.filter(
            jti__in=[d.refresh_jti for d in devices],
            expires_at__gt=timezone.now(),
            blacklistedtoken__isnull=True,
        ).values_list("jti", flat=True)
    )
    dead = [d.pk for d in devices if d.refresh_jti not in live]
    if dead:
        SignedInDevice.objects.filter(pk__in=dead).delete()


def end(device, request=None, reason="limit"):
    """Sign ``device`` out for real, drop its row, and record the event.

    ``reason`` is ``limit`` (a newer sign-in pushed it out) or ``user``
    (its owner signed it out from the devices list).
    """
    for token in OutstandingToken.objects.filter(jti=device.refresh_jti):
        BlacklistedToken.objects.get_or_create(token=token)

    audit.record(
        "device_signed_out",
        request=request,
        user=device.user,
        device=device.pk,
        label=device.label,
        reason=reason,
    )
    device.delete()


def enforce(user, keep, request=None):
    """End the oldest devices until ``user`` is within the limit.

    ``keep`` -- the device just registered -- is never ended, so a sign-in
    always succeeds. The user's row is locked for the duration, so two
    sign-ins racing cannot both count the same devices and both leave the
    account one over.
    """
    with transaction.atomic():
        User.objects.select_for_update().filter(pk=user.pk).first()
        prune(user, keep=keep)
        # Oldest first, explicitly: the order decides which devices are ended.
        devices = list(SignedInDevice.objects.filter(user=user).order_by("last_seen_at", "id"))
        excess = len(devices) - limit()
        for device in [d for d in devices if d.pk != keep.pk][: max(excess, 0)]:
            end(device, request=request)


def live_devices(user):
    """``user``'s devices after dropping the dead ones, most recently seen first."""
    prune(user)
    return list(SignedInDevice.objects.filter(user=user).order_by("-last_seen_at", "-id"))
