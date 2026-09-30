"""Every API error as ``{"detail", "code"}``.

``detail`` is for people and may be reworded; ``code`` is for programs and
will not change. DRF's own errors already carry ``detail`` and know their
code (``not_authenticated``, ``throttled``, ``not_found``...) but do not
put it in the body, so a client would have to branch on English. This adds
it. A 400 from a serializer keeps DRF's field-to-messages shape -- a form
needs to know which field -- plus ``code: "invalid"``.
"""

from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    data = response.data
    if isinstance(data, dict) and "code" not in data:
        if "detail" in data:
            codes = exc.get_codes() if hasattr(exc, "get_codes") else None
            data["code"] = codes if isinstance(codes, str) else "error"
        else:
            data["code"] = "invalid"
    elif isinstance(data, list):
        # A serializer-level ValidationError raised with a bare list.
        response.data = {"detail": data, "code": "invalid"}

    return response
