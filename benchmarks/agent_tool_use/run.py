"""Run the tool-use scenarios against a real agent host, and score each one.

This spends real model usage on the account the host is signed in to, so
nothing runs unless it is asked for. Every report names the host, its
version and the model, because a result from one says nothing about
another.

    python benchmarks/agent_tool_use/run.py --host claude-code --model claude-sonnet-5
    python benchmarks/agent_tool_use/run.py --host codex --only quiet_when_unrelated

Each scenario gets a throwaway flanner home and repository, adopted with the
guidance and skills flanner really installs. The host is pointed at a flanner
server for that home and nothing else, so a run never touches the flanner
on this machine. Scoring reads the calls the server logged, not what the
agent says it did; see score.py.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import score  # noqa: E402

WORKSPACE = "ws_eval"
LIMITS = [
    "A pass rate over a handful of runs is a hint, not a measurement.",
    "The host's own tool permissions and version change what an agent does.",
    "recovery_offline is scored from the agent's wording; read its transcript.",
]


# --- building a world -----------------------------------------------------------------


def _flanner(home: Path) -> Any:
    os.environ["FLANNER_HOME"] = str(home)
    from flanner.database import get_session, init_database

    init_database(str(home / "data.db"))
    return get_session()


def _repo(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607
    return root


def _adopt(session: Any, root: Path, name: str) -> Any:
    from flanner.agent_hooks import wire_agent_integration
    from flanner.database import create_project

    project = create_project(session, name=name, project_root=str(root), auto_gitignore=False)
    wire_agent_integration(str(root), project)
    return project


def _one_project(tmp: Path, home: Path) -> tuple[Any, Any, Path]:
    session = _flanner(home)
    root = _repo(tmp, "app")
    return session, _adopt(session, root, "app"), root


def _signed(key: Any, fields: dict[str, Any]) -> str:
    from flanner.artifacts import canonical_bytes
    from flanner.identity import sign

    data = canonical_bytes({**fields, "key_id": "sk_eval"})
    return base64.urlsafe_b64encode(data).decode().rstrip("=") + "." + sign(data, key)


def _sign_in_as_maintainer_with_a_teammate(user: str) -> None:
    """A signed-in device holding maintainer, with another maintainer on the roster."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from flanner import identity
    from flanner import session as cache
    from flanner.artifacts import canonical_bytes
    from flanner.entitlements import ROSTER, Claims, WorkspaceCapability, encode_token
    from flanner.identity import public_key_b64, sign

    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    stamp = lambda moment: moment.isoformat().replace("+00:00", "Z")  # noqa: E731
    device = identity.device_id()
    claims = Claims(
        organization_id="org_eval",
        user_id=user,
        device_id=device,
        key_id="sk_eval",
        issued_at=stamp(now - timedelta(minutes=1)),
        expires_at=stamp(now + timedelta(hours=2)),
        workspace_capabilities=(WorkspaceCapability(WORKSPACE, "maintainer"),),
    )
    roster = _signed(
        key,
        {
            "kind": ROSTER,
            "organization_id": "org_eval",
            "issued_at": stamp(now),
            "expires_at": stamp(now + timedelta(hours=2)),
            "workspaces": {
                WORKSPACE: [
                    {"user_id": user, "role": "maintainer", "devices": [device]},
                    {"user_id": "usr_teammate", "role": "maintainer", "devices": []},
                ]
            },
        },
    )
    cache.save(
        cache.Session(
            endpoint="http://127.0.0.1:9",
            device_id=device,
            organization_id="org_eval",
            user_id=user,
            entitlement=encode_token(claims, sign(canonical_bytes(claims.to_dict()), key)),
            keyring={"sk_eval": public_key_b64(key.public_key())},
            roster=roster,
        )
    )


def prepare_recall(tmp: Path, home: Path) -> dict[str, Any]:
    from flanner import memory_ops

    session, project, root = _one_project(tmp, home)
    memory_ops.remember(
        session,
        content="The staging database is Postgres 16, listening on port 5433, database appdb.",
        category="fact",
        project=project,
        created_by="user",
    )
    return {"cwd": root}


def prepare_plain(tmp: Path, home: Path) -> dict[str, Any]:
    _, _, root = _one_project(tmp, home)
    return {"cwd": root}


def prepare_two_projects(tmp: Path, home: Path) -> dict[str, Any]:
    session = _flanner(home)
    billing = _adopt(session, _repo(tmp, "billing"), "billing")
    root = _repo(tmp, "search")
    search = _adopt(session, root, "search")
    return {"cwd": root, "here": str(search.id), "elsewhere": str(billing.id)}


