"""R3's fix corpus: 100% rollback and zero config corruption (Curb PRD §15, §16).

Every launch posture in the R1 corpus that sets user settings, on each OS,
gets planned fixes applied with a stand-in grant. Each one must:

- leave the settings file parsing to exactly the data the fix meant;
- leave no channel broader than before, by the same reach assessment;
- be undone byte for byte.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from flanner import curb_approval, curb_fix, curb_reach, curb_settings, curb_tighten
from flanner.curb_approval import Broker
from flanner.curb_context import BASELINE, CLAUDE, CODEX, default
from tests import test_curb_corpus as corpus

RANK = {"absent": 0, "controlled": 1, "informational": 1, "unknown": 2, "uncontrolled": 3}
PROFILE = frozenset({corpus.AWS, corpus.DOTENV, corpus.ENV})


class Yes:
    name, weak = "test prompt", False

    def available(self):
        return True

    def confirm(self, reason):
        return True


def _postures(agent):
    postures = corpus.CLAUDE_POSTURES if agent == CLAUDE else corpus.CODEX_POSTURES
    return [p for p in postures if p.user is not None and not p.argv and not p.doubt]


def _assess(agent, w, platform, creds):
    context = default(agent, w.dotenv)
    return curb_reach.assess(
        context,
        curb_settings.resolve(context, platform=platform, root=w.system),
        creds,
        platform=platform,
        home=w.homes[PROFILE],
        env=corpus._environment(PROFILE),
        version=BASELINE[agent],
    )


def _run(agent, w, platform, monkeypatch):
    failures, applied = [], 0
    extra = {name for p in _postures(agent) for name in p.files}
    creds = corpus.curb_credentials.find(
        w.homes[PROFILE], w.dotenv, corpus._environment(PROFILE), platform
    )
    for posture in _postures(agent):
        corpus._place(agent, posture, w, False, extra)
        before = _assess(agent, w, platform, creds)
        planned = curb_fix.plan(
            [before], home=w.homes[PROFILE], platform=platform, env=corpus._environment(PROFILE)
        )
        if not planned.edits:
            continue
        originals = {
            e.path: e.path.read_bytes() if e.path.exists() else None for e in planned.edits
        }
        broker = Broker(Yes())
        grant = broker.request("fix", curb_approval.change_hash(planned.change()))
        folder = curb_fix.apply(planned, broker, grant)
        applied += 1
        name = f"{agent}/{platform}/{posture.name}"
        for edit in planned.edits:
            if curb_tighten.load(edit.path) != edit.after:
                failures.append(f"{name}: {edit.path.name} does not parse as meant")
        after = _assess(agent, w, platform, creds)
        was = {c.key: c.state for c in before.channels}
        for channel in after.channels:
            if RANK[channel.state] > RANK[was.get(channel.key, "absent")]:
                failures.append(f"{name}: {channel.key} got broader")
        broker = Broker(Yes())
        grant = broker.request("undo", curb_approval.change_hash(curb_fix.undo_change(folder)))
        curb_fix.undo(broker, grant, folder)
        for path, data in originals.items():
            now = path.read_bytes() if path.exists() else None
            if now != data:
                failures.append(f"{name}: {path.name} was not restored byte for byte")
    return failures, applied


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
def test_every_fix_rolls_back_cleanly_and_corrupts_nothing(agent, tmp_path, monkeypatch):
    if agent == CODEX and sys.version_info < (3, 11):
        pytest.skip("Codex config is TOML")
    w = corpus._workspace(tmp_path / "ws")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(w.claude))
    monkeypatch.setenv("CODEX_HOME", str(w.codex))
    failures, applied = [], 0
    for platform in corpus.OSES:
        found, count = _run(agent, w, platform, monkeypatch)
        failures += found
        applied += count
    assert applied >= 10, f"only {applied} fixes applied"
    assert not failures, "\n".join(failures[:20])


def test_the_corpus_reaches_both_file_formats():
    assert _postures(CLAUDE) and _postures(CODEX)
    assert all(isinstance(p.name, str) for p in _postures(CLAUDE))
    assert Path(corpus.__file__).name == "test_curb_corpus.py"
