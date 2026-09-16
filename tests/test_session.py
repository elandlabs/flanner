"""Getting an entitlement onto a device, and living without one."""

import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from flanner import account, identity, refusals, tui
from flanner import session as cache
from flanner.entitlements import EXPIRED, IN_GRACE, MALFORMED, VALID


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path))
    return tmp_path


def a_session(**overrides):
    defaults = dict(
        endpoint="https://api.example.test",
        device_id="dev_abc",
        organization_id="org_1",
        user_id="maria",
        entitlement="claims.signature",
        keyring={"sk_1": "key"},
    )
    return cache.Session(**{**defaults, **overrides})


# --- the cache -------------------------------------------------------------


def test_a_saved_session_survives_a_round_trip(home):
    cache.save(a_session())
    loaded = cache.load()
    assert loaded == a_session()


def test_nothing_saved_means_not_logged_in(home):
    assert cache.load() is None


@pytest.mark.skipif(os.name != "posix", reason="Windows has no POSIX mode bits")
def test_the_cache_is_readable_only_by_its_owner(home):
    """It is not a secret worth stealing, but it is nobody else's business."""
    path = cache.save(a_session())
    mode = stat.S_IMODE(path.stat().st_mode)
    assert not mode & (stat.S_IRGRP | stat.S_IROTH), oct(mode)


def test_the_cache_is_created_restricted_rather_than_widened_afterwards(home, monkeypatch):
    """Runs everywhere, including where mode bits are not enforced.

    Creating then chmod-ing would leave a window where the file is readable,
    so the mode has to be an argument to the create itself.
    """
    seen = []
    real_open = os.open
    monkeypatch.setattr(
        os,
        "open",
        lambda path, flags, *mode: (seen.append(mode), real_open(path, flags, *mode))[1],
    )
    cache.save(a_session())
    assert seen and all(mode == (0o600,) for mode in seen), seen


def test_a_corrupt_cache_reads_as_never_logged_in(home):
    cache.session_path().write_text("{ not json", encoding="utf-8")
    assert cache.load() is None


def test_a_truncated_cache_reads_as_never_logged_in(home):
    """Missing fields must not crash a command that only wanted the device id."""
    cache.session_path().write_text(json.dumps({"user_id": "maria"}), encoding="utf-8")
    assert cache.load() is None


def test_saving_leaves_no_temporary_file_behind(home):
    cache.save(a_session())
    assert sorted(p.name for p in home.iterdir()) == ["session.json"]


def test_logging_out_forgets_the_session_but_keeps_the_device_key(home):
    identity.load_or_create_device_key()
    before = identity.device_id()
    cache.save(a_session())

    assert cache.clear() is True
    assert cache.load() is None
    assert identity.device_id() == before, "the same machine must not look like a new one"


def test_logging_out_twice_is_harmless(home):
    assert cache.clear() is False


# --- the network edge ------------------------------------------------------


def test_login_requires_a_code(home):
    with pytest.raises(account.SessionError, match="code is required"):
        account.login("   ", endpoint="https://api.example.test")


def test_a_non_http_endpoint_is_refused(home):
    """Otherwise urllib would happily open a file:// URL."""
    with pytest.raises(account.SessionError, match="not an http endpoint"):
        account.login("code", endpoint="file:///etc/passwd")


def test_refreshing_without_a_session_says_so(home):
    with pytest.raises(account.SessionError, match="not logged in"):
        account.refresh()


def test_an_unusable_response_is_reported_not_cached(home):
    with pytest.raises(account.SessionError, match="unusable"):
        account._session_from("https://api.example.test", {"device_id": "dev_abc"})


# --- graded expiry ---------------------------------------------------------


def test_a_session_with_no_token_reports_malformed(home):
    assert a_session(entitlement="").status().status == MALFORMED


def test_ensure_fresh_returns_nothing_when_never_logged_in(home):
    assert account.ensure_fresh() is None


def test_ensure_fresh_does_not_call_out_for_a_valid_entitlement(home, monkeypatch):
    """A working entitlement is used as-is; the network is not consulted."""
    held = a_session()
    held.roster = "a signed roster"
    cache.save(held)
    monkeypatch.setattr(
        account, "refresh", lambda *a, **k: pytest.fail("should not have refreshed")
    )
    monkeypatch.setattr(cache.Session, "status", lambda self, **k: _verdict(VALID))
    assert account.ensure_fresh() is not None


def test_a_session_from_before_rosters_renews_once(home, monkeypatch):
    """Otherwise teammates stay invisible until the entitlement next expires."""
    cache.save(a_session())
    renewed = []
    monkeypatch.setattr(account, "refresh", lambda current: renewed.append(current) or current)
    monkeypatch.setattr(cache.Session, "status", lambda self, **k: _verdict(VALID))
    account.ensure_fresh()
    assert len(renewed) == 1


