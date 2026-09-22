"""Registration flow tests for flanner.claude_integration.

Every test monkeypatches get_claude_config_path to a temp file so the user's
real Claude config is NEVER touched.
"""

import json
import sys

import pytest

import flanner.claude_integration as ci


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "claude" / "claude_desktop_config.json"
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: path)
    return path


def test_read_claude_config_missing_file(config_path):
    assert ci.read_claude_config() == {"mcpServers": {}}


def test_read_claude_config_invalid_json(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not json")
    assert ci.read_claude_config() == {"mcpServers": {}}


def test_read_claude_config_adds_missing_key(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"other": 1}))
    config = ci.read_claude_config()
    assert config["mcpServers"] == {}
    assert config["other"] == 1


def test_read_claude_config_path_none(monkeypatch):
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: None)
    assert ci.read_claude_config() == {"mcpServers": {}}


def test_write_claude_config_roundtrip(config_path):
    assert ci.write_claude_config({"mcpServers": {"x": {"command": "y"}}}) is True
    assert json.loads(config_path.read_text())["mcpServers"]["x"]["command"] == "y"


def test_write_claude_config_path_none(monkeypatch):
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: None)
    assert ci.write_claude_config({"mcpServers": {}}) is False


def test_register_and_unregister_local(config_path):
    assert ci.is_server_registered() is False

    success, message = ci.register_mcp_server()
    assert success is True
    assert "Registered" in message
    assert ci.is_server_registered() is True
    assert ci.get_server_config_from_claude() == ci.get_local_server_config()

    # Already registered without force: no-op success
    success, message = ci.register_mcp_server()
    assert success is True
    assert "already registered" in message

    # Force re-register
    success, message = ci.register_mcp_server(force=True)
    assert success is True
    assert "Updated" in message

    success, message = ci.unregister_mcp_server()
    assert success is True
    assert ci.is_server_registered() is False

    # Unregister when not registered
    success, message = ci.unregister_mcp_server()
    assert success is False


def test_register_cloud(config_path):
    success, _ = ci.register_mcp_server(server_type="cloud", server_url=None)
    assert success is False

    success, _ = ci.register_mcp_server(
        server_type="cloud", server_url="https://mcp.example.com", api_key="k"
    )
    assert success is True
    config = ci.get_server_config_from_claude()
    assert config["type"] == "cloud"
    assert config["url"] == "https://mcp.example.com"
    assert config["apiKey"] == "k"


def test_register_invalid_type(config_path):
    success, message = ci.register_mcp_server(server_type="bogus")
    assert success is False
    assert "Invalid server type" in message


def test_get_cloud_server_config_no_api_key():
    config = ci.get_cloud_server_config("https://mcp.example.com")
    assert "apiKey" not in config


def test_verify_server_config(config_path):
    ok, message, current = ci.verify_server_config()
    assert ok is False
    assert current is None

    ci.register_mcp_server()
    ok, message, current = ci.verify_server_config()
    assert ok is True
    assert current == ci.get_local_server_config()

    # Corrupt the stored config
    config = ci.read_claude_config()
    config["mcpServers"]["flanner"] = {"command": "stale"}
    ci.write_claude_config(config)
    ok, message, current = ci.verify_server_config()
    assert ok is False
    assert current == {"command": "stale"}


def test_auto_register_on_init(config_path):
    # Not registered: registers fresh
    success, message = ci.auto_register_on_init()
    assert success is True

    # Already registered and valid
    success, message = ci.auto_register_on_init()
    assert success is True
    assert "up-to-date" in message

    # Registered but stale: updates
    config = ci.read_claude_config()
    config["mcpServers"]["flanner"] = {"command": "stale"}
    ci.write_claude_config(config)
    success, message = ci.auto_register_on_init()
    assert success is True
    assert "updated" in message


def test_check_server_status(config_path, monkeypatch):
    status = ci.check_server_status()
    assert status["registered"] is False
    assert "not registered" in status["message"]

    ci.register_mcp_server()
    status = ci.check_server_status()
    assert status["registered"] is True
    assert status["config_valid"] is True

    monkeypatch.setattr(ci, "get_claude_config_path", lambda: None)
    status = ci.check_server_status()
    assert status["config_path"] == "Not found"
    assert status["action_needed"]


def test_get_claude_config_info(config_path):
    info = ci.get_claude_config_info()
    assert info["config_exists"] is False
    assert info["server_registered"] is False

    ci.register_mcp_server()
    info = ci.get_claude_config_info()
    assert info["config_exists"] is True
    assert info["server_registered"] is True
    assert info["total_servers"] == 1
    assert info["our_server_config"]["command"] == sys.executable


def test_get_claude_config_info_path_none(monkeypatch):
    monkeypatch.setattr(ci, "get_claude_config_path", lambda: None)
    info = ci.get_claude_config_info()
    assert info["config_path"] == "Not found"
    assert info["config_exists"] is False


def test_registration_instructions_are_returned_not_printed():
    """The library hands text back; the CLI decides how to show it."""
    text = ci.registration_instructions()
    assert "Restart Claude Code" in text
    assert "flanner" in text


def _install_launcher():
    """What the desktop app puts in FLANNER_HOME (a temp dir, per conftest)."""
    import os
    from pathlib import Path

    name = "flanner-mcp.exe" if sys.platform == "win32" else "flanner-mcp"
    launcher = Path(os.environ["FLANNER_HOME"]) / "bin" / name
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("", encoding="utf-8")
    return launcher


def test_without_the_desktop_app_the_interpreter_is_named():
    assert ci.mcp_launcher() is None
    assert ci.get_local_server_config()["command"] == sys.executable


def test_the_desktop_launcher_is_named_so_updates_do_not_break_it():
    launcher = _install_launcher()
    assert ci.get_local_server_config() == {"command": str(launcher), "args": [], "env": {}}


def test_init_moves_an_existing_entry_onto_the_launcher(config_path):
    """A pip flanner registered first; installing the app then re-running init
    must switch Claude Desktop to the launcher rather than keep the old path."""
    ci.register_mcp_server()
    assert ci.get_server_config_from_claude()["command"] == sys.executable

    launcher = _install_launcher()
    ok, message = ci.auto_register_on_init()

    assert ok is True
    assert "updated" in message
    assert ci.get_server_config_from_claude()["command"] == str(launcher)
