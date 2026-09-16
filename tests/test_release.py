"""What version ran last, and whether a newer one has been published."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from flanner import release


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway flanner home, so no test can read or write the real one."""
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "fh"))
    return tmp_path / "fh"


# --- what changed ------------------------------------------------------------


def test_a_first_install_is_not_an_upgrade(home):
    """Arriving is not upgrading; release notes for a version you never had."""
    assert release.upgraded_from("0.11.0") is None


def test_the_version_that_ran_last_is_reported_once(home):
    release.remember_version("0.10.0")
    assert release.upgraded_from("0.11.0") == "0.10.0"

    release.remember_version("0.11.0")
    assert release.upgraded_from("0.11.0") is None


def test_an_unreadable_state_file_is_treated_as_empty(home):
    home.mkdir(parents=True)
    (home / release.STATE_FILE).write_text("{not json", encoding="utf-8")
    assert release.read_state() == {}
    release.remember_version("0.11.0")
    assert release.read_state()["version"] == "0.11.0"


# --- is there a newer one ----------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "than", "newer"),
    [
        ("0.12.0", "0.11.0", True),
        ("0.11.1", "0.11.0", True),
        ("1.0.0", "0.99.0", True),
        ("0.11.0", "0.11.0", False),
        ("0.9.0", "0.11.0", False),
        ("0.12.0", "not-a-version", False),
        ("", "0.11.0", False),
    ],
)
def test_version_comparison(candidate, than, newer):
    assert release.is_newer(candidate, than) is newer


def test_nothing_is_fetched_before_anybody_has_been_asked(home, monkeypatch):
    """The sidebar says nothing leaves your disk. Consent comes first."""
    monkeypatch.setattr(
        release, "_fetch_latest", lambda: pytest.fail("asked pypi without consent")
    )
    assert release.newer_release("0.0.1") is None


def test_declining_is_remembered_and_silences_the_check(home, monkeypatch):
    release.set_update_check_consent(False)
    monkeypatch.setattr(release, "_fetch_latest", lambda: pytest.fail("asked after a no"))
    assert release.newer_release("0.0.1") is None
    assert release.update_check_consent() is False


def test_a_newer_release_is_reported_and_cached(home, monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return "0.12.0"

    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", fetch)

    assert release.newer_release("0.11.0") == "0.12.0"
    assert release.newer_release("0.11.0") == "0.12.0"
    assert len(calls) == 1, "the cache should hold for a day"


def test_the_cache_expires(home, monkeypatch):
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", lambda: "0.12.0")
    release.newer_release("0.11.0")

    stale = datetime.now(timezone.utc) - release.CHECK_EVERY - timedelta(minutes=1)
    state = release.read_state()
    state["checked_at"] = stale.isoformat()
    release.write_state(state)

    monkeypatch.setattr(release, "_fetch_latest", lambda: "0.13.0")
    assert release.newer_release("0.11.0") == "0.13.0"


def test_being_current_says_nothing(home, monkeypatch):
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", lambda: "0.11.0")
    assert release.newer_release("0.11.0") is None


def test_a_failed_lookup_is_silent(home, monkeypatch):
    """Not hearing about a release beats an error nobody can act on."""
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", lambda: None)
    assert release.newer_release("0.11.0") is None


def test_a_corrupt_timestamp_does_not_raise(home, monkeypatch):
    release.set_update_check_consent(True)
    release.write_state({"update_check": True, "latest": "0.12.0", "checked_at": "whenever"})
    monkeypatch.setattr(release, "_fetch_latest", lambda: "0.12.0")
    assert release.newer_release("0.11.0") == "0.12.0"


def test_the_reply_shape_is_checked(home, monkeypatch):
    """A mirror that answers with something unexpected must not crash a command."""
    import urllib.request

    class Reply:
        def read(self):
            return json.dumps({"info": {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Reply())
    assert release._fetch_latest() is None
