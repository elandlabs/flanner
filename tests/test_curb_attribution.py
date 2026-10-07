"""Agent commit attribution (Curb PRD §10.15): keys, the broker, the registry, five states.

Covers the R7 client criteria: an agent commit verifies with its key; the
broker refuses a commit outside an agent session; registration carries a
proof of possession; the registry is refused when older, changed at the
same version, moving a key or dropping a revocation; known revocations
hold offline; and "key status unknown" without a fresh registry.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flanner import (
    curb_attribution,
    curb_log,
    curb_signer,
    curb_sshsig,
    curb_store,
    curb_wire,
    identity,
)
from tests.test_curb_policy import ISSUER, ISSUER_RING, ORG, signer, stamp

needs_toml = pytest.mark.skipif(sys.version_info < (3, 11), reason="no tomllib")
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")


def entry(key, status="active", device="dev_a", agent="claude"):
    return {
        "fingerprint": curb_sshsig.fingerprint(key.public_key()),
        "public_key": base64.b64encode(curb_sshsig.raw_public(key.public_key())).decode(),
        "agent": agent,
        "device_id": device,
        "status": status,
        "changed_at": stamp(NOW),
    }


def registry(version, entries, *, expires=NOW + timedelta(days=7), org=ORG):
    fields = {
        "kind": curb_wire.REGISTRY,
        "key_id": "iss",
        "organization_id": org,
        "version": version,
        "issued_at": stamp(NOW - timedelta(hours=1)),
        "expires_at": stamp(expires),
        "keys": entries,
    }
    return curb_wire.encode(fields, signer(ISSUER))


def accept(token):
    return curb_attribution.accept_registry(token, ISSUER_RING, ORG)


# --- keys ---------------------------------------------------------------------------------


def test_a_key_lives_only_in_the_os_credential_store():
    public = curb_attribution.create("claude", now=1000)
    held = (curb_store.curb_dir() / "attribution.json").read_text(encoding="utf-8")
    key = curb_attribution.private_key("claude")
    assert key is not None and key.public_key() == public
    seed = key.private_bytes_raw().hex()
    assert seed not in held and "private" not in held
    assert not list(curb_store.curb_dir().glob("*.secret"))


def test_without_a_keychain_there_is_no_key(monkeypatch):
    monkeypatch.setattr(identity, "_keychain", lambda: None)
    with pytest.raises(curb_attribution.NoKeychain):
        curb_attribution.create("claude")


def test_rotation_retires_the_key_and_deletes_its_private_half():
    old = curb_attribution.create("claude", now=1000)
    retired, new = curb_attribution.rotate("claude", now=2000)
    assert retired == curb_sshsig.fingerprint(old) and new != old
    assert curb_attribution.private_key("claude").public_key() == new
    held = curb_attribution.keys()
    assert held["retired"][0]["fingerprint"] == retired
    assert held["keys"]["claude"]["replaces"] == retired
    assert curb_attribution.agent_for(curb_sshsig.openssh_line(old)) == "claude"


def test_rotation_is_due_after_ninety_days():
    curb_attribution.create("codex", now=0)
    assert curb_attribution.due(now=89 * 86400) == []
    assert curb_attribution.due(now=91 * 86400) == ["codex"]


def test_registration_proves_possession_of_the_key():
    curb_attribution.create("claude")
    body = curb_attribution.registration("claude", "dev_a")
    proof = curb_attribution.proof_bytes("dev_a", body["public_key"], body["nonce"], "")
    public = identity.load_public_key(body["public_key"])
    public.verify(base64.b64decode(body["proof"]), proof)  # raises if it does not verify
    again = curb_attribution.registration("claude", "dev_a")
    assert again["nonce"] != body["nonce"]


# --- the registry ---------------------------------------------------------------------------

A = Ed25519PrivateKey.from_private_bytes(bytes([31]) * 32)
B = Ed25519PrivateKey.from_private_bytes(bytes([32]) * 32)


def test_a_registry_is_kept_only_when_it_verifies_for_this_organization():
    assert accept(registry(1, [entry(A)]))[1] == ""
    forged = curb_wire.encode(curb_wire.fields_of(registry(2, [])), signer(A))
    assert "does not verify" in accept(forged)[1]
    assert "another organization" in accept(registry(3, [], org="org_other"))[1]


def test_an_older_registry_is_refused():
    accept(registry(2, [entry(A)]))
    assert "older" in accept(registry(1, [entry(A)]))[1]


def test_the_same_version_must_match_and_may_only_extend_expiry():
    accept(registry(2, [entry(A)]))
    later = NOW + timedelta(days=10)
    assert accept(registry(2, [entry(A)], expires=later))[1] == ""
    assert "without a new version" in accept(registry(2, [entry(A), entry(B)]))[1]


def test_a_newer_registry_may_not_drop_a_revocation_or_move_a_key():
    accept(registry(1, [entry(A, "revoked"), entry(B, device="dev_b")]))
    assert (
        "drops a known revocation" in accept(registry(2, [entry(A), entry(B, device="dev_b")]))[1]
    )
    assert "drops a known revocation" in accept(registry(2, [entry(B, device="dev_b")]))[1]
    moved = [entry(A, "revoked"), entry(B, device="dev_c")]
    assert "moves a key" in accept(registry(2, moved))[1]
    assert curb_attribution.revoked() == {curb_sshsig.fingerprint(A.public_key())}


# --- states ----------------------------------------------------------------------------------


def signed_commit(key, message=b"agent change\n"):
    payload = (
        b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
        b"author Agent <agent@example.com> 1759400000 +0000\n"
        b"committer Agent <agent@example.com> 1759400000 +0000\n\n" + message
    )
    return curb_attribution.with_signature(payload, curb_sshsig.sign(key, payload))


def verdict(raw, listing=None, now=NOW):
    return curb_attribution.state_of(raw, listing, curb_attribution.revoked(), now)


def test_the_five_states():
    listing, _ = accept(registry(1, [entry(A), entry(B, "retired", device="dev_b")]))
    stranger = Ed25519PrivateKey.from_private_bytes(bytes([33]) * 32)
    assert verdict(signed_commit(A), listing).state == curb_attribution.ATTRIBUTED
    assert verdict(signed_commit(B), listing).state == curb_attribution.RETIRED
    assert verdict(signed_commit(stranger), listing).state == curb_attribution.UNATTRIBUTED
    assert verdict(b"tree x\n\nunsigned\n", listing).state == curb_attribution.UNATTRIBUTED
    tampered = signed_commit(A).replace(b"agent change", b"other change")
    assert verdict(tampered, listing).state == curb_attribution.UNATTRIBUTED
    later = NOW + timedelta(days=8)
    assert verdict(signed_commit(A), listing, later).state == curb_attribution.UNKNOWN
    assert verdict(signed_commit(A), None).state == curb_attribution.UNKNOWN  # never fetched


def test_a_revocation_seen_once_holds_offline_and_forever():
    accept(registry(1, [entry(A, "revoked"), entry(B)]))
    later = NOW + timedelta(days=30)  # the registry has long expired
    held = curb_attribution.registry(ISSUER_RING)
    assert verdict(signed_commit(A), held, later).state == curb_attribution.REVOKED
    assert verdict(signed_commit(B), held, later).state == curb_attribution.UNKNOWN


def test_signature_header_round_trips_and_names_the_commit():
    raw = signed_commit(A)
    payload, armored = curb_attribution.split_signature(raw)
    assert curb_attribution.with_signature(payload, armored) == raw
    assert curb_sshsig.verify(armored, payload) is not None


@needs_git
def test_an_agent_commit_verifies_with_its_key_in_a_real_repository(tmp_path):
    git = ["git", "-C", str(tmp_path)]
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Agent",
        "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "Agent",
        "GIT_COMMITTER_EMAIL": "a@x",
    }
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "unsigned"], check=True, env=env)
    payload = curb_attribution.raw_commit("HEAD", tmp_path)
    signed = curb_attribution.with_signature(payload, curb_sshsig.sign(A, payload))
    written = (
        subprocess.run(
            [*git, "hash-object", "-t", "commit", "-w", "--stdin"],
            input=signed,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    assert written == curb_attribution.commit_id(signed)
    listing, _ = accept(registry(1, [entry(A)]))
    shas = curb_attribution.commits(written, tmp_path)
    assert shas == [written]
    assert (
        verdict(curb_attribution.raw_commit(written, tmp_path), listing).state
        == curb_attribution.ATTRIBUTED
    )


# --- the broker ------------------------------------------------------------------------------


def session_record(agent="claude", event="PreToolUse", channel="shell_files", when=None):
    curb_log.append(
        {
            "kind": "tool",
            "agent": agent,
            "session": "s-9",
            "event": event,
            "tool": "Bash",
            "channel": channel,
        },
        now=when,
    )


def call_broker(tmp_path, agent="claude"):
    public = curb_attribution.keys()["keys"][agent]["public"]
    key_file = tmp_path / "key.pub"
    key_file.write_text(f"{public}\n", encoding="utf-8")
    data = tmp_path / "buffer"
    data.write_bytes(
        b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\nauthor A <a@x> 0 +0000\n\nmsg\n"
    )
    code = curb_signer.main(["-Y", "sign", "-n", "git", "-f", str(key_file), str(data)])
    return code, data


def test_the_broker_refuses_a_commit_outside_an_agent_session(tmp_path, capsys):
    curb_attribution.create("claude")
    code, data = call_broker(tmp_path)
    assert code == 1 and not Path(f"{data}.sig").exists()
    assert "not inside an agent session" in capsys.readouterr().err
    session_record(event="PostToolUse")  # a finished tool call is no session
    assert call_broker(tmp_path)[0] == 1
    assert curb_log.records()[-1]["decision"] == "refused"


def test_the_broker_signs_inside_a_session_and_logs_the_commit(tmp_path):
    curb_attribution.create("claude")
    session_record()
    code, data = call_broker(tmp_path)
    assert code == 0
    armored = Path(f"{data}.sig").read_text(encoding="ascii")
    signer_key = curb_sshsig.verify(armored, data.read_bytes())
    assert (
        curb_sshsig.fingerprint(signer_key)
        == curb_attribution.keys()["keys"]["claude"]["fingerprint"]
    )
    record = curb_log.records()[-1]
    assert (record["kind"], record["decision"], record["session"]) == (
        "attribution",
        "signed",
        "s-9",
    )
    assert record["commit"] == curb_attribution.commit_id(
        curb_attribution.with_signature(data.read_bytes(), armored)
    )
    assert curb_log.verify()[0]


def test_the_broker_signs_with_the_agents_current_key_after_rotation(tmp_path):
    curb_attribution.create("claude")
    old_public = curb_attribution.keys()["keys"]["claude"]["public"]
    curb_attribution.rotate("claude")
    session_record()
    key_file = tmp_path / "old.pub"
    key_file.write_text(old_public, encoding="utf-8")  # agent settings still name the old key
    data = tmp_path / "buffer"
    data.write_bytes(b"tree x\n\nmsg\n")
    assert curb_signer.main(["-Y", "sign", "-n", "git", "-f", str(key_file), str(data)]) == 0
    signed_by = curb_sshsig.verify(
        Path(f"{data}.sig").read_text(encoding="ascii"), data.read_bytes()
    )
    assert (
        curb_sshsig.fingerprint(signed_by)
        == curb_attribution.keys()["keys"]["claude"]["fingerprint"]
    )


def test_other_ssh_keygen_operations_pass_through(monkeypatch):
    seen = []
    monkeypatch.setattr(
        curb_signer.subprocess,
        "run",
        lambda argv, check: seen.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    assert (
        curb_signer.main(["-Y", "verify", "-n", "git", "-f", "allowed", "-I", "x", "-s", "sig"])
        == 0
    )
    assert seen[0][0] == "ssh-keygen" and seen[0][1:3] == ["-Y", "verify"]


@needs_ssh_keygen
def test_ssh_keygen_accepts_the_brokers_signature(tmp_path):
    curb_attribution.create("claude")
    session_record()
    code, data = call_broker(tmp_path)
    assert code == 0
    allowed = tmp_path / "allowed"
    allowed.write_text(
        "agent " + curb_attribution.keys()["keys"]["claude"]["public"] + "\n",
        encoding="ascii",
        newline="\n",
    )
    with data.open("rb") as message:
        checked = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(allowed),
                "-I",
                "agent",
                "-n",
                "git",
                "-s",
                f"{data}.sig",
            ],
            stdin=message,
            capture_output=True,
            check=False,
        )
    assert checked.returncode == 0, checked.stderr


# --- setting agents up -------------------------------------------------------------------------


@needs_toml
def test_setup_routes_each_agents_commits_through_the_broker(tmp_path, monkeypatch):
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    (codex / "config.toml").write_text('# mine\nmodel = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    for agent in ("claude", "codex"):
        curb_attribution.create(agent)
    plan = curb_attribution.setup_plan(["claude", "codex"], Path("/opt/flanner-curb-sign"))
    by_agent = {}
    for edit in plan.edits:
        by_agent.setdefault(edit.agent, []).append(edit)
    claude_edit = next(e for e in by_agent["claude"] if e.path.name == "settings.json")
    env = claude_edit.after["env"]
    assert env["GIT_CONFIG_KEY_1"] == "gpg.ssh.program" and env["GIT_CONFIG_VALUE_3"] == "true"
    assert env["GIT_CONFIG_VALUE_2"].startswith("key::ssh-ed25519 ")
    assert "PreToolUse" in claude_edit.after["hooks"]  # the broker needs recorded sessions
    codex_edit = next(e for e in by_agent["codex"] if e.path.name == "config.toml")
    assert codex_edit.text.startswith("# mine\n")
    assert codex_edit.after["shell_environment_policy"]["set"]["GIT_CONFIG_KEY_0"] == "gpg.format"
    assert json.dumps(plan.change()).count("BEGIN") == 0  # no private material anywhere