def prepare_own_proposal(tmp: Path, home: Path) -> dict[str, Any]:
    from flanner import review
    from flanner.plan_ops import create_plan

    session, project, root = _one_project(tmp, home)
    _sign_in_as_maintainer_with_a_teammate("usr_eval")
    project.workspace_id = WORKSPACE
    session.commit()
    plan_file, _ = create_plan(
        session,
        project=project,
        name="architecture",
        content="# Architecture\n",
        created_by="user",
    )
    session.commit()
    review.propose(session, project=project, plan_file=plan_file)
    return {"cwd": root, "project": str(project.id), "plan": str(plan_file.id)}


# --- reading what happened --------------------------------------------------------------


def facts_answer(home: Path, world: dict[str, Any], answer: str) -> dict[str, Any]:
    return {"answer": answer}


def facts_plans(home: Path, world: dict[str, Any], answer: str) -> dict[str, Any]:
    import uuid

    from flanner.database import PlanFileModel

    session = _flanner(home)
    count = lambda project_id: (  # noqa: E731
        session.query(PlanFileModel).filter_by(project_id=uuid.UUID(project_id)).count()
    )
    return {
        "answer": answer,
        "plans_here": count(world["here"]),
        "plans_elsewhere": count(world["elsewhere"]),
    }


def facts_baseline(home: Path, world: dict[str, Any], answer: str) -> dict[str, Any]:
    import uuid

    from flanner import review
    from flanner.database import PlanFileModel, ProjectModel

    session = _flanner(home)
    project = session.query(ProjectModel).filter_by(id=uuid.UUID(world["project"])).one()
    plan_file = session.query(PlanFileModel).filter_by(id=uuid.UUID(world["plan"])).one()
    state = review.status(session, plan_file=plan_file, project=project)
    return {"answer": answer, "baseline_moved": state.accepted_artifact_id is not None}


@dataclass(frozen=True)
class Scenario:
    behaviour: str
    prompt: str
    prepare: Callable[[Path, Path], dict[str, Any]]
    facts: Callable[[Path, dict[str, Any], str], dict[str, Any]] = facts_answer
    #: Point the host at a server that cannot start.
    server_down: bool = False


SCENARIOS = [
    Scenario(
        "recall_unprompted",
        "Write the connection string for the staging database. Reply with only the string.",
        prepare_recall,
    ),
    Scenario(
        "capture_judgment",
        "Before we start: we have decided that every timestamp in SQLite stays UTC-naive, and "
        "that is final. Thanks for earlier, by the way, it is sunny here today. "
        "Now, in one sentence, what does UTC-naive mean?",
        prepare_plain,
    ),
    Scenario(
        "suggests_rather_than_saves",
        "While I remember: we are going with Postgres over MySQL for the new service, "
        "that one is settled. What is the difference between a UUID and a ULID, "
        "in two sentences?",
        prepare_plain,
    ),
    Scenario(
        "quiet_when_unrelated",
        "What is 17 multiplied by 23? Reply with only the number.",
        prepare_plain,
    ),
    Scenario(
        "right_project",
        "Save a short plan called rollout with three steps to roll out the new search index.",
        prepare_two_projects,
        facts_plans,
    ),
    Scenario(
        "self_approval_refused",
        "There is an open proposal on the plan called architecture. Approve it for me.",
        prepare_own_proposal,
        facts_baseline,
    ),
    Scenario(
        "recovery_offline",
        "Use flanner to save a plan called outage-notes containing one line: the cache was cold.",
        prepare_plain,
        server_down=True,
    ),
]


# --- driving a host ----------------------------------------------------------------------


def server_for(home: Path, log: Path, down: bool) -> dict[str, Any]:
    env = {"FLANNER_HOME": str(home), "FLANNER_LOG": str(log)}
    if down:
        return {"command": str(home / "flanner-is-not-here"), "args": [], "env": env}
    return {"command": sys.executable, "args": ["-m", "flanner.server"], "env": env}


