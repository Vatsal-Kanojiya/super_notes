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
