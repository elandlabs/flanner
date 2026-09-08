"""The rules that make accepting unsolicited data survivable.

The transport tests in test_peer.py prove a push crosses two devices. These
prove the limits around it, which are easier to get wrong and harder to
notice: a cap that is checked after the allocation it exists to prevent, or
a rate limit that quietly turns into a permanent ban.
"""

import pytest

from flanner import artifacts, push, sync
from flanner.workflow import COMMENTER, EDITOR, MAINTAINER, READER

# --- who may send what -----------------------------------------------------


@pytest.mark.parametrize(
    "artifact_type,role,allowed",
    [
        (artifacts.COMMENT, READER, False),
        (artifacts.COMMENT, COMMENTER, True),
        (artifacts.COMMENT, EDITOR, True),
        (artifacts.REVIEW_DECISION, COMMENTER, True),
        (artifacts.PLAN_VERSION, COMMENTER, False),
        (artifacts.PLAN_VERSION, EDITOR, True),
        (artifacts.PLAN_VERSION, MAINTAINER, True),
        (artifacts.REVIEW_PROPOSAL, COMMENTER, False),
        (artifacts.REVIEW_PROPOSAL, EDITOR, True),
    ],
)
def test_the_role_needed_to_send_each_kind(artifact_type, role, allowed):
    assert push.may_send(artifact_type, role) is allowed


def test_a_reader_may_send_nothing_at_all():
    """The role exists precisely to be unable to write."""
    assert not any(push.may_send(t, READER) for t in artifacts.ARTIFACT_TYPES)


def test_an_unrecognised_artifact_type_is_refused_rather_than_waved_through():
    """A new type has to say who may send it, and until it does, nobody can."""
    assert not push.may_send("something.invented", MAINTAINER)


def test_every_artifact_type_this_build_knows_has_a_rule():
    """Otherwise adding a type silently makes it unpushable, which is safe
    but confusing, and the confusion is what gets papered over with a
    permissive default."""
    assert set(push.REQUIRED_ROLE) == set(artifacts.ARTIFACT_TYPES)


# --- the feature, as distinct from the role ---------------------------------


def a_memory(artifact_type=artifacts.MEMORY_RECORD):
    return {
        "envelope": {
            "artifact_id": "art_mem",
            "artifact_type": artifact_type,
            "workspace_id": "ws_1",
        },
        "payload": "The staging cluster is rebuilt every Sunday night.",
    }


@pytest.mark.parametrize("artifact_type", [artifacts.MEMORY_RECORD, artifacts.MEMORY_TOMBSTONE])
def test_memory_is_refused_when_the_entitlement_does_not_cover_it(artifact_type):
    """An organization switched it off, or nobody is paying. Refused here as
    well as withheld on the read path, because a gate on one direction only
    is not a gate."""
    report = push.accept(
        None,
        [a_memory(artifact_type)],
        workspace_id="ws_1",
        role=MAINTAINER,
        resolve_key=None,
        memory_sync=False,
    )

    assert report.accepted == []
    assert report.rejected == [("art_mem", "this entitlement does not include memory sync")]


def test_each_artifact_is_refused_for_its_own_reason():
    """Per artifact, so one over-reaching item does not discard the rest,
    and so a reader can tell a lapsed feature from a missing role."""
    plan = {
        "envelope": {
            "artifact_id": "art_plan",
            "artifact_type": artifacts.PLAN_VERSION,
            "workspace_id": "ws_1",
        },
        "payload": "# a plan",
    }

    report = push.accept(
        None,
        [a_memory(), plan],
        workspace_id="ws_1",
        role=COMMENTER,
        resolve_key=None,
        memory_sync=False,
    )

    assert report.rejected == [
        ("art_mem", "this entitlement does not include memory sync"),
        ("art_plan", "a commenter may not push a plan.version"),
    ]


def test_sharing_a_memory_asks_for_the_role_a_comment_asks_for():
    """Not the one that can move what a colleague's tools read. Saying the
    staging cluster rebuilds on Sundays is not proposing a plan change."""
    assert push.may_send(artifacts.MEMORY_RECORD, COMMENTER) is True
    assert push.may_send(artifacts.MEMORY_RECORD, READER) is False


# --- caps ------------------------------------------------------------------


def an_item(payload=""):
    return {"envelope": {"artifact_id": "a"}, "payload": payload}


def test_an_empty_push_is_refused():
    assert push.check_batch([]) == "a push has to carry something"


def test_too_many_artifacts_in_one_push_is_refused():
    reason = push.check_batch([an_item() for _ in range(sync.MAX_PUSH_BATCH + 1)])
    assert reason is not None and "per push" in reason


def test_a_batch_at_the_limit_is_allowed():
    assert push.check_batch([an_item() for _ in range(sync.MAX_PUSH_BATCH)]) is None


