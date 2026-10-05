"""The R2 planted-secret corpus, scored per stratum (Curb PRD §15, §16).

A stratum is agent × OS × artifact type. Each holds 20 planted secrets: 10
on a machine whose agent can read everything, 10 on one whose agent is
denied the whole test folder. The label for each secret is its exposure
class, from the PRD's definitions:

- in a transcript or prompt history: A, whatever the settings;
- elsewhere, readable by the agent: B;
- elsewhere, denied: C. On native Windows Claude Code's sandbox does not
  run (E12), so its shell still reads the file: B. On Python 3.10, which
  has no TOML reader, Curb cannot read Codex's config, and a setting it
  could not read is never a control (§9.4): B.

The gates: recall of at least 95% per stratum (found, in the right class),
false positives at most 5%, and no planted value in any output.

The detector is a stand-in that knows the planted format, so this measures
where the sweep looks and how it classes what it finds. With the `sweep`
extra installed, the same corpus also runs through Kingfisher itself, with
real-format GitHub tokens among text that must not match.
"""

from __future__ import annotations

import json
import re
import secrets
import string
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from flanner import curb_kingfisher, curb_match, curb_settings, curb_sweep
from flanner.curb_context import BASELINE, CLAUDE, CODEX, default
from flanner.curb_kingfisher import Match

OSES = ("darwin", "linux", "win32")
PER_POSTURE = 10
MIN_RECALL, MAX_FALSE = 0.95, 0.05
SHARED = ("shell history", "project .env", "flanner plan", "flanner memory")
TYPES = {
    CLAUDE: (
        "Claude Code transcript",
        "CLAUDE.md",
        "skill",
        "MCP config",
        "Claude Code settings",
        *SHARED,
    ),
    CODEX: (
        "Codex session",
        "Codex prompt history",
        "AGENTS.md",
        "skill",
        "Codex config",
        *SHARED,
    ),
}
SENT_TYPES = {"Claude Code transcript", "Codex session", "Codex prompt history"}
#: Text that looks busy but holds no secret, around every planted one.
NOISE = (
    "commit 3f9a2c1b7d4e5f60718293a4b5c6d7e8f9012345",
    "request id 9b2f6c1e-4a7d-4e2b-9c3f-1a2b3c4d5e6f",
    "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "build 2026.10.02-rc.1 passed in 42.7s",
)


# --- planted values ------------------------------------------------------------------

_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase


def github_token() -> str:
    """A classic GitHub token in its real format: 30 random characters and a CRC32 checksum."""
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(30))
    checksum, digits = zlib.crc32(body.encode()), ""
    while checksum:
        checksum, rest = divmod(checksum, 62)
        digits = _BASE62[rest] + digits
    return f"ghp_{body}{digits.rjust(6, '0')}"


FAKE = re.compile(r"CURBTEST_[A-Z0-9]{12}")


def stand_in(path: Path) -> list[Match]:
    found = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        found += [Match("test.token", "Test token", number, m) for m in FAKE.findall(line)]
    return found


def fake_token() -> str:
    return "CURBTEST_" + "".join(
        secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12)
    )


# --- writing artifacts --------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def _line(value: str, n: int) -> str:
    return f"{NOISE[n % len(NOISE)]}\nexport DEPLOY_TOKEN={value}\n"


