"""Scrubbing (Curb PRD §10.3, R3): same-length placeholders, parse checks, no backup."""

import json
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from flanner import curb_approval, curb_kingfisher, curb_scrub, curb_store
from flanner.cli import cli
from flanner.curb_approval import Broker, NoGrant
from flanner.curb_kingfisher import Match
from flanner.curb_scrub import ScrubFailed

SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
TOKEN = re.compile(r"ghp_[A-Za-z0-9]{36}")


def detect(path: Path) -> list[Match]:
    text = path.read_text(encoding="utf-8")
    return [Match("github", "GitHub token", 1, m) for m in TOKEN.findall(text)]


class Yes:
    name, weak = "test prompt", False

    def available(self):
        return True

    def confirm(self, reason):
        return True


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(curb_approval, "process_chain", lambda: [])


def granted(scrub):
    broker = Broker(Yes())
    return broker, broker.request("scrub", curb_approval.change_hash(scrub.change()))


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_a_transcript_is_scrubbed_line_by_line_and_stays_json(tmp_path):
    lines = [
        json.dumps({"type": "user", "text": f"use {SECRET} please"}),
        json.dumps({"type": "assistant", "text": "ok"}),
        json.dumps({"type": "tool", "output": f"export GH={SECRET}"}),
    ]
    path = write(tmp_path / "session.jsonl", "\n".join(lines) + "\n")
    size = path.stat().st_size
    scrub = curb_scrub.plan(path, detect)
    assert (scrub.secrets, scrub.lines) == (1, 2)
    curb_scrub.apply(scrub, *granted(scrub))
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text and "CURB-SCRUBBED" in text
    assert path.stat().st_size == size  # same length
    assert all(json.loads(line) for line in text.splitlines())


def test_a_replacement_that_breaks_a_line_leaves_the_file_unchanged(tmp_path):
    # Here the "secret" swallows a closing quote, so replacing it breaks the JSON.
    def greedy(path):
        return [Match("x", "X", 1, 'ab"}')]

    path = write(tmp_path / "s.jsonl", json.dumps({"k": "ab"}) + "\n")
    before = path.read_bytes()
    with pytest.raises(ScrubFailed, match="no longer be JSON"):
        curb_scrub.plan(path, greedy)
    assert path.read_bytes() == before


def test_an_escaped_copy_that_cannot_be_replaced_fails_the_scrub(tmp_path):
    secret = "tok/en_" + "x" * 20

    def finder(path):
        return [Match("x", "X", 1, secret)]

    path = write(
        tmp_path / "notes.md", f"{secret}\n" + json.dumps(secret).replace("/", "\\/") + "\n"
    )
    with pytest.raises(ScrubFailed, match="escaped form"):
        curb_scrub.plan(path, finder)


def test_env_toml_and_yaml_files_must_still_parse(tmp_path):
    env = write(tmp_path / ".env", f"GH_TOKEN={SECRET}\nOTHER=1\n")
    curb_scrub.apply(scrub := curb_scrub.plan(env, detect), *granted(scrub))
    assert env.read_text(encoding="utf-8").startswith("GH_TOKEN=CURB-SCRUBBED")
    if sys.version_info >= (3, 11):
        toml = write(tmp_path / "config.toml", f'[mcp_servers.gh.env]\nTOKEN = "{SECRET}"\n')
        curb_scrub.apply(scrub := curb_scrub.plan(toml, detect), *granted(scrub))
        assert SECRET not in toml.read_text(encoding="utf-8")


def test_without_a_grant_nothing_is_written(tmp_path):
    path = write(tmp_path / "history", f"curl -H 'Authorization: {SECRET}'\n")
    before = path.read_bytes()
    scrub = curb_scrub.plan(path, detect)
    with pytest.raises(NoGrant):
        curb_scrub.apply(scrub, Broker(Yes()), None)
    assert path.read_bytes() == before


def test_a_file_changed_after_it_was_read_is_left_alone(tmp_path):
    path = write(tmp_path / "history", f"{SECRET}\n")
    scrub = curb_scrub.plan(path, detect)
    write(path, f"{SECRET}\nnew line\n")
    with pytest.raises(ScrubFailed, match="changed since"):
        curb_scrub.apply(scrub, *granted(scrub))
    assert path.read_text(encoding="utf-8") == f"{SECRET}\nnew line\n"


def test_no_copy_of_a_scrubbed_secret_remains(tmp_path):
    path = write(tmp_path / "history", f"{SECRET}\n")
    scrub = curb_scrub.plan(path, detect)
    assert SECRET.encode() not in scrub.new and SECRET not in repr(scrub)
    curb_scrub.apply(scrub, *granted(scrub))
    leftovers = [
        p for p in tmp_path.rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()
    ]
    assert leftovers == []
    assert not (curb_store.curb_dir() / "backups").exists()


def test_a_short_secret_becomes_stars():
    assert curb_scrub.placeholder(5) == b"*****"
    assert curb_scrub.placeholder(16) == b"CURB-SCRUBBED***"


def test_the_command_counts_asks_and_names_no_secret(tmp_path, monkeypatch):
    path = write(tmp_path / "history", f"export GH={SECRET}\n")
    monkeypatch.setattr(curb_kingfisher, "unavailable", lambda: None)
    monkeypatch.setattr(curb_kingfisher, "Detector", lambda: detect)
    monkeypatch.setattr(curb_approval, "method", lambda: Yes())
    dry = CliRunner().invoke(cli, ["curb", "scrub", str(path), "--dry-run"])
    assert dry.exit_code == 0 and "1 secret(s) on 1 line(s)" in dry.output
    assert SECRET in path.read_text(encoding="utf-8")
    done = CliRunner().invoke(cli, ["curb", "scrub", str(path)])
    assert done.exit_code == 0, done.output
    assert SECRET not in done.output and SECRET not in path.read_text(encoding="utf-8")
