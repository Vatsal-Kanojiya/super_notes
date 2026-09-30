"""Request parsers.

DRF's JSONParser catches ``ValueError`` only. ``json.loads`` on a body
nested a few thousand levels deep raises ``RecursionError`` instead, which
is not one, so the client got a 500 (and an error report) for what is plain
bad input. A body that size is well under DATA_UPLOAD_MAX_MEMORY_SIZE, so
the size limit does not stop it.
"""

from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser as DRFJSONParser


class JSONParser(DRFJSONParser):
    """DRF's JSONParser, with too-deep nesting a 400 (``parse_error``)."""

    def parse(self, stream, media_type=None, parser_context=None):
        try:
            return super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError("JSON is nested too deeply.") from None