def plant(kind: str, agent: str, w: SimpleNamespace, value: str, n: int, platform: str) -> None:
    """Put one secret in one artifact of the given type, varying the file by n."""
    noise = NOISE[n % len(NOISE)]
    if kind == "Claude Code transcript":
        record = {"type": "user", "message": {"content": f"{noise}; use {value} to deploy"}}
        _write(
            w.claude / "projects" / f"repo-{n % 3}" / f"session-{n}.jsonl",
            json.dumps(record) + "\n",
        )
    elif kind == "Codex session":
        record = {"type": "response_item", "payload": {"text": f"{noise} token={value}"}}
        _write(
            w.codex / "sessions" / "2026" / "10" / f"{n:02d}" / f"rollout-{n}.jsonl",
            json.dumps(record) + "\n",
        )
    elif kind == "Codex prompt history":
        _write(w.codex / "history.jsonl", json.dumps({"ts": n, "text": f"{noise} {value}"}) + "\n")
    elif kind == "CLAUDE.md":
        where = [w.claude / "CLAUDE.md", w.project / "CLAUDE.md", w.project / "CLAUDE.local.md"]
        _write(where[n % 3], f"\n- {noise}\n- The staging key is {value}\n")
    elif kind == "AGENTS.md":
        where = [w.codex / "AGENTS.md", w.project / "AGENTS.md"]
        _write(where[n % 2], f"\n- {noise}\n- Use {value} for the registry\n")
    elif kind == "skill":
        root = w.project / (".claude" if agent == CLAUDE else ".agents") / "skills" / f"skill-{n}"
        _write(root / "SKILL.md", f"---\nname: skill-{n}\n---\n{noise}\n")
        _write(root / "scripts" / "run.sh", f"#!/bin/sh\ncurl -H 'Authorization: {value}' x\n")
    elif kind == "MCP config":
        where = w.claude / ".claude.json" if n % 2 else w.project / ".mcp.json"
        servers = json.loads(where.read_text(encoding="utf-8")) if where.exists() else {}
        servers.setdefault("mcpServers", {})[f"server-{n}"] = {
            "command": "npx",
            "env": {"API_TOKEN": value},
        }
        where.write_text(json.dumps(servers), encoding="utf-8")
    elif kind == "Claude Code settings":
        where = [
            w.project / ".claude" / "settings.local.json",
            w.project / ".claude" / "settings.json",
        ]
        path = where[n % 2]
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault("env", {})[f"TOKEN_{n}"] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    elif kind == "Codex config":
        _write(
            w.codex / "config.toml",
            f'\n[mcp_servers.server{n}]\ncommand = "npx"\nenv = {{ API_TOKEN = "{value}" }}\n',
        )
    elif kind == "shell history":
        files = [w.home / ".bash_history", w.home / ".zsh_history"]
        if platform == "win32":
            files.append(
                w.roaming
                / "Microsoft"
                / "Windows"
                / "PowerShell"
                / "PSReadLine"
                / "ConsoleHost_history.txt"
            )
        else:
            files.append(w.home / ".local" / "share" / "fish" / "fish_history")
        _write(files[n % len(files)], _line(value, n))
    elif kind == "project .env":
        names = [".env", ".env.local", f"svc{n}/.env", f"apps/web{n}/.env.production"]
        _write(w.project / names[n % len(names)], f"# {noise}\nAPI_TOKEN_{n}={value}\n")
    elif kind == "flanner plan":
        _write(w.project / ".plans" / f"plan-{n}.md", f"# Plan {n}\n\n{noise}\n\nKey: {value}\n")
    elif kind == "flanner memory":
        folder = w.flanner / "memory" / "personal" if n % 2 else w.project / ".flanner" / "memory"
        _write(folder / f"memory-{n}.md", f"---\ntitle: m{n}\n---\n{noise}\nvalue {value}\n")
    else:  # pragma: no cover
        raise AssertionError(kind)


# --- postures -------------------------------------------------------------------------


