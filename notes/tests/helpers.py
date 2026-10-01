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


def make_pdf(*pages, password=None, compress=False):
    """A real PDF with one page per string, its lines as Helvetica text.

    ``password`` encrypts it (``""``: opens without one, permissions only);
    ``compress`` Flate-compresses the page content, as most real PDFs are.
    """
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(612, 792)
        operators = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for line in text.split("\n"):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            operators.append(f"({escaped}) Tj T*")
        operators.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(operators).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        if compress:
            page.compress_content_streams()
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
