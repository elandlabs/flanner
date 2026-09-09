"""The page arithmetic every list shares."""

from flanner.paging import DEFAULT_PER_PAGE, Page, paginate, per_page_or_default, window


def test_a_page_size_off_the_menu_is_the_default():
    assert per_page_or_default("30") == 30
    assert per_page_or_default(100) == 100
    assert per_page_or_default("1000") == DEFAULT_PER_PAGE
    assert per_page_or_default("0") == DEFAULT_PER_PAGE
    assert per_page_or_default("lots") == DEFAULT_PER_PAGE
    assert per_page_or_default(None) == DEFAULT_PER_PAGE


def test_a_page_past_the_end_lands_on_the_last_page():
    assert window(100, "900", "15") == (7, 15, 90)
    assert window(100, "0", "15") == (1, 15, 0)
    assert window(100, "-3", "15") == (1, 15, 0)
    assert window(100, "two", "15") == (1, 15, 0)


def test_an_empty_list_is_one_empty_page():
    page = paginate([], "5", "50")
    assert (page.page, page.pages, page.first, page.last, page.items) == (1, 1, 0, 0, [])
    assert not page.has_prev and not page.has_next


def test_paginate_slices_and_counts():
    rows = list(range(1, 38))
    page = paginate(rows, "3", "15")
    assert page.items == list(range(31, 38))
    assert (page.first, page.last, page.total, page.pages) == (31, 37, 37, 3)
    assert page.has_prev and not page.has_next

    middle = paginate(rows, "2", "15")
    assert (middle.first, middle.last) == (16, 30)
    assert middle.has_prev and middle.has_next


def test_a_query_page_reports_its_position_from_the_offset():
    # The web routes hand Page the rows a LIMIT/OFFSET query returned.
    page = Page(items=["a", "b"], page=4, per_page=15, total=47)
    assert (page.offset, page.first, page.last, page.pages) == (45, 46, 47, 4)
