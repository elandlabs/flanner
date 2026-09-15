"""A browser that asks for something that is not there gets a page that helps.

A mistyped address used to answer with raw JSON, because the error handler
was registered for FastAPI's exception and the router raises Starlette's.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


def test_an_unknown_address_gets_a_page_not_json(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    client = TestClient(app, base_url=LOCAL_URL)

    page = client.get("/no-such-page")

    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "Nothing lives at this address" in page.text
    assert "/no-such-page" in page.text
    for place in ('href="/projects"', 'href="/plans"', 'href="/memory"'):
        assert place in page.text


def test_a_missing_plan_says_what_was_missing(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    client = TestClient(app, base_url=LOCAL_URL)

    page = client.get(f"/plans/{uuid.uuid4()}")

    assert page.status_code == 404
    assert "Nothing lives at this address" in page.text
    assert "not found" in page.text.lower()


def test_an_unknown_api_address_still_answers_in_json(db, tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    client = TestClient(app, base_url=LOCAL_URL)

    answer = client.get("/api/no-such-thing")

    assert answer.status_code == 404
    assert answer.json() == {"detail": "Not Found"}
