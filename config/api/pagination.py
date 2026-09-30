"""Cursor pagination, and why it dictates the ordering.

A cursor encodes "where the last page stopped" as a value of the ordering
field. That only works if the field is **unique and does not change**: not
unique, and rows sharing a value straddle the boundary, so one is returned
twice or skipped; editable, and a row that moves drops a client into the
wrong place. ``updated_at`` fails the second test on every autosave. The
primary key fails neither, which is why it is the ordering here.
"""

from rest_framework.pagination import CursorPagination


class IdCursorPagination(CursorPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "page_size"
    ordering = "-id"
