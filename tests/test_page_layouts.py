"""Pages of the same shape are laid out the same way.

Two shapes, two layouts. A page of sections a person picks between uses the
tab strip Skills already uses. A report or a task read top to bottom is one
column of cards in the middle of the page. Settings used to be six cards in
a column pinned to the left edge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flanner.web import app

WEB = Path(__file__).resolve().parent.parent / "flanner" / "web"
LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return TestClient(app, base_url=LOCAL_URL)


def test_settings_is_tabbed_with_every_card_in_exactly_one_panel(client):
    page = client.get("/settings").text

    tabs = re.findall(r'<a class="tab" href="#(tab-[a-z]+)" data-tab>', page)
    panels = re.findall(r'<section id="(tab-[a-z]+)" class="tab-stack" data-tab-panel>', page)

    assert tabs == ["tab-agents", "tab-team", "tab-machine", "tab-appearance"]
    assert panels == tabs
    sections = re.split(r'<section id="tab-', page)[1:]
    titles = [re.findall(r'class="card-title"[^>]*>([^<]+)<', section) for section in sections]
    assert titles == [
        ["Agents"],
        ["Team"],
        ["General", "Storage", "Where your data is"],
        ["Appearance"],
    ]


def test_the_narrow_column_sits_in_the_middle_and_hidden_panels_stay_hidden():
    css = (WEB / "static" / "css" / "shell.css").read_text(encoding="utf-8")

    narrow = re.search(r"\.pane\.narrow \{([^}]*)\}", css)
    assert narrow is not None and "margin-inline: auto" in narrow.group(1)
    assert ".tab-stack[hidden] { display: none; }" in css


@pytest.mark.parametrize("template", ["setup.html", "register.html", "integrations.html"])
def test_report_and_task_pages_use_the_centred_column(template):
    source = (WEB / "templates" / template).read_text(encoding="utf-8")

    assert '<div class="pane narrow">' in source
    assert "data-tabs" not in source


@pytest.mark.parametrize(
    ("template", "titles"),
    [
        ("mesh.html", ["How syncing actually works"]),
        ("review.html", ["Deciding from here", "What the marks mean"]),
    ],
)
def test_explainers_are_collapsed_with_a_one_line_summary(template, titles):
    """Explanations a person reads once start closed and say what is inside."""
    source = (WEB / "templates" / template).read_text(encoding="utf-8")

    found = re.findall(
        r'<details class="card explainer">\s*<summary>\s*<h2 class="card-title">([^<]+)</h2>'
        r'\s*<span class="gist">[^<]+</span>',
        source,
    )
    assert found == titles
    assert "card-prose" not in source
    assert 'class="footnote"' not in source or template == "mesh.html"


def test_review_explainers_render_closed(client):
    page = client.get("/review").text

    assert page.count('<details class="card explainer">') == 2
    assert '<details class="card explainer" open' not in page


def test_explainers_span_the_page_and_cap_only_their_text():
    """As wide as the cards around them, so their edges line up."""
    css = (WEB / "static" / "css" / "shell.css").read_text(encoding="utf-8")

    rule = re.search(r"\.explainer \{([^}]*)\}", css)
    assert rule is not None
    assert "width: 100%" in rule.group(1) and "max-width" not in rule.group(1)
    assert re.search(r"\.explainer-body p \{[^}]*max-width: 66ch", css)


def test_notices_are_an_icon_on_a_tint_with_no_side_bar():
    """The coloured left bar was bent into a curve by the corner radius."""
    css = (WEB / "static" / "css" / "shell.css").read_text(encoding="utf-8")

    base = re.search(r"\.notice \{([^}]*)\}", css)
    assert base is not None
    assert "border-left" not in base.group(1) and "border: 0" in base.group(1)
    for tone in ("good", "warn", "bad"):
        rule = re.search(r"\.notice\." + tone + r" +\{([^}]*)\}", css)
        assert rule is not None, tone
        assert "--notice-icon: url(" in rule.group(1), tone
    assert "border-left-color" not in css.split(".notice {", 1)[1].split("/* ---", 1)[0]


@pytest.mark.parametrize(
    ("template", "marker"),
    [
        ("skills.html", "found. Nothing is broken on its own"),
        ("memory_pending.html", "This may contradict"),
        ("plan_view.html", "This plan is retired."),
    ],
)
def test_advice_uses_the_amber_tone_not_the_failure_tone(template, marker):
    source = (WEB / "templates" / template).read_text(encoding="utf-8")

    opening = source[: source.index(marker)].rsplit("<div", 1)[1]
    assert 'class="notice warn"' in opening


def test_the_memory_page_is_laid_out_like_the_other_detail_pages():
    """Its body was half width, its headings mis-padded, its history had no columns."""
    source = (WEB / "templates" / "memory_detail.html").read_text(encoding="utf-8")
    css = (WEB / "static" / "css" / "shell.css").read_text(encoding="utf-8")

    assert "card-prose" not in source
    assert '<div class="card card-pad">\n    <div class="card-head">' not in source
    head = re.search(r'grid-head cols-events">\s*((?:<span>[^<]*</span>)+)', source)
    assert head is not None and head.group(1).count("<span>") == 3
    rule = re.search(r"\.cols-events \{ grid-template-columns: ([^;]+);", css)
    assert rule is not None and len(rule.group(1).split()) == 3