@pytest.mark.parametrize("state", [IN_GRACE, EXPIRED, MALFORMED])
def test_an_unreachable_control_plane_falls_back_to_the_cache(home, monkeypatch, state):
    """Losing the network must cost team features later, not this command now."""
    saved = a_session()
    cache.save(saved)
    monkeypatch.setattr(cache.Session, "status", lambda self, **k: _verdict(state))
    monkeypatch.setattr(
        account, "refresh", _raising(account.SessionError("could not reach the control plane"))
    )
    assert account.ensure_fresh() == saved


def test_renewing_on_use_falls_back_to_the_cached_session_when_offline(home, monkeypatch):
    """What pull, push and `peer serve` call first. Offline must not stop them.

    Through the real `refresh`, not a stand-in, so the key fetch it now
    makes is on the path too.
    """
    from flanner import cli

    cache.save(a_session())
    monkeypatch.setattr(account, "_post", _raising(account.SessionError("no route to host")))
    assert cli._renew_on_use() == a_session()
    assert cache.load() == a_session(), "a failed renewal must not touch the cache"


def _verdict(state):
    from flanner.entitlements import Verdict

    return Verdict(state)


def _raising(error):
    def fail(*args, **kwargs):
        raise error

    return fail


# --- what a signed request proves ------------------------------------------


def test_a_refresh_request_is_signed_by_the_device_key(home):
    from flanner.device_auth import sign_request, verify_request

    request = sign_request({"why": "refresh"})
    assert verify_request(request, identity.device_public_key_b64())


