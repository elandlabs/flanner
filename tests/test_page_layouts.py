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
