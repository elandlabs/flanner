"""One page of a longer list.

Every list in the web UI pages the same way: a page number and a page size
from the query string, clamped so a bookmarked ``?page=900`` lands on the
last page rather than an empty one, and a size chosen from a short menu
rather than typed, so nobody asks for a million rows. The arithmetic lives
here so no two pages can disagree about it, and so the command line can
cut a listing by the same rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

#: Rows on a page unless the reader chose otherwise. Fifteen fits a laptop
#: screen with the chrome above and the pager below, without scrolling.
DEFAULT_PER_PAGE = 15

#: The sizes a reader may choose from. A menu rather than a free field: a
#: hundred is the most a page can hold and still be read as a page.
PER_PAGE_CHOICES = (15, 30, 50, 100)

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """The rows on one page, and where that page sits in the whole."""

    items: Sequence[T]
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def first(self) -> int:
        """1-based position of the first row shown; 0 when there is none."""
        return 0 if not self.items else self.offset + 1

    @property
    def last(self) -> int:
        return self.offset + len(self.items)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def per_page_or_default(value: Any) -> int:
    """A page size from the menu, or the default.

    Anything else — text, zero, a thousand — is the default rather than an
    error, because the value arrives from a query string or a cookie and a
    stale bookmark should still render.
    """
    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    return size if size in PER_PAGE_CHOICES else DEFAULT_PER_PAGE


def window(total: int, page: Any, per_page: Any) -> tuple[int, int, int]:
    """``(page, per_page, offset)`` for a query, with both inputs clamped."""
    size = per_page_or_default(per_page)
    try:
        number = int(page)
    except (TypeError, ValueError):
        number = 1
    pages = max(1, -(-total // size))
    number = min(max(1, number), pages)
    return number, size, (number - 1) * size


def paginate(items: Sequence[T], page: Any, per_page: Any) -> Page[T]:
    """A page of a list that is already in memory."""
    number, size, offset = window(len(items), page, per_page)
    return Page(list(items[offset : offset + size]), number, size, len(items))