def test_a_request_signed_by_another_key_does_not_verify(home, tmp_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from flanner.device_auth import sign_request, verify_request

    impostor = Ed25519PrivateKey.generate()
    request = sign_request({}, device_id=identity.device_id(), signing_key=impostor)
    result = verify_request(request, identity.device_public_key_b64())
    assert not result and "signature" in result.reason


def test_changing_the_body_after_signing_invalidates_it(home):
    """The signature covers the arguments, not just the identity."""
    from dataclasses import replace

    from flanner.device_auth import sign_request, verify_request

    request = sign_request({"workspace_id": "ws_mine"})
    tampered = replace(request, body={"workspace_id": "ws_yours"})
    assert not verify_request(tampered, identity.device_public_key_b64())


def test_a_captured_request_stops_working(home):
    from flanner.device_auth import sign_request, verify_request

    request = sign_request({})
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    result = verify_request(request, identity.device_public_key_b64(), now=later)
    assert not result and "out of date" in result.reason
    # The reason has to name the cause, not only the measurement: this
    # refusal is nearly always two clocks disagreeing, not an attack.
    assert "clocks" in result.reason and "Sync the clock" in result.reason


def test_a_request_without_a_nonce_is_refused(home):
    from dataclasses import replace

    from flanner.device_auth import sign_request, verify_request

    request = replace(sign_request({}), nonce="")
    assert not verify_request(request, identity.device_public_key_b64())


def test_two_requests_never_share_a_nonce(home):
    from flanner.device_auth import sign_request

    assert sign_request({}).nonce != sign_request({}).nonce


def test_a_garbled_request_is_refused_rather_than_raising(home):
    from flanner.device_auth import SignedRequest, verify_request

    request = SignedRequest("dev_x", "not-a-date", "n", {}, "sig")
    assert not verify_request(request, identity.device_public_key_b64())


def test_a_request_missing_a_field_is_reported_as_malformed():
    from flanner.device_auth import SignedRequest

    with pytest.raises(ValueError, match="Malformed"):
        SignedRequest.from_dict({"device_id": "dev_x"})


# --- the CLI ---------------------------------------------------------------


def test_whoami_without_an_account_does_not_look_like_a_failure(home):
    """Most users never log in. That is a supported state, not an error."""
    from click.testing import CliRunner

    from flanner.cli import cli

    result = CliRunner(env={"FLANNER_HOME": str(home)}).invoke(cli, ["whoami"])
    assert result.exit_code == 0
    assert "not signed in" in result.output
    assert identity.device_id() in result.output


def test_login_reports_a_refusal_and_exits_nonzero(home):
    from click.testing import CliRunner

    from flanner.cli import cli

    result = CliRunner(env={"FLANNER_HOME": str(home)}).invoke(
        cli, ["login", "bad-code", "--endpoint", "http://127.0.0.1:1"]
    )
    assert result.exit_code == 1
    assert tui.CROSS in result.output


def test_logout_is_quiet_when_there_is_nothing_to_forget(home):
    from click.testing import CliRunner

    from flanner.cli import cli

    result = CliRunner(env={"FLANNER_HOME": str(home)}).invoke(cli, ["logout"])
    assert result.exit_code == 0
    assert "Not signed in" in result.output


def test_the_session_file_never_holds_the_device_private_key(home):
    """A cached entitlement is copyable; the thing that makes it usable is not."""
    identity.load_or_create_device_key()
    cache.save(a_session())
    assert "PRIVATE KEY" not in cache.session_path().read_text(encoding="utf-8")
    # The key lives in the OS keychain now, so its absence from the home is
    # expected. What matters here is that it is still there to be loaded.
    assert identity.device_id().startswith(identity.DEVICE_ID_PREFIX)


def test_whoami_refresh_asks_even_when_the_entitlement_is_still_valid(home, monkeypatch):
    """Found by dogfooding: ensure_fresh would skip the call.

    Someone runs `whoami --refresh` precisely to pick up a grant made a
    moment ago, while the held entitlement is still perfectly valid. Routing
    that through the renew-only-if-stale path made the flag a no-op.
    """
    from click.testing import CliRunner

    from flanner import account
    from flanner.cli import cli

    cache.save(a_session())
    called = []
    monkeypatch.setattr(account, "refresh", lambda *a, **k: called.append(1) or a_session())
    monkeypatch.setattr(
        account, "ensure_fresh", lambda **k: pytest.fail("--refresh must not be a heuristic")
    )

    result = CliRunner(env={"FLANNER_HOME": str(home)}).invoke(cli, ["whoami", "--refresh"])
    assert result.exit_code == 0
    assert called, "the flag did not renew anything"


def test_whoami_refresh_falls_back_to_the_cache_when_offline(home, monkeypatch):
    from click.testing import CliRunner

    from flanner import account
    from flanner.cli import cli

    cache.save(a_session())
    monkeypatch.setattr(account, "refresh", _raising(account.SessionError("no route to host")))

    result = CliRunner(env={"FLANNER_HOME": str(home)}).invoke(cli, ["whoami", "--refresh"])
    assert result.exit_code == 0
    assert "could not renew" in result.output
    assert "maria" in result.output, "the cached entitlement is still reported"


# --- the console ------------------------------------------------------------


def test_every_console_call_needs_a_session(home):
    """Each of these signs with the device key, so there is nothing to sign with."""
    from flanner import account

    calls = [
        (account.request_enrollment_code, ()),
        (account.list_devices, ()),
        (account.revoke_device, ("dev_x",)),
        (account.invite_member, ("someone@example.test",)),
        (account.list_members, ()),
        (account.remove_member, ("mem_x",)),
    ]
    for call, args in calls:
        with pytest.raises(account.SessionError, match="not logged in"):
            call(*args)


def test_accepting_an_invitation_needs_both_a_token_and_a_user_id(home):
    from flanner import account

    with pytest.raises(account.SessionError, match="invitation token"):
        account.accept_invitation("  ", user_id="raj", endpoint="https://api.example.test")
    with pytest.raises(account.SessionError, match="user id"):
        account.accept_invitation("tok", user_id="  ", endpoint="https://api.example.test")


def test_console_calls_sign_the_arguments_not_just_the_caller(home, monkeypatch):
    """A captured revoke must not be replayable against a different device."""
    from flanner import account
    from flanner.device_auth import SignedRequest, verify_request

    cache.save(a_session(device_id=identity.device_id()))
    sent = {}

    def capture(endpoint, path, payload, *, repeatable=False):
        # `payload` is a function now, not a dict: a nonce is spent on use,
        # so every attempt has to sign afresh. Calling it here is what the
        # real `_post` does per attempt.
        sent.update(payload() if callable(payload) else payload)
        return {}

    monkeypatch.setattr(account, "_post", capture)
    account.revoke_device("dev_lost")

    request = SignedRequest.from_dict(sent)
    assert request.body == {"device_id": "dev_lost"}
    assert verify_request(request, identity.device_public_key_b64())

    from dataclasses import replace

    tampered = replace(request, body={"device_id": "dev_someone_else"})
    assert not verify_request(tampered, identity.device_public_key_b64())


def test_the_console_commands_report_a_refusal_rather_than_a_traceback(home):
    from click.testing import CliRunner

    from flanner.cli import cli

    runner = CliRunner(env={"FLANNER_HOME": str(home)})
    for args in (
        ["devices", "list"],
        ["devices", "add"],
        ["devices", "revoke", "dev_x"],
        ["invite", "someone@example.test"],
        ["members"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 1, args
        assert tui.CROSS in result.output and "not logged in" in result.output, args
        assert result.exception is None or isinstance(result.exception, SystemExit), args


def test_a_rate_limited_refusal_says_when_to_come_back(home):
    """The server works out the wait; throwing it away makes the message worse."""
    import urllib.error
    from email.message import Message

    from flanner import account

    headers = Message()
    headers["Retry-After"] = "12"
    error = urllib.error.HTTPError(
        "https://api.example.test/v1/entitlements",
        429,
        "Too Many Requests",
        headers,
        io.BytesIO(b'{"detail": "too many requests; slow down"}'),
    )
    detail, code = account._refusal(error)
    assert "slow down" in detail
    assert "12s" in detail
    # Falls back to a code derived from nothing, because this refusal
    # carried none. An older control plane looks exactly like this.
    assert code == refusals.UNKNOWN


def test_a_refusal_without_a_retry_hint_reads_cleanly(home):
    import urllib.error
    from email.message import Message

    from flanner import account

    error = urllib.error.HTTPError(
        "https://api.example.test/v1/entitlements",
        403,
        "Forbidden",
        Message(),
        io.BytesIO(b'{"detail": "this device may not act on this organization"}'),
    )
    detail, _ = account._refusal(error)
    assert detail == "this device may not act on this organization"
