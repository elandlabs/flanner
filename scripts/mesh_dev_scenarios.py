"""Run mesh messaging end to end on one machine, from the feature branches.

The mesh messaging plan, section 21. Two people on one machine: two flanner
homes, a local control plane, and each home's `flanner peer serve`. They
talk device to device over iroh, the same path two laptops use. Nothing
touches the hosted control plane or your real `~/.flanner`.

    python scripts/mesh_dev_scenarios.py up        start everything
    python scripts/mesh_dev_scenarios.py send question
    python scripts/mesh_dev_scenarios.py all       every scenario, checked
    python scripts/mesh_dev_scenarios.py down      stop and delete the test homes

Needs the `flanner-cloud` worktree beside this one (or `--cloud`) and a
Python for it with the cloud installed (`--cloud-python`). This script's
own Python must have this branch's `flanner` installed.

Slice 2 runs the scenarios the CLI can check. The agent scenarios arrive
with slice 3.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DEFAULT_CLOUD = HERE.parent / "flanner-cloud-mesh-messaging"
HOMES = {
    "you": Path.home() / ".flanner-mesh-you",
    "teammate": Path.home() / ".flanner-mesh-teammate",
}
STATE = Path.home() / ".flanner-mesh-dev.json"
PORT = 8023
ENDPOINT = f"http://127.0.0.1:{PORT}"


# --- running things ------------------------------------------------------------


def flanner(who: str, *args: str, stdin: str | None = None, check: bool = True) -> str:
    """Run the branch's `flanner` as one of the two people."""
    env = {
        **os.environ,
        "FLANNER_HOME": str(HOMES[who]),
        "FLANNER_DESKTOP_NOTIFICATIONS": "off",
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "flanner", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if check and done.returncode != 0:
        raise SystemExit(f"flanner {' '.join(args)} failed as {who}:\n{done.stdout}{done.stderr}")
    return (done.stdout or "") + (done.stderr or "")


def as_json(who: str, *args: str) -> dict:
    return json.loads(flanner(who, *args, "--json"))


def background(
    who: str,
    args: list[str],
    log: Path,
    *,
    cwd: Path | None = None,
    python: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> int:
    env = {**os.environ, **(env_extra or {})}
    if who in HOMES:
        env["FLANNER_HOME"] = str(HOMES[who])
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603 - our own interpreters
        [python or sys.executable, *args], cwd=cwd, env=env, stdout=handle, stderr=handle
    )
    return process.pid


def stop(pid: int) -> None:
    if os.name == "nt":
        kill = ["taskkill", "/PID", str(pid), "/T", "/F"]
        subprocess.run(kill, capture_output=True, check=False)  # noqa: S603,S607
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def cloud_python(args: argparse.Namespace, *code: str) -> str:
    done = subprocess.run(  # noqa: S603 - the cloud's own interpreter
        [args.cloud_python, "-c", "\n".join(code)],
        cwd=args.cloud,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return done.stdout


# --- up and down ---------------------------------------------------------------


def up(args: argparse.Namespace) -> None:
    state = load_state()
    if state:
        raise SystemExit("Already up. Run `down` first.")
    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    env_file = Path(args.cloud) / ".env.local"
    if not env_file.exists():
        env_file.write_text(
            "FLANNER_WEB_UI=1\nFLANNER_INSECURE_COOKIES=1\nFLANNER_ALLOW_EPHEMERAL_KEY=1\n"
            f"DATABASE_URL=sqlite:///./flanner-mesh-dev.db\nPUBLIC_URL={ENDPOINT}\n"
            f"FLANNER_PORT={PORT}\n",
            encoding="utf-8",
        )
    state["cloud"] = background(
        "cloud",
        ["scripts/serve_local.py"],
        logs / "cloud.log",
        cwd=Path(args.cloud),
        python=args.cloud_python,
        env_extra={"FLANNER_PORT": str(PORT)},
    )
    save_state(state)
    for _ in range(60):
        try:
            import urllib.request

            urllib.request.urlopen(f"{ENDPOINT}/health", timeout=2)  # noqa: S310 - localhost
            break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit(f"The control plane did not start; see {logs / 'cloud.log'}")

    seeded = subprocess.run(  # noqa: S603
        [args.cloud_python, "scripts/seed_mesh_dev_team.py"],
        cwd=args.cloud,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    codes = {
        line.split()[0].lstrip("@"): line.split()[3]
        for line in seeded.splitlines()
        if line.startswith("@")
    }
    for who, home in HOMES.items():
        home.mkdir(parents=True, exist_ok=True)
        flanner(who, "login", codes[who], "--endpoint", ENDPOINT)
        state[f"serve_{who}"] = background(
            who,
            ["-m", "flanner", "peer", "serve"],
            logs / f"serve-{who}.log",
            env_extra={"FLANNER_DESKTOP_NOTIFICATIONS": "off" if who == "teammate" else "on"},
        )
        save_state(state)
    state["workspace"] = workspace_of("you")
    save_state(state)
    print(f"Up. Control plane {ENDPOINT}; homes {HOMES['you']} and {HOMES['teammate']}.")
    print(f"Logs in {logs}. Serving peers take a few seconds to come online.")


def workspace_of(who: str) -> str:
    shown = json.loads(flanner(who, "whoami", "--output", "json"))
    grants = shown.get("grants") or shown.get("workspaces") or []
    for grant in grants:
        if isinstance(grant, dict) and grant.get("workspace_id"):
            return str(grant["workspace_id"])
    raise SystemExit(f"No workspace for {who}: {shown}")


def down(_args: argparse.Namespace) -> None:
    import shutil

    for key, pid in load_state().items():
        if key == "cloud" or key.startswith("serve_"):
            stop(int(pid))
    STATE.unlink(missing_ok=True)
    for home in HOMES.values():
        shutil.rmtree(home, ignore_errors=True)
    print("Down. Test homes deleted.")


# --- scenarios -----------------------------------------------------------------


def check(ok: object, detail: object) -> None:
    """A scenario's expectation. Not `assert`, which `python -O` removes."""
    if not ok:
        raise AssertionError(str(detail))


def unread_from_teammate(expect_text: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inbox = as_json("you", "mesh", "inbox")
        for thread in inbox["threads"]:
            if thread["last"]["preview"].startswith(expect_text[:80]):
                return thread
        time.sleep(2)
    raise AssertionError(f"'{expect_text}' did not arrive within {timeout:.0f}s")


def delivery_to_you(thread_short: str) -> str:
    view = as_json("teammate", "mesh", "read", thread_short)
    return view["thread"]["messages"][-1]["delivery"][0]["state"]


def question(_args: argparse.Namespace) -> str:
    text = "drop the old column now, or next release?"
    flanner("teammate", "mesh", "send", "you", text, "--yes")
    thread = unread_from_teammate(text)
    check(thread["last"]["from"]["handle"] == "teammate", thread)
    return "arrived, from @teammate"


def workspace(_args: argparse.Namespace) -> str:
    text = "heads up, deploying billing in ten minutes"
    flanner(
        "teammate",
        "mesh",
        "broadcast",
        text,
        "--workspace",
        load_state()["workspace"],
        stdin="y\n",
    )
    thread = unread_from_teammate(text)
    check(thread["workspace"], thread)
    return f"arrived as a workspace message ({thread['workspace']})"


def offline(args: argparse.Namespace) -> str:
    state = load_state()
    stop(int(state["serve_you"]))
    time.sleep(2)
    text = "are you there? (sent while you were offline)"
    out = flanner("teammate", "mesh", "send", "you", text, "--yes")
    check("Queued for @you" in out, out)
    state["serve_you"] = background(
        "you", ["-m", "flanner", "peer", "serve"], Path(args.logs) / "serve-you.log"
    )
    save_state(state)
    # The teammate's upkeep retries after 1 minute, then 5.
    unread_from_teammate(text, timeout=150)
    return "queued while offline, delivered after you came back"


def quiet(_args: argparse.Namespace) -> str:
    now = time.localtime()
    start = time.strftime("%H:%M", time.localtime(time.mktime(now) - 600))
    end = time.strftime("%H:%M", time.localtime(time.mktime(now) + 3600))
    flanner("you", "mesh", "quiet-hours", f"{start}-{end}")
    try:
        check(as_json("you", "mesh", "quiet-hours")["active"], "quiet hours are not active")
        text = "a message during quiet hours"
        flanner("teammate", "mesh", "send", "you", text, "--yes")
        unread_from_teammate(text)
    finally:
        flanner("you", "mesh", "quiet-hours", "off")
    return "stored and listed during quiet hours; no interruption"


def muted(_args: argparse.Namespace) -> str:
    flanner("you", "mesh", "mute", "teammate", "--for", "1h")
    try:
        text = "a message while muted"
        flanner("teammate", "mesh", "send", "you", text, "--yes")
        check(unread_from_teammate(text)["muted"], "the message was not marked muted")
    finally:
        flanner("you", "mesh", "mute", "teammate", "--off")
    return "stored and listed as muted"


def switched_off(args: argparse.Namespace) -> str:
    def switch(on: bool) -> None:
        cloud_python(
            args,
            "from sqlalchemy.orm import sessionmaker",
            "from flanner_cloud import directory",
            "from flanner_cloud.models import Organization, create_engine_for",
            "s = sessionmaker(bind=create_engine_for('sqlite:///./flanner-mesh-dev.db', "
            "create_tables=False))()",
            "org = s.query(Organization).filter_by(name='Mesh dev team').one()",
            f"directory.set_messaging(s, organization_id=org.id, enabled={on})",
        )
        for who in HOMES:
            flanner(who, "whoami", "--refresh")

    switch(False)
    try:
        out = flanner("teammate", "mesh", "send", "you", "hello?", "--yes", check=False)
        check("Messaging is off for your organization" in out, out)
    finally:
        switch(True)
    return "refused with messaging_off, and back on afterwards"


def limits(_args: argparse.Namespace) -> str:
    too_big = flanner("teammate", "mesh", "send", "you", "x" * 5000, "--yes", check=False)
    check("up to 4 KB" in too_big, too_big)
    control = flanner("teammate", "mesh", "send", "you", "look \x1b[2J here", "--yes", check=False)
    check("control characters" in control, control)
    return "4 KB and control characters refused before sending"


SCENARIOS = {
    "question": question,
    "workspace": workspace,
    "quiet-hours": quiet,
    "muted": muted,
    "switched-off": switched_off,
    "limits": limits,
    "offline": offline,
}


def send(args: argparse.Namespace) -> None:
    if not load_state():
        raise SystemExit("Not up. Run `up` first.")
    print(f"{args.scenario}: {SCENARIOS[args.scenario](args)}")


def run_all(args: argparse.Namespace) -> None:
    if not load_state():
        raise SystemExit("Not up. Run `up` first.")
    failed = 0
    for name, scenario in SCENARIOS.items():
        try:
            print(f"ok    {name}: {scenario(args)}")
        except (AssertionError, SystemExit, subprocess.SubprocessError) as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    raise SystemExit(1 if failed else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cloud", default=str(DEFAULT_CLOUD))
    parser.add_argument("--cloud-python", default=os.environ.get("FLANNER_CLOUD_PYTHON", "python"))
    parser.add_argument("--logs", default=str(Path.home() / ".flanner-mesh-dev-logs"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up").set_defaults(run=up)
    commands.add_parser("down").set_defaults(run=down)
    commands.add_parser("all").set_defaults(run=run_all)
    one = commands.add_parser("send")
    one.add_argument("scenario", choices=sorted(SCENARIOS))
    one.set_defaults(run=send)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