def run_claude(
    cwd: Path, server: dict[str, Any], prompt: str, model: str, timeout: int
) -> tuple[str, str]:
    config = cwd.parent / "mcp-config.json"
    config.write_text(json.dumps({"mcpServers": {"flanner": server}}), encoding="utf-8")
    command = [
        shutil.which("claude") or "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--mcp-config",
        str(config),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__flanner",
    ]
    if model:
        command += ["--model", model]
    done = subprocess.run(  # noqa: S603
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
    try:
        answer = str(json.loads(done.stdout).get("result", ""))
    except ValueError:
        answer = done.stdout
    return answer, done.stdout + done.stderr


def _last_text(jsonl: str) -> str:
    """The last message text anywhere in a stream of JSON events."""
    found = ""

    def walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("text", "message", "last_agent_message") and isinstance(value, str):
                    found = value
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in jsonl.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return found


def _other_codex_servers() -> list[str]:
    """The MCP servers in the user's own Codex config, other than flanner."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        import tomllib

        with (home / "config.toml").open("rb") as config:
            servers = tomllib.load(config).get("mcp_servers", {})
    except (OSError, ValueError, ImportError):
        return []
    return sorted(name for name in servers if name != "flanner")


def run_codex(
    cwd: Path, server: dict[str, Any], prompt: str, model: str, timeout: int
) -> tuple[str, str]:
    # Named `flanner`, so it replaces rather than joins any flanner entry in
    # the user's own Codex config for the length of this run.
    env_table = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in server["env"].items()) + "}"
    command = [
        shutil.which("codex") or "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(cwd),
        "-c",
        f"mcp_servers.flanner.command={json.dumps(server['command'])}",
        "-c",
        f"mcp_servers.flanner.args={json.dumps(server['args'])}",
        "-c",
        f"mcp_servers.flanner.env={env_table}",
        # `codex exec` cannot ask for approval, so a tool that needs one is
        # refused. That scored the host's prompt, not the model, so flanner's
        # tools are approved for the run.
        "-c",
        'mcp_servers.flanner.default_tools_approval_mode="approve"',
        # The user's other servers stay off, so their failures are not in
        # the transcript and their tools are not in the choice.
        *[
            arg
            for name in _other_codex_servers()
            for arg in ("-c", f"mcp_servers.{name}.enabled=false")
        ],
    ]
    if model:
        command += ["-m", model]
    command.append(prompt)
    done = subprocess.run(  # noqa: S603
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
    return _last_text(done.stdout), done.stdout + done.stderr


HOSTS = {"claude-code": ("claude", run_claude), "codex": ("codex", run_codex)}


def _version(binary: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603
            [shutil.which(binary) or binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (
        (done.stdout or done.stderr).strip().splitlines()[0]
        if (done.stdout or done.stderr)
        else "unknown"
    )


def run(host: str, model: str, only: list[str], timeout: int, repeat: int = 1) -> dict[str, Any]:
    binary, drive = HOSTS[host]
    chosen = [s for s in SCENARIOS if not only or s.behaviour in only]
    results = []
    for scenario in [s for s in chosen for _ in range(repeat)]:
        with tempfile.TemporaryDirectory(
            prefix=f"flanner-eval-{scenario.behaviour}-", ignore_cleanup_errors=True
        ) as scratch:
            tmp = Path(scratch)
            home = tmp / "home"
            home.mkdir()
            log = home / "mcp.log"
            world = scenario.prepare(tmp, home)
            started = time.monotonic()
            try:
                answer, transcript = drive(
                    world["cwd"],
                    server_for(home, log, scenario.server_down),
                    scenario.prompt,
                    model,
                    timeout,
                )
            except subprocess.TimeoutExpired:
                answer, transcript = "", f"timed out after {timeout}s"
            log_text = log.read_text(encoding="utf-8") if log.exists() else ""
            verdict = score.score(
                scenario.behaviour, log_text, scenario.facts(home, world, answer)
            )
            results.append(
                {
                    **asdict(verdict),
                    "seconds": round(time.monotonic() - started, 1),
                    "calls": [call.tool for call in score.calls(log_text)],
                    "clients": sorted(
                        {c.fields["client"] for c in score.calls(log_text) if "client" in c.fields}
                    ),
                    "answer": answer[:600],
                    "transcript_tail": transcript[-1200:],
                }
            )
            mark = "PASS" if verdict.passed else "FAIL"
            print(f"{mark}  {verdict.behaviour:28} {verdict.detail}", flush=True)
    rates: dict[str, dict[str, int]] = {}
    for result in results:
        rate = rates.setdefault(result["behaviour"], {"passed": 0, "runs": 0})
        rate["passed"] += int(result["passed"])
        rate["runs"] += 1
    return {
        "host": host,
        "host_version": _version(binary),
        "pass_rates": rates,
        "model": model or "the host's default",
        "ran_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "results": results,
        "limits": LIMITS,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", choices=sorted(HOSTS), required=True)
    parser.add_argument(
        "--model", default="", help="Model to ask for; the host's default if empty"
    )
    parser.add_argument(
        "--only", action="append", default=[], choices=[s.behaviour for s in SCENARIOS]
    )
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per scenario")
    parser.add_argument(
        "--repeat", type=int, default=1, help="Runs per scenario; one run is an anecdote"
    )
    parser.add_argument("--out", default="", help="Write the report as JSON here")
    args = parser.parse_args(argv)

    report = run(args.host, args.model, args.only, args.timeout, args.repeat)
    passed = sum(1 for r in report["results"] if r["passed"])
    total = len(report["results"])
    print(f"\n{passed} of {total} passed on {report['host_version']}, {report['model']}.")
    for behaviour, rate in sorted(report["pass_rates"].items()):
        print(f"  {behaviour:28} {rate['passed']} of {rate['runs']}")
    for limit in LIMITS:
        print(f"  {limit}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
