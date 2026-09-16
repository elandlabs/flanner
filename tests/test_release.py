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


def test_going_backwards_is_not_an_upgrade(home):
    """Pinning back is a decision somebody made, not news to announce."""
    release.remember_version("0.12.0")
    assert release.upgraded_from("0.11.0") is None


def test_two_metadata_records_do_not_announce_forever(home):
    """An editable install whose dist-info is stale reports two versions.

    The command entry point and an import from the source tree can
    disagree. Announcing both directions would fire on every other
    command, for good.
    """
    seen = []
    for reported in ("0.11.0", "0.12.0") * 3:
        was = release.upgraded_from(reported)
        release.remember_version(reported)
        if was:
            seen.append((was, reported))
    assert seen == [("0.11.0", "0.12.0")], seen


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


def _seed(**values):
    release.write_state({**release.read_state(), **values})


def test_nothing_is_fetched_before_anybody_has_been_asked(home, monkeypatch):
    """The sidebar says nothing leaves your disk. Consent comes first."""
    monkeypatch.setattr(release, "_spawn_check", lambda: pytest.fail("spawned without consent"))
    assert release.refresh_in_background() is False
    assert release.known_newer("0.0.1") is None


def test_declining_is_remembered_and_silences_everything(home, monkeypatch):
    release.set_update_check_consent(False)
    _seed(latest="9.9.9")
    monkeypatch.setattr(release, "_spawn_check", lambda: pytest.fail("spawned after a no"))
    assert release.refresh_in_background() is False
    assert release.known_newer("0.0.1") is None
    assert release.update_check_consent() is False


def test_a_command_never_waits_on_the_network(home, monkeypatch):
    """known_newer reads the cache and nothing else."""
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", lambda: pytest.fail("fetched inline"))
    assert release.known_newer("0.11.0") is None
    _seed(latest="0.12.0")
    assert release.known_newer("0.11.0") == "0.12.0"


def test_being_current_says_nothing(home):
    release.set_update_check_consent(True)
    _seed(latest="0.11.0")
    assert release.known_newer("0.11.0") is None


def test_a_refresh_is_started_once_a_day_even_when_it_fails(home, monkeypatch):
    """No network means one try a day, not one per command."""
    spawned = []
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_spawn_check", lambda: spawned.append(1) or True)

    assert release.refresh_in_background() is True
    assert release.refresh_in_background() is False
    assert len(spawned) == 1

    later = datetime.now(timezone.utc) + release.CHECK_EVERY + timedelta(minutes=1)
    assert release.refresh_in_background(now=later) is True
    assert len(spawned) == 2


def test_a_fresh_answer_is_not_refreshed(home, monkeypatch):
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", lambda: "0.12.0")
    release.fetch_and_store()
    monkeypatch.setattr(release, "_spawn_check", lambda: pytest.fail("refreshed a fresh answer"))
    assert release.refresh_in_background() is False


def test_the_child_records_what_it_found(home, monkeypatch):
    release.set_update_check_consent(True)
    monkeypatch.setattr(release, "_fetch_latest", lambda: "0.12.0")
    assert release.fetch_and_store() == "0.12.0"
    assert release.read_state()["latest"] == "0.12.0"
    assert release.known_newer("0.11.0") == "0.12.0"


def test_a_failed_lookup_keeps_the_last_answer(home, monkeypatch):
    """Not hearing about a release beats forgetting one already known."""
    release.set_update_check_consent(True)
    _seed(latest="0.12.0")
    monkeypatch.setattr(release, "_fetch_latest", lambda: None)
    assert release.fetch_and_store() is None
    assert release.known_newer("0.11.0") == "0.12.0"


def test_the_notice_is_repeated_once_a_day_not_once_a_command(home):
    assert release.due_to_tell() is True
    release.mark_told()
    assert release.due_to_tell() is False
    tomorrow = datetime.now(timezone.utc) + release.TELL_EVERY + timedelta(minutes=1)
    assert release.due_to_tell(now=tomorrow) is True


def test_a_corrupt_timestamp_counts_as_due(home):
    _seed(told_at="whenever", checked_at="never", attempted_at=7)
    assert release.due_to_tell() is True


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


# --- turning it on and off ------------------------------------------------------


def test_updates_command_changes_the_answer_init_recorded(home):
    """A yes at init has to be reversible without editing a json file."""
    from click.testing import CliRunner

    from flanner.cli import cli

    runner = CliRunner()
    shown = runner.invoke(cli, ["updates"])
    assert shown.exit_code == 0
    assert "Not decided yet" in shown.output

    on = runner.invoke(cli, ["updates", "on"])
    assert on.exit_code == 0
    assert release.update_check_consent() is True
    assert "flanner updates off" in on.output

    off = runner.invoke(cli, ["updates", "off"])
    assert off.exit_code == 0
    assert release.update_check_consent() is False
    assert "Nothing here reaches the network unasked" in off.output


def test_turning_it_off_silences_a_notice_already_cached(home):
    release.set_update_check_consent(True)
    _seed(latest="99.0.0")
    assert release.known_newer("0.11.0") == "99.0.0"
    release.set_update_check_consent(False)
    assert release.known_newer("0.11.0") is None
