"""The signing broker git calls for agent commits: `flanner-curb-sign` (Curb PRD §10.15).

git runs `flanner-curb-sign -Y sign -n git -f <public key> <data>` and reads
`<data>.sig`. The broker signs only while an agent session the hooks
recorded is running a shell command, with that agent's current key from
the OS credential store, and writes the commit's id, the key and the
session into the action log. Whatever else git asks of it, such as
verifying, goes to the real ssh-keygen.

It labels agent commits; it does not prove who wrote them. The developer,
or malware running as the same user, can call it inside a session.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import curb_attribution, curb_log, curb_sshsig

#: A shell tool call that started longer ago than this is not "running now".
OPEN_FOR = 30 * 60


def running_session(agent: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The agent's tool call running now: its newest record is a shell PreToolUse."""
    moment = now or time.time()
    for record in reversed(curb_log.records()):
        if record.get("kind") != "tool" or record.get("agent") != agent:
            continue
        shell = str(record.get("channel") or "").startswith("shell")
        recent = moment - float(record.get("time") or 0) <= OPEN_FOR
        return record if record.get("event") == "PreToolUse" and shell and recent else None
    return None


def _refuse(agent: str | None, why: str) -> int:
    print(f"flanner curb: not signing: {why}", file=sys.stderr)
    if agent:
        curb_log.append({"kind": "attribution", "agent": agent, "decision": "refused"})
    return 1


def _option(args: Sequence[str], flag: str) -> str | None:
    for index, value in enumerate(args[:-1]):
        if value == flag:
            return args[index + 1]
    return None


def sign(args: Sequence[str], *, now: float | None = None) -> int:
    """`-Y sign -n NAMESPACE -f KEYFILE FILE`: an SSHSIG written beside FILE."""
    namespace, key_file = _option(args, "-n"), _option(args, "-f")
    if not namespace or not key_file or len(args) < 2:
        return _refuse(None, "git called the broker without a namespace, key and file")
    data_path = Path(args[-1])
    try:
        configured = Path(key_file).read_text(encoding="utf-8")
    except OSError:
        return _refuse(None, "the configured signing key cannot be read")
    agent = curb_attribution.agent_for(configured)
    if agent is None:
        return _refuse(None, "the configured key is not an attribution key on this device")
    session = running_session(agent, now=now)
    if session is None:
        return _refuse(agent, "this commit is not inside an agent session the hooks recorded")
    key = curb_attribution.private_key(agent)
    if key is None:
        return _refuse(agent, "the agent's key is not in the OS credential store")
    data = data_path.read_bytes()
    armored = curb_sshsig.sign(key, data, namespace)
    Path(f"{data_path}.sig").write_text(armored, encoding="ascii", newline="\n")
    signed = curb_attribution.with_signature(data, armored)
    curb_log.append(
        {
            "kind": "attribution",
            "agent": agent,
            "session": str(session.get("session") or ""),
            "decision": "signed",
            "key": curb_sshsig.fingerprint(key.public_key()),
            "commit": curb_attribution.commit_id(signed) if data.startswith(b"tree ") else None,
        }
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:2] == ["-Y", "sign"]:
        return sign(args[2:])
    # Verifying and finding principals are ssh-keygen's job, unchanged.
    return subprocess.run(["ssh-keygen", *args], check=False).returncode  # noqa: S603, S607 - git's own ssh-keygen call, passed through


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
