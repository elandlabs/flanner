"""The Skills usage table is searched, sorted and paged like every other list.

It listed every used skill in one table with no search, sort or pages. The
page's other list group, the skills table, has no filter of its own, and the
shared script fell back to the first filter on the page, which would have
been the usage table's.
"""

from __future__ import annotations

from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "flanner" / "web"


def usage_card() -> str:
    source = (WEB / "templates" / "skills.html").read_text(encoding="utf-8")
    start = source.index('<section id="tab-usage"')
    return source[start : source.index('<section id="tab-versions"', start)]


def test_the_usage_table_has_search_sort_and_pages():
    card = usage_card()

    assert "data-listgroup" in card
    assert "data-list-filter" in card and "data-list-sort" in card
    assert "data-list-pager" in card and "data-list-empty" in card
    assert 'data-list-item data-name="{{ row.skill }}"' in card
    assert 'data-uses="{{ row.invocations }}"' in card


def test_a_list_only_borrows_controls_that_belong_to_no_other_list():
    script = (WEB / "static" / "js" / "app.js").read_text(encoding="utf-8")

    assert "el.closest('[data-listgroup]')" in script
    assert "|| document.querySelector('[data-list-filter]')" not in script