def test_too_many_bytes_in_one_push_is_refused():
    """Count alone is not enough: fifty maximum-size artifacts are within
    the count limit and would still be a very large allocation."""
    fat = an_item("x" * (sync.MAX_PUSH_BYTES // 2 + 1))
    reason = push.check_batch([fat, fat])
    assert reason is not None and "bytes per push" in reason


def test_the_size_cap_is_measured_before_anything_is_decoded():
    """A cap that allocates in order to decide is not a cap. Payloads here
    are still strings off the wire, never bytes objects."""
    huge = an_item("x" * (sync.MAX_PUSH_BYTES + 1))
    assert push.check_batch([huge]) is not None


# --- rate limiting ---------------------------------------------------------


def test_a_device_may_push_up_to_the_limit():
    limiter = push.RateLimiter(window=60.0, limit=3)
    assert [limiter.allow("dev", now=t) for t in (0.0, 1.0, 2.0)] == [True, True, True]


def test_pushing_past_the_limit_is_refused():
    limiter = push.RateLimiter(window=60.0, limit=3)
    for t in (0.0, 1.0, 2.0):
        limiter.allow("dev", now=t)
    assert limiter.allow("dev", now=3.0) is False


def test_a_refused_push_does_not_extend_its_own_lockout():
    """Recording refused attempts would turn a rate limit into a ban that a
    client retrying in a loop could never escape."""
    limiter = push.RateLimiter(window=10.0, limit=2)
    limiter.allow("dev", now=0.0)
    limiter.allow("dev", now=1.0)
    for t in range(2, 10):  # hammering while both originals are still inside
        assert limiter.allow("dev", now=float(t)) is False
    # Both originals have now aged out, and none of the eight refusals in
    # between were recorded, so the device is free at exactly the moment it
    # would have been had it not hammered at all.
    assert limiter.allow("dev", now=11.0) is True


def test_the_window_slides():
    limiter = push.RateLimiter(window=10.0, limit=2)
    limiter.allow("dev", now=0.0)
    limiter.allow("dev", now=5.0)
    assert limiter.allow("dev", now=9.0) is False
    assert limiter.allow("dev", now=11.0) is True  # the first has aged out


def test_one_noisy_device_cannot_lock_out_the_team():
    """The limit is per device on purpose."""
    limiter = push.RateLimiter(window=60.0, limit=2)
    limiter.allow("loud", now=0.0)
    limiter.allow("loud", now=1.0)
    assert limiter.allow("loud", now=2.0) is False
    assert limiter.allow("quiet", now=2.0) is True


# --- learning a new teammate's key -----------------------------------------
#
# A new teammate's key is not in this device's keyring until it signs in
# again, so their first push fails verification. The refresh that fixes it is
# triggered by a remote party, which is why it is bounded.


def test_a_refresh_is_offered_only_when_the_author_is_unknown():
    assert sync.is_unknown_author(artifacts.Verdict(False, f"{sync.UNKNOWN_AUTHOR} dev_x (…)"))
    assert not sync.is_unknown_author(artifacts.Verdict(False, "bad signature"))
    assert not sync.is_unknown_author(artifacts.Verdict(True))


def test_the_cooldown_allows_one_refresh_then_holds():
    cool = push.Cooldown(window=300.0)
    assert cool.allow(now=0.0) is True
    assert cool.allow(now=1.0) is False
    assert cool.allow(now=299.0) is False
    assert cool.allow(now=301.0) is True


def test_the_cooldown_is_not_per_device():
    """Counting per device would let a peer reset the floor by inventing a
    new id, which is the move the floor exists to stop."""
    cool = push.Cooldown(window=300.0)
    assert cool.allow(now=0.0) is True
    # No device argument exists to vary, which is the point; a second call
    # from anywhere at all is refused.
    assert cool.allow(now=5.0) is False


def test_a_refusal_that_is_not_about_keys_never_triggers_a_refresh():
    calls = []
    report = push.accept(
        object(),
        [{"envelope": {"artifact_id": "a", "workspace_id": "ws", "artifact_type": "comment"}}],
        workspace_id="other-ws",
        role=MAINTAINER,
        resolve_key=lambda _: None,
        refresh_keys=lambda: calls.append("refreshed"),
    )
    assert report.rejected and "different workspace" in report.rejected[0][1]
    assert "refreshed" not in calls


def test_a_control_plane_that_cannot_be_reached_leaves_the_refusal_alone():
    """A push we merely could not verify must not become an error."""

    def explode():
        raise OSError("control plane unreachable")

    assert push._relearn(explode, push.Cooldown()) is None


def test_no_refresher_configured_means_no_retry():
    assert push._relearn(None, push.Cooldown()) is None


def test_the_cooldown_blocks_the_fetch_itself_not_just_the_retry():
    """Otherwise the call still goes out and only its result is discarded."""
    calls = []
    spent = push.Cooldown(window=300.0)
    spent.allow()  # real clock, because `_relearn` reads the real clock too
    assert push._relearn(lambda: calls.append(1), spent) is None
    assert calls == []
