"""Small builders for TipTap documents and users, so tests read as content."""

from django.contrib.auth import get_user_model


def text(value, marks=None):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def para(*parts):
    return {"type": "paragraph", "content": [text(p) if isinstance(p, str) else p for p in parts]}


def heading(value, level=1):
    return {"type": "heading", "attrs": {"level": level}, "content": [text(value)]}


def item(*blocks):
    return {"type": "listItem", "content": [para(b) if isinstance(b, str) else b for b in blocks]}


def bullets(*items):
    return {"type": "bulletList", "content": [item(i) if isinstance(i, str) else i for i in items]}


def task(value, checked=False):
    return {"type": "taskItem", "attrs": {"checked": checked}, "content": [para(value)]}


def tasks(*items):
    return {"type": "taskList", "content": list(items)}


def doc(*blocks):
    return {"type": "doc", "content": [para(b) if isinstance(b, str) else b for b in blocks]}


def checklist_doc(*pairs):
    """``checklist_doc(("milk", True), ("eggs", False))``"""
    return doc(tasks(*(task(value, checked) for value, checked in pairs)))


def make_user(name="alice"):
    return get_user_model().objects.create_user(email=f"{name}@example.com")


def format_limit(free, premium=None, system=None):
    """LIMIT_DEFAULTS with the ``format`` key set, for override_settings."""
    from django.conf import settings

    return {
        **settings.LIMIT_DEFAULTS,
        "format": {
            **settings.LIMIT_DEFAULTS["format"],
            "user_free": free,
            "user_premium": free if premium is None else premium,
            "system": settings.LIMIT_DEFAULTS["format"]["system"] if system is None else system,
        },
    }


def isolate_attachment_storage(testcase):
    """Point attachment storage at a fresh temp dir for one test.

    The test runner already uses a temp dir, but one shared by every
    parallel worker; a test counting files needs its own.
    """
    import shutil
    import tempfile

    from django.conf import settings
    from django.test import override_settings

    location = tempfile.mkdtemp(prefix="sn-test-attachments-")
    testcase.addCleanup(shutil.rmtree, location, True)
    override = override_settings(
        STORAGES={
            **settings.STORAGES,
            "attachments": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": location},
            },
        }
    )
    override.enable()
    testcase.addCleanup(override.disable)
    return location
