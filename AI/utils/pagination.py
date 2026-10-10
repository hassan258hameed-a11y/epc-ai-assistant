"""Pagination and query-string helpers for list pages.

Everything here reads user-controlled query parameters, so none of it may
raise on a bad value: a mistyped bookmark or a hand-edited URL shows the page
with that one value ignored rather than a server error.
"""
from __future__ import annotations

from datetime import date

from django.core.paginator import Paginator

#: The largest integer SQLite (and a Postgres bigint) can store. A primary-key
#: filter with anything larger raises OverflowError inside the database driver.
MAX_DB_INT = 2**63 - 1


def clean_int(raw, *, minimum: int | None = None, maximum: int = MAX_DB_INT) -> int | None:
    """``raw`` as an int, or None when it is not a usable whole number.

    Out-of-range values are *rejected*, not clamped: ``?student=9999…`` names
    nobody, and quietly turning it into the largest id would name somebody.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > 20:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    if minimum is not None and value < minimum:
        return None
    if value > maximum:
        return None
    return value


def clean_id(raw) -> int | None:
    """A positive primary key from a query parameter, or None."""
    return clean_int(raw, minimum=1)


def clean_date(raw) -> date | None:
    """An ISO ``YYYY-MM-DD`` date from a query parameter, or None."""
    text = str(raw).strip() if raw is not None else ""
    if not text or len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def paginate(request, queryset, per_page: int = 25):
    """The page the request asked for, clamped into the range that exists.

    ``?page=abc`` and ``?page=0`` (or anything below one) mean the first page;
    a number past the end means the last. ``Paginator.get_page`` alone sends a
    number below one to the *last* page, which turned a stray ``?page=0`` in a
    bookmark into a jump to the oldest records.
    """
    paginator = Paginator(queryset, per_page)
    raw = request.GET.get("page") or 1
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        number = 1
    if number < 1:
        number = 1
    return paginator.get_page(number)


def querystring(request, **overrides) -> str:
    """Rebuild the querystring with ``overrides`` applied (None removes a key)."""
    params = request.GET.copy()
    for key, value in overrides.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


def page_range(page, window: int = 2) -> list[int | None]:
    """Compact page list: 1 … 4 5 [6] 7 8 … 20 (None marks an ellipsis)."""
    paginator = page.paginator
    current, last = page.number, paginator.num_pages
    if last <= 7:
        return list(range(1, last + 1))

    pages: list[int | None] = [1]
    start = max(2, current - window)
    end = min(last - 1, current + window)
    if start > 2:
        pages.append(None)
    pages.extend(range(start, end + 1))
    if end < last - 1:
        pages.append(None)
    pages.append(last)
    return pages