def _locked(agent: str, w: SimpleNamespace) -> None:
    """Deny the agent the whole test folder, through every channel it has."""
    if agent == CLAUDE:
        settings = {
            "permissions": {"deny": [f"Read(/{curb_match.posix(w.base)}/**)"]},
            "sandbox": {
                "enabled": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {"denyRead": [str(w.base)]},
            },
        }
        (w.claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    else:
        # A permissions profile alone: Codex documents that it does not
        # combine with sandbox_mode.
        locked = (
            'approval_policy = "never"\ndefault_permissions = "locked"\n'
            '[permissions.locked]\nextends = ":workspace"\n[permissions.locked.filesystem]\n'
            f'{json.dumps(str(w.base))} = "deny"\n'
        )
        config = w.codex / "config.toml"
        rest = config.read_text(encoding="utf-8") if config.exists() else ""
        config.write_text(locked + rest, encoding="utf-8")


def expected(agent: str, kind: str, locked: bool, platform: str) -> str:
    if kind in SENT_TYPES:
        return curb_sweep.SENT
    unread = agent == CODEX and sys.version_info < (3, 11)  # its lock is in config.toml
    if not locked or unread or (agent == CLAUDE and platform == "win32"):
        return curb_sweep.READABLE
    return curb_sweep.BLOCKED


# --- running the strata -----------------------------------------------------------------


@dataclass
class Stratum:
    agent: str
    platform: str
    kind: str
    planted: dict[str, str]  # value -> expected class
    found: dict[str, list[str]]  # value -> classes found
    false: int
    total: int
    views: list[str]


def _box(base: Path) -> SimpleNamespace:
    w = SimpleNamespace(
        base=base,
        home=base / "home",
        claude=base / "claude-config",
        codex=base / "codex-home",
        project=base / "project",
        flanner=base / "flanner-home",
    )
    w.roaming = w.home / "AppData" / "Roaming"
    for folder in (w.home, w.claude, w.codex, w.project, w.flanner):
        folder.mkdir(parents=True)
    return w


def _run(
    agent: str,
    platform: str,
    kind: str,
    base: Path,
    detector: Callable[[Path], list[Match]],
    make: Callable[[], str],
) -> Stratum:
    planted: dict[str, str] = {}
    found: dict[str, list[str]] = {}
    false = total = 0
    views: list[str] = []
    for locked in (False, True):
        w = _box(base / f"{agent}-{platform}-{kind.replace(' ', '-')}-{locked}")
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CLAUDE_CONFIG_DIR", str(w.claude))
            mp.setenv("CODEX_HOME", str(w.codex))
            mp.setenv("FLANNER_HOME", str(w.flanner))
            mp.setenv("HOME", str(w.home))
            mp.setenv("USERPROFILE", str(w.home))
            values = [make() for _ in range(PER_POSTURE)]
            for n, value in enumerate(values):
                plant(kind, agent, w, value, n, platform)
                planted[value] = expected(agent, kind, locked, platform)
            if locked:
                _locked(agent, w)
            context = default(agent, w.project)
            settings = curb_settings.resolve(context, platform=platform, root=base / "system")
            report = curb_sweep.run(
                w.project,
                detector,
                [(context, settings, BASELINE[agent])],
                home=w.home,
                env={"APPDATA": str(w.roaming)},
                platform=platform,
                key=b"corpus-key".ljust(32, b"."),
            )
        for finding in report.findings:
            total += 1
            if finding.match.secret in planted:
                found.setdefault(finding.match.secret, []).append(finding.exposure)
            else:
                false += 1
        views += [
            json.dumps(curb_sweep.redacted(report)),
            json.dumps(curb_sweep.stored(report)),
            json.dumps(curb_sweep.full(report)),
        ]
    return Stratum(agent, platform, kind, planted, found, false, total, views)


def _corpus(
    base: Path, detector: Callable[[Path], list[Match]], make: Callable[[], str]
) -> list[Stratum]:
    return [
        _run(agent, platform, kind, base, detector, make)
        for agent in (CLAUDE, CODEX)
        for platform in OSES
        for kind in TYPES[agent]
    ]


def _failures(strata: list[Stratum]) -> list[str]:
    bad = []
    for s in strata:
        right = sum(1 for value, want in s.planted.items() if want in s.found.get(value, []))
        recall = right / len(s.planted)
        false_rate = s.false / s.total if s.total else 0.0
        if len(s.planted) < 20 or recall < MIN_RECALL or false_rate > MAX_FALSE:
            missed = [v[:12] for v, want in s.planted.items() if want not in s.found.get(v, [])]
            bad.append(
                f"{s.agent}/{s.platform}/{s.kind}: {len(s.planted)} planted, recall {recall:.2f}, "
                f"false {false_rate:.2f}; missed {missed[:3]}"
            )
    return bad


def _disclosures(strata: list[Stratum]) -> list[str]:
    return [
        f"{s.agent}/{s.platform}/{s.kind}"
        for s in strata
        if any(value in view for value in s.planted for view in s.views)
    ]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> list[Stratum]:
    return _corpus(tmp_path_factory.mktemp("sweep-corpus"), stand_in, fake_token)


def test_every_stratum_meets_recall_and_false_positive_targets(corpus):
    assert len(corpus) == sum(len(TYPES[a]) for a in (CLAUDE, CODEX)) * len(OSES)
    assert not _failures(corpus), "\n".join(_failures(corpus))


def test_no_planted_value_reaches_any_output(corpus):
    assert not _disclosures(corpus)


@pytest.mark.skipif(curb_kingfisher.unavailable() is not None, reason="needs the sweep extra")
def test_kingfisher_meets_the_targets_on_real_format_secrets(tmp_path):
    strata = _corpus(tmp_path, curb_kingfisher.Detector(), github_token)
    assert not _failures(strata), "\n".join(_failures(strata))
    assert not _disclosures(strata)


def test_the_planted_github_tokens_carry_a_valid_checksum():
    value = github_token()
    body, checksum = value[4:34], value[34:]
    number = 0
    for character in checksum:
        number = number * 62 + _BASE62.index(character)
    assert len(value) == 40 and number == zlib.crc32(body.encode())
