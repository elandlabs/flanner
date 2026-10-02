"""The Agent reach page: redacted like the terminal, details only in the window (R3)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flanner import curb_approval, curb_store, curb_window
from flanner.web import app

LOCAL_URL = "http://127.0.0.1:8080"


class Yes:
    name, weak = "test prompt", False

    def available(self):
        return True

    def confirm(self, reason):
        return True


@pytest.fixture
def page(db, tmp_path, monkeypatch):
    claude = tmp_path / "claude-config"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr("flanner.curb_inventory.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_report.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_inventory.run", lambda argv: None)
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])
    aws = Path.home() / ".aws" / "credentials"
    aws.parent.mkdir(parents=True, exist_ok=True)
    aws.write_text("[quokka-prod]\naws_access_key_id = x\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return TestClient(app, base_url=LOCAL_URL), claude


def test_the_page_is_redacted_like_the_terminal(page):
    client, _ = page
    response = client.get("/curb")
    assert response.status_code == 200
    assert "Claude Code" in response.text and "High" in response.text
    assert "quokka" not in response.text.lower()
    assert ".aws" not in response.text


def test_the_page_shows_the_last_sweeps_counts(page):
    client, _ = page
    curb_store.save_report(
        "sweep",
        {
            "by_class": {"A": 2, "B": 1, "C": 0},
            "classes": {
                "A": "sent to a model provider",
                "B": "readable by an agent",
                "C": "on disk but blocked",
            },
        },
    )
    assert "sent to a model provider" in client.get("/curb").text


def test_details_open_in_the_window_never_in_the_browser(page, monkeypatch):
    client, _ = page
    started = []
    monkeypatch.setattr(curb_window, "unavailable", lambda: None)
    monkeypatch.setattr(curb_window, "launch", lambda args: started.append(list(args)))
    response = client.post("/curb/show", follow_redirects=False)
    assert response.status_code == 303 and started[0][0] == "--dir"
    assert "quokka" not in client.get(response.headers["location"]).text.lower()


def test_a_fix_from_the_page_needs_the_operating_systems_yes(page, monkeypatch):
    client, claude = page
    monkeypatch.setattr(curb_approval, "method", lambda: None)
    refused = client.post("/curb/fix", follow_redirects=False)
    assert "No+approval+method" in refused.headers["location"].replace("%20", "+")
    assert not (claude / "settings.json").exists()
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    done = client.post("/curb/fix", follow_redirects=False)
    assert "Changed" in done.headers["location"]
    deny = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["permissions"][
        "deny"
    ]
    assert any(".aws" in rule for rule in deny)


def test_another_site_cannot_press_the_fix_button(page, monkeypatch):
    client, claude = page
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    response = client.post("/curb/fix", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert not (claude / "settings.json").exists()
