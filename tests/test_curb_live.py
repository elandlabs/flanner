"""What the web UI holds in memory between requests: slow reads, long jobs, messages."""

import threading

import pytest

from flanner import curb_live


@pytest.fixture(autouse=True)
def fresh():
    curb_live.reset()
    yield
    curb_live.reset()


def test_a_slow_read_is_done_in_the_background_and_kept():
    gate, calls = threading.Event(), []

    def work():
        gate.wait(5)
        calls.append(1)
        return "answer"

    memo = curb_live.Memo()
    first = memo.get("k", work, max_age=60)
    assert first.value is None and first.busy
    gate.set()
    memo.settle()
    again = memo.get("k", work, max_age=60)
    assert again.value == "answer" and not again.busy and calls == [1]


def test_a_stale_answer_is_shown_while_the_next_is_worked_out():
    answers = iter(["old", "new"])
    memo = curb_live.Memo()
    memo.get("k", lambda: next(answers), max_age=60)
    memo.settle()
    memo.stale()
    during = memo.get("k", lambda: next(answers), max_age=60)
    assert during.value == "old"
    memo.settle()
    assert memo.get("k", lambda: "unused", max_age=60).value == "new"


def test_a_failed_read_keeps_the_last_answer_and_says_why():
    memo = curb_live.Memo()
    memo.get("k", lambda: "good", max_age=60)
    memo.settle()
    memo.stale()

    def broken():
        raise OSError("disk went away")

    memo.get("k", broken, max_age=60)
    memo.settle()
    held = memo.get("k", broken, max_age=60)
    assert held.value == "good" and held.error == "disk went away"


def test_a_failure_is_said_without_the_file_it_names():
    def denied():
        raise PermissionError(13, "Permission denied", "C:/Users/someone/.aws/credentials")

    def lost():
        raise ValueError("cannot read /home/someone/.ssh/id_ed25519")

    memo = curb_live.Memo()
    memo.get("a", denied, max_age=60)
    memo.get("b", lost, max_age=60)
    memo.settle()
    assert memo.get("a", denied, max_age=60).error == "Permission denied"
    assert memo.get("b", lost, max_age=60).error == "ValueError"


def test_a_job_reports_its_own_progress_and_runs_one_at_a_time():
    gate = threading.Event()

    def work(job):
        job.step(1, 4)
        gate.wait(5)
        return "found"

    job = curb_live.Job()
    assert job.start(work, label="scanning")
    assert job.running and not job.start(work)
    gate.set()
    job.settle()
    assert (job.state, job.done, job.total, job.result) == ("done", 1, 4, "found")


def test_a_failed_job_keeps_the_last_result():
    job = curb_live.Job()
    job.start(lambda _: "first")
    job.settle()

    def broken(_):
        raise RuntimeError("the scanner stopped")

    job.start(broken)
    job.settle()
    assert job.state == "failed" and job.error == "the scanner stopped"
    assert job.result == "first"


def test_a_message_is_heard_once_and_only_by_its_browser():
    curb_live.say("one", "Applied 2 fixes.")
    curb_live.say(None, "nobody")
    assert curb_live.heard("two") is None
    assert curb_live.heard("one") == ("success", "Applied 2 fixes.")
    assert curb_live.heard("one") is None
    for number in range(curb_live.MAX_SAID + 5):
        curb_live.say(str(number), "x")
    assert curb_live.heard("0") is None and curb_live.heard(str(curb_live.MAX_SAID + 4))


def test_a_quick_read_is_kept_until_something_changes():
    calls = []
    read = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert curb_live.kept("k", read, seconds=60) == 1
    assert curb_live.kept("k", read, seconds=60) == 1
    curb_live.changed()
    assert curb_live.kept("k", read, seconds=60) == 2
