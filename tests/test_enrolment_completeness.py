"""A freshly enrolled device must be able to reach its teammates.

This is the workflow the product advertises, and it did not work. Three
things were wrong in the same few lines, each invisible on its own:

- `_session_from` dropped `relay_url`, so the relay fallback never engaged.
- Nothing ever populated `device_keys`, so `peer pull dev_...` could not
  turn a teammate's id into the key needed to dial them.
- The error message recommended `whoami --refresh`, which renewed the
  entitlement and deliberately preserved the empty key cache.

None of the unit tests noticed, because each one tested a part that worked.
"""

from __future__ import annotations

from typing import Any

from flanner import account
from flanner import session as cache
from flanner.session import Session


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "device_id": "dev_self",
        "organization_id": "org_1",
        "user_id": "maria",
        "entitlement": "token",
        "keyring": {"sk_1": "issuerkey"},
        **extra,
    }


def test_the_relay_url_is_kept() -> None:
    """Dropped on arrival, so the documented fallback never activated."""
    built = account._session_from("http://x.test", _body(relay_url="https://relay.example"))
    assert built.relay_url == "https://relay.example"


def test_a_response_without_the_optional_fields_still_works() -> None:
    """Every control plane in the wild today sends neither."""
    built = account._session_from("http://x.test", _body())
    assert built.relay_url == ""
    assert built.device_keys == {}


def test_device_keys_are_read_if_a_control_plane_ever_sends_them() -> None:
    """None does today. Reading anyway means the day one starts, this works."""
    built = account._session_from("http://x.test", _body(device_keys={"dev_a": "keya"}))
    assert built.device_keys == {"dev_a": "keya"}


def test_login_learns_the_organization_device_keys(monkeypatch) -> None:
    """The defect: enrol, then be unable to dial a single colleague."""
    posted: list[str] = []

    def fake_post(endpoint, path, payload, *, repeatable=False):
        posted.append(path)
        if path == "/v1/devices/keyring":
            return {"devices": {"dev_alice": "alicekey", "dev_bob": "bobkey"}}
        return _body()

    monkeypatch.setattr(account, "_post", fake_post)
    session = account.login("a-code", endpoint="http://x.test")

    assert "/v1/devices/enroll" in posted
    assert "/v1/devices/keyring" in posted, "enrolment did not learn any peers"
    assert session.device_keys == {"dev_alice": "alicekey", "dev_bob": "bobkey"}
    assert cache.load().device_keys == session.device_keys, "not persisted"


def test_accepting_an_invitation_learns_them_too(monkeypatch) -> None:
    """The other way in. Both paths, or one of them strands the user."""

    def fake_post(endpoint, path, payload, *, repeatable=False):
        if path == "/v1/devices/keyring":
            return {"devices": {"dev_alice": "alicekey"}}
        return _body()

    monkeypatch.setattr(account, "_post", fake_post)
    session = account.accept_invitation("tok", user_id="maria", endpoint="http://x.test")
    assert session.device_keys == {"dev_alice": "alicekey"}


def test_a_failed_key_fetch_does_not_fail_the_enrolment(monkeypatch) -> None:
    """Enrolment has already succeeded by then.

    Failing it because a second request did not land would trade a working
    install for a missing convenience, and leave the device with no session
    at all rather than one that needs a follow-up command.
    """

    def fake_post(endpoint, path, payload, *, repeatable=False):
        if path == "/v1/devices/keyring":
            raise account.SessionError("network down")
        return _body()

    monkeypatch.setattr(account, "_post", fake_post)
    session = account.login("a-code", endpoint="http://x.test")
    assert session.device_id == "dev_self"
    assert session.device_keys == {}
    assert cache.load() is not None, "the enrolment itself was lost"


def _held_with_alice() -> None:
    cache.save(
        Session(
            endpoint="http://x.test",
            device_id="dev_self",
            organization_id="org_1",
            user_id="maria",
            entitlement="old",
            device_keys={"dev_alice": "alicekey"},
        )
    )


def test_refresh_replaces_the_device_keys_rather_than_copying_them(monkeypatch) -> None:
    """Copying them forward kept a removed teammate's key trusted.

    Renewal was the one call a device makes regularly, and it was the one
    that never looked at who was still in the organization.
    """
    _held_with_alice()

    def fake_post(endpoint, path, payload, *, repeatable=False):
        if path == "/v1/devices/keyring":
            return {"devices": {"dev_bob": "bobkey"}}
        return _body(entitlement="new")

    monkeypatch.setattr(account, "_post", fake_post)
    renewed = account.refresh()
    assert renewed.entitlement == "new"
    assert renewed.device_keys == {"dev_bob": "bobkey"}, "alice was removed and is still trusted"
    assert cache.load().device_keys == {"dev_bob": "bobkey"}, "not persisted"


def test_refresh_keeps_the_keys_it_had_when_the_key_fetch_fails(monkeypatch) -> None:
    """The renewal has already landed by then.

    Dropping the cache here would break artifact verification offline until
    the next fetch that works, which is worse than a stale list.
    """
    _held_with_alice()

    def fake_post(endpoint, path, payload, *, repeatable=False):
        if path == "/v1/devices/keyring":
            raise account.SessionError("network down")
        return _body(entitlement="new")

    monkeypatch.setattr(account, "_post", fake_post)
    renewed = account.refresh()
    assert renewed.entitlement == "new"
    assert renewed.device_keys == {"dev_alice": "alicekey"}


def test_the_recommended_recovery_command_refreshes_the_directory() -> None:
    """Six messages point at `whoami --refresh` for "cannot resolve device".

    Read off the source rather than driven, because the assertion is about
    the command doing both halves — and a version that renews only the
    entitlement passes every behavioural test while helping nobody.
    """
    import inspect

    from flanner import cli

    # `.callback` because click wraps the function in a Command object, and
    # asking inspect for the source of a Command is a TypeError rather than
    # the thing under test.
    source = inspect.getsource(cli.whoami.callback)
    assert "account.refresh()" in source
    assert "account.fetch_device_keys()" in source, (
        "whoami --refresh renews the entitlement but not the device keys, "
        "which is the thing every error message recommends it for"
    )
