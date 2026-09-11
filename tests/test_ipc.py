"""Tests for daemon discovery, IPC endpoints, and MCP write forwarding."""

import pytest
from fastapi.testclient import TestClient

from flanner import ipc

# The Host header the middleware expects. TestClient defaults to
# "testserver", which flanner refuses on purpose: a Host it does not
# serve is how DNS rebinding reaches a local-only tool.
LOCAL_URL = "http://127.0.0.1:8080"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_daemon_info_roundtrip(home):
    assert ipc.read_daemon_info() is None
    ipc.write_daemon_info(8080, "tok")
    info = ipc.read_daemon_info()
    assert info["port"] == 8080
    assert info["token"] == "tok"
    ipc.clear_daemon_info()
    assert ipc.read_daemon_info() is None


def test_call_daemon_without_daemon_returns_none(home):
    assert ipc.call_daemon("/ipc/call", {}) is None


def test_call_daemon_with_stale_info_returns_none(home):
    # Advertised daemon is not actually listening: fall back, don't raise.
    ipc.write_daemon_info(1, "tok")
    assert ipc.call_daemon("/ipc/call", {}) is None


@pytest.fixture
def web_client(db, monkeypatch):
    from flanner.web import app

    monkeypatch.setenv(ipc.TOKEN_ENV, "secret-token")
    return TestClient(app, base_url=LOCAL_URL, follow_redirects=False)


@pytest.fixture
def project_id(web_client, git_repo):
    response = web_client.post(
        "/projects/new",
        data={"name": "ipcproj", "project_root": str(git_repo), "plan_directory": ".plans"},
    )
    assert response.status_code in (302, 303)
    return response.headers["location"].rstrip("/").split("/")[-1]


def _headers(token="secret-token"):
    return {"X-Flanner-Token": token}


def test_ipc_rejects_bad_token(web_client, project_id):
    response = web_client.post(
        "/ipc/call",
        json={"op": "create_project", "args": {"name": "x"}},
        headers=_headers("wrong"),
    )
    assert response.status_code == 401


def test_ipc_disabled_without_env(web_client, project_id, monkeypatch):
    monkeypatch.delenv(ipc.TOKEN_ENV)
    response = web_client.post(
        "/ipc/call",
        json={"op": "create_project", "args": {"name": "x"}},
        headers=_headers(),
    )
    assert response.status_code == 503


def _call(client, op, **args):
    return client.post("/ipc/call", json={"op": op, "args": args}, headers=_headers())


def test_ipc_create_and_update_plan(web_client, project_id):
    created = _call(
        web_client, "create_plan_file", project_id=project_id, name="specs/api", content="# v1\n"
    )
    assert created.status_code == 200
    body = created.json()["result"]
    assert body["version"] == 1
    plan_id = body["id"]

    updated = _call(web_client, "update_plan_file", plan_file_id=plan_id, content="# v2\n")
    assert updated.json()["result"]["version"] == 2

    unchanged = _call(web_client, "update_plan_file", plan_file_id=plan_id, content="# v2\n")
    assert "No changes" in unchanged.json()["result"]["message"]


def test_ipc_unknown_operation_is_refused(web_client):
    response = web_client.post("/ipc/call", json={"op": "rm_rf", "args": {}}, headers=_headers())
    assert response.status_code == 422


def test_ipc_failing_operation_returns_200_so_caller_does_not_retry(web_client):
    # A refusal (non-200) means "not executed, safe to run locally"; an
    # operation that runs and fails must not be mistaken for a refusal.
    response = _call(web_client, "create_plan_file", project_id="not-a-uuid", name="x", content="")
    assert response.status_code == 200
    assert response.json()["result"]["error"] is True


def test_mcp_write_tools_forward_to_daemon(monkeypatch):
    import flanner.server as server
    from flanner import services

    seen = []

    def fake_call(path, payload):
        seen.append((path, payload["op"]))
        return {"result": {"forwarded": True}}

    monkeypatch.setattr(services.ipc, "call_daemon", fake_call)

    def forwarded(result: dict) -> dict:
        # Recorded here, in the process that was asked, so the reply also
        # names its entry in the action history.
        assert result.pop("action_id")
        return result

    assert forwarded(server.create_plan_file_tool("p", "n", "c")) == {"forwarded": True}
    assert forwarded(server.update_plan_file_tool("p", "c")) == {"forwarded": True}
    assert forwarded(server.create_project_tool("proj")) == {"forwarded": True}
    assert forwarded(server.link_plan_to_jira_tool("p", "ABC-1")) == {"forwarded": True}
    assert seen == [
        ("/ipc/call", "create_plan_file"),
        ("/ipc/call", "update_plan_file"),
        ("/ipc/call", "create_project"),
        ("/ipc/call", "link_plan_to_jira"),
    ]


def test_daemon_refusal_falls_back_to_local_execution(monkeypatch, db, git_repo):
    # An HTTP refusal must look like "no daemon" so the write still happens once.
    from flanner import services

    monkeypatch.setattr(services.ipc, "call_daemon", lambda path, payload: None)
    result = services.dispatch(
        "create_project", {"name": "fallback", "project_root": str(git_repo)}
    )
    assert result["name"] == "fallback"
