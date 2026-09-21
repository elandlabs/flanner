"""Messages between team members: what one is, and what this device holds.

The mesh messaging plan. A message is a signed `mesh.message` artifact that
goes straight to the devices of the people it names (`mesh_delivery`) and
is kept in its own tables here, never in `artifacts`: every row there is
offered in workspace manifests, so a message stored there would reach
every teammate rather than the people it is for.

Everything a surface shows goes through this module, so the CLI, the MCP
tools and the web UI read one set of rules and one set of answers
(plan section 6.1). Nothing here talks to a network.

**A message is data, never an instruction.** Nothing in this module acts on
a body; it stores, lists and expires them.
"""

from __future__ import annotations

import difflib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import artifacts, entitlements, identity, push, refusals
from .database import MeshDeliveryModel, MeshMessageModel, MeshMuteModel

# --- limits (plan section 5.4) -------------------------------------------------

MAX_BODY_BYTES = 4096
MAX_RECIPIENTS = 20
MAX_REFS = 5
PER_PERSON_PER_MINUTE = 20
WORKSPACE_PER_HOUR = 5
OUTBOX_LIMIT = 500
#: How long a queued message keeps trying before it is reported failed.
OUTBOX_LIFETIME = timedelta(hours=24)
#: Minutes between attempts: 1, 5, 15, then every 30.
RETRY_MINUTES = (1, 5, 15, 30)
#: Characters a body may not hold even though they are not Cc: they reorder
#: text on screen, which is how a message could disguise what it says.
_BIDI_OVERRIDES = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))

QUEUED = "queued"
DELIVERED = "delivered"
FAILED = "failed"

QUIET_HOURS_FILENAME = "mesh-quiet-hours.json"


class MessageError(Exception):
    """A refusal a person can act on: a code from `refusals` and a sentence."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": True, "code": self.code, "message": self.message, **self.extra}


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stamp(moment: datetime) -> str:
    return moment.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_stamp(raw: str) -> datetime:
    moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


# --- what a message may contain ------------------------------------------------


def check_body(body: str) -> str:
    """The body as sent, or a refusal. Plain text only (plan section 5.4)."""
    if not body.strip():
        raise MessageError(refusals.MALFORMED, "A message needs some text.", fields=["body"])
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise MessageError(refusals.BODY_TOO_LARGE, "Messages are up to 4 KB.")
    for char in body:
        if char in "\n\t":
            continue
        if unicodedata.category(char) == "Cc" or char in _BIDI_OVERRIDES:
            raise MessageError(
                refusals.BODY_INVALID,
                "Messages are plain text; control characters are not allowed.",
            )
    return body


def check_refs(refs: Any) -> list[dict[str, Any]]:
    """Plan references, at most five, each `{kind, id, version}`."""
    if not refs:
        return []
    if not isinstance(refs, list):
        raise MessageError(refusals.MALFORMED, "refs must be a list.", fields=["refs"])
    if len(refs) > MAX_REFS:
        raise MessageError(refusals.TOO_MANY_REFS, "Up to 5 plans per message.")
    out = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("kind") != "plan" or not ref.get("id"):
            raise MessageError(refusals.MALFORMED, "Each ref is a plan.", fields=["refs"])
        version = ref.get("version")
        out.append(
            {
                "kind": "plan",
                "id": str(ref["id"]),
                "version": int(version) if isinstance(version, int) else None,
            }
        )
    return out


def check_recipients(to: Any) -> list[str] | dict[str, str]:
    """Named user ids (1 to 20) or `{"workspace": id}`."""
    if isinstance(to, dict):
        workspace = str(to.get("workspace") or "")
        if not workspace:
            raise MessageError(refusals.MALFORMED, "Name a workspace.", fields=["to"])
        return {"workspace": workspace}
    if not isinstance(to, list) or not to:
        raise MessageError(refusals.MALFORMED, "Name at least one person.", fields=["to"])
    if len(to) > MAX_RECIPIENTS:
        raise MessageError(
            refusals.TOO_MANY_RECIPIENTS, "Up to 20 people; for more, send to the workspace."
        )
    return sorted({str(user) for user in to})


# --- the signed artifact -------------------------------------------------------


def compose(
    *,
    workspace_id: str,
    to: list[str] | dict[str, str],
    body: str,
    refs: list[dict[str, Any]],
    thread_id: str | None,
    user_id: str,
    organization_id: str,
    sent_at: datetime | None = None,
    signing_key: Any = None,
) -> tuple[artifacts.Artifact, bytes]:
    """Sign a message. The payload travels beside the envelope, as for plans.

    A first message carries no `thread_id`: it cannot contain its own hash,
    so its id becomes the thread's id (plan section 5.7).
    """
    fields: dict[str, Any] = {
        "to": to,
        "body": body,
        "refs": refs,
        "sent_at": _stamp(sent_at or now_utc()),
    }
    if thread_id:
        fields["thread_id"] = thread_id
    payload = artifacts.canonical_bytes(fields)
    envelope = artifacts.make_artifact(
        artifact_type=artifacts.MESH_MESSAGE,
        workspace_id=workspace_id,
        content_hash=artifacts.hash_bytes(payload),
        organization_id=organization_id,
        actor_user_id=user_id,
        signing_key=signing_key,
    )
    return envelope, payload


def _store(
    session: Session,
    envelope: artifacts.Artifact,
    fields: dict[str, Any],
    payload: bytes,
    *,
    outgoing: bool,
) -> MeshMessageModel:
    row = MeshMessageModel(
        message_id=envelope.artifact_id,
        thread_id=str(fields.get("thread_id") or envelope.artifact_id),
        workspace_id=envelope.workspace_id,
        author_user_id=str(envelope.actor_user_id or ""),
        author_device_id=envelope.actor_device_id,
        recipients=json.dumps(fields["to"], sort_keys=True),
        body=str(fields["body"]),
        refs=json.dumps(fields.get("refs") or []),
        sent_at=_parse_stamp(str(fields["sent_at"])),
        outgoing=outgoing,
        read_at=now_utc() if outgoing else None,
        envelope=json.dumps(envelope.to_dict(), sort_keys=True),
        payload=payload.decode("utf-8"),
    )
    session.add(row)
    return row


def record_outgoing(
    session: Session,
    envelope: artifacts.Artifact,
    payload: bytes,
    devices: dict[str, tuple[str, ...]],
    *,
    now: datetime | None = None,
) -> MeshMessageModel:
    """Keep a sent message and one queued delivery per recipient device."""
    moment = now or now_utc()
    row = _store(session, envelope, json.loads(payload), payload, outgoing=True)
    for user_id, device_ids in devices.items():
        for device_id in device_ids:
            session.add(
                MeshDeliveryModel(
                    message_id=envelope.artifact_id,
                    user_id=user_id,
                    device_id=device_id,
                    state=QUEUED,
                    queued_at=moment,
                    next_attempt_at=moment,
                )
            )
    session.commit()
    return row


# --- receiving -----------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who sent it, as the peer layer proved: device, user, role here."""

    device_id: str
    user_id: str
    role: str


def receive(
    session: Session,
    *,
    envelope: dict[str, Any],
    payload: bytes,
    caller: Caller,
    workspace_id: str,
    me: str,
    roster: entitlements.Roster,
    public_key: str | None,
    now: datetime | None = None,
) -> str:
    """Check and keep a message another device pushed. Returns what happened.

    Every rule is applied here, on receipt, as well as when sending: the
    sender is another machine, and its checks are its own business.
    """
    moment = now or now_utc()
    try:
        artifact = artifacts.Artifact.from_dict(envelope)
    except ValueError as e:
        raise MessageError(refusals.MALFORMED, f"not a message: {e}") from None
    if artifact.artifact_type != artifacts.MESH_MESSAGE:
        raise MessageError(refusals.MALFORMED, "not a message")
    if artifact.workspace_id != workspace_id:
        raise MessageError(refusals.MALFORMED, "the message names a different workspace")
    if artifact.actor_device_id != caller.device_id or artifact.actor_user_id != caller.user_id:
        # Signed by one device and delivered by another is a relay, which
        # v1 does not have; refusing it keeps "who sent this" one fact.
        raise MessageError(refusals.BAD_SIGNATURE, "the sender is not the device delivering it")
    check_may_send(caller.role)
    if not public_key:
        raise MessageError(refusals.DEVICE_UNKNOWN, "the sending device is not known here")
    verdict = artifacts.verify_artifact(artifact, public_key, payload)
    if not verdict:
        raise MessageError(refusals.BAD_SIGNATURE, verdict.reason)

    if session.get(MeshMessageModel, artifact.artifact_id) is not None:
        return "already_held"

    try:
        fields = json.loads(payload)
        to = check_recipients(fields.get("to"))
        check_body(str(fields.get("body") or ""))
        fields["refs"] = check_refs(fields.get("refs"))
        sent_at = _parse_stamp(str(fields["sent_at"]))
    except (ValueError, KeyError, TypeError) as e:
        raise MessageError(refusals.MALFORMED, f"not a message: {e}") from None

    if sent_at < moment - timedelta(days=roster.message_retention_days):
        raise MessageError(refusals.MESSAGE_EXPIRED, "Arrived after the retention period.")

    if isinstance(to, dict):
        members = {member.user_id for member in roster.members(to["workspace"])}
        addressed = to["workspace"] == workspace_id and me in members
    else:
        addressed = me in to
    if not addressed:
        raise MessageError(refusals.NOT_A_RECIPIENT, "Not sent to this device's person.")

    _rate_check(session, sender=caller.user_id, workspace=isinstance(to, dict), now=moment)
    _store(session, artifact, fields, payload, outgoing=False)
    session.commit()
    return "accepted"


def _rate_check(session: Session, *, sender: str, workspace: bool, now: datetime) -> None:
    """20 a minute from one sender; 5 workspace messages an hour.

    Counted from what is stored, so the limit holds across processes and
    restarts without a second place to keep counts. By when this device
    stored each one, never by the sender's own `sent_at`, which a sender
    could backdate.
    """
    recent = session.query(func.count(MeshMessageModel.message_id)).filter(
        MeshMessageModel.author_user_id == sender,
        MeshMessageModel.received_at > now - timedelta(minutes=1),
    )
    if (recent.scalar() or 0) >= PER_PERSON_PER_MINUTE:
        raise MessageError(
            refusals.THROTTLED, "Too many messages. Try again in a minute.", retry_after=60
        )
    if workspace:
        hour = session.query(MeshMessageModel.recipients).filter(
            MeshMessageModel.author_user_id == sender,
            MeshMessageModel.received_at > now - timedelta(hours=1),
        )
        if sum(1 for (raw,) in hour if raw.startswith("{")) >= WORKSPACE_PER_HOUR:
            raise MessageError(
                refusals.THROTTLED,
                "Too many workspace messages. Try again within the hour.",
                retry_after=3600,
            )


def check_may_send(role: str | None) -> None:
    """Whether a role in the workspace may send messages there (section 5.2)."""
    if role is None or not push.may_send(artifacts.MESH_MESSAGE, role):
        raise MessageError(
            refusals.NO_GRANT, "You can read this workspace but not send messages in it."
        )


def check_send_rate(session: Session, *, me: str, workspace: bool, now: datetime) -> None:
    """The same limits, checked before sending, against what this device sent.

    ponytail: counts everything this device sent in the window, not per
    recipient, so it is stricter than the receiver's per-sender check.
    Split it per person if anyone ever hits it honestly.
    """
    _rate_check(session, sender=me, workspace=workspace, now=now)


def check_outbox(session: Session) -> None:
    queued = session.query(func.count(func.distinct(MeshDeliveryModel.message_id))).filter(
        MeshDeliveryModel.state == QUEUED
    )
    if (queued.scalar() or 0) >= OUTBOX_LIMIT:
        raise MessageError(
            refusals.OUTBOX_FULL, "Too many messages waiting to send. Wait for some to deliver."
        )


# --- addressing ----------------------------------------------------------------


@dataclass
class Addressed:
    """Who a message goes to, resolved against the signed roster."""

    workspace_id: str
    to: list[str] | dict[str, str]
    #: user id -> that person's devices.
    devices: dict[str, tuple[str, ...]] = field(default_factory=dict)


def resolve_people(roster: entitlements.Roster, names: list[str], me: str) -> list[str]:
    """Handles (`ben`, `@ben`) or user ids to user ids, or `member_unknown`."""
    known = {m.user_id: m for members in roster.workspaces.values() for m in members}
    by_handle = {m.handle: m.user_id for m in known.values() if m.handle}
    out: list[str] = []
    for raw in names:
        name = raw.strip().lstrip("@")
        user = by_handle.get(name.lower()) or (name if name in known else None)
        if user is None:
            close = difflib.get_close_matches(name.lower(), list(by_handle), n=3)
            hint = ", ".join(known[by_handle[h]].label for h in close)
            raise MessageError(
                refusals.MEMBER_UNKNOWN,
                f"No one called @{name} on this team."
                + (f" Did you mean {hint}?" if hint else ""),
                matches=[f"@{h}" for h in close],
            )
        if user != me and user not in out:
            out.append(user)
    if not out:
        raise MessageError(refusals.MALFORMED, "Name someone other than yourself.", fields=["to"])
    return out


def address(
    roster: entitlements.Roster,
    *,
    me: str,
    people: list[str] | None = None,
    workspace: str | None = None,
    preferred_workspace: str | None = None,
) -> Addressed:
    """Where a message goes: named people in a shared workspace, or a workspace."""
    if workspace:
        members = roster.members(workspace)
        if not any(m.user_id == me for m in members):
            raise MessageError(refusals.NO_GRANT, "You are not in that workspace.")
        others = {m.user_id: m.devices for m in members if m.user_id != me}
        return Addressed(workspace, {"workspace": workspace}, others)

    users = check_recipients(people or [])
    assert isinstance(users, list)  # noqa: S101 - a list goes in, a list comes out
    candidates = [preferred_workspace] if preferred_workspace else []
    candidates += sorted(roster.workspaces)
    for candidate in candidates:
        devices = {m.user_id: m.devices for m in roster.members(candidate)}
        if me in devices and all(user in devices for user in users):
            return Addressed(candidate, users, {user: devices[user] for user in users})
    raise MessageError(refusals.NO_GRANT, "You do not share a workspace with everyone you named.")


# --- delivery bookkeeping ------------------------------------------------------


def due(session: Session, now: datetime | None = None) -> list[MeshDeliveryModel]:
    """Deliveries worth trying now. Overdue ones are marked failed first."""
    moment = now or now_utc()
    for row in session.query(MeshDeliveryModel).filter(MeshDeliveryModel.state == QUEUED):
        if row.queued_at is not None and moment - row.queued_at > OUTBOX_LIFETIME:
            row.state = FAILED
            row.code = row.code or refusals.UPSTREAM_UNAVAILABLE
            row.detail = "Not delivered within 24 hours."
            row.next_attempt_at = None
    session.commit()
    return (
        session.query(MeshDeliveryModel)
        .filter(MeshDeliveryModel.state == QUEUED, MeshDeliveryModel.next_attempt_at <= moment)
        .all()
    )


def delivered(session: Session, row: MeshDeliveryModel, *, now: datetime | None = None) -> None:
    row.state = DELIVERED
    row.delivered_at = now or now_utc()
    row.next_attempt_at = None
    row.attempts += 1
    session.commit()


def not_delivered(
    session: Session,
    row: MeshDeliveryModel,
    *,
    code: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """A refusal that retrying cannot fix fails now; anything else waits."""
    moment = now or now_utc()
    row.attempts += 1
    row.code = code
    row.detail = detail
    retry = code == refusals.UNKNOWN or refusals.is_retryable(code)
    if not retry:
        row.state = FAILED
        row.next_attempt_at = None
    else:
        wait = RETRY_MINUTES[min(row.attempts - 1, len(RETRY_MINUTES) - 1)]
        row.next_attempt_at = moment + timedelta(minutes=wait)
    session.commit()


def delivery_by_person(session: Session, message_id: str) -> list[dict[str, Any]]:
    """Per recipient: delivered once any of their devices took it (section 12)."""
    rows = session.query(MeshDeliveryModel).filter_by(message_id=message_id).all()
    people: dict[str, list[MeshDeliveryModel]] = {}
    for row in rows:
        people.setdefault(row.user_id, []).append(row)
    out = []
    for user_id, devices in sorted(people.items()):
        done = [r for r in devices if r.state == DELIVERED]
        if done:
            first = min(r.delivered_at for r in done if r.delivered_at is not None)
            out.append({"user_id": user_id, "state": DELIVERED, "at": _stamp(first)})
        elif all(r.state == FAILED for r in devices):
            reason = devices[0]
            out.append(
                {
                    "user_id": user_id,
                    "state": FAILED,
                    "code": reason.code,
                    "message": reason.detail,
                }
            )
        else:
            out.append({"user_id": user_id, "state": QUEUED})
    return out


# --- retention -----------------------------------------------------------------


def expire(session: Session, retention_days: int, *, now: datetime | None = None) -> int:
    """Delete messages older than the organization's period, with their rows."""
    cutoff = (now or now_utc()) - timedelta(days=retention_days)
    old = [
        message_id
        for (message_id,) in session.query(MeshMessageModel.message_id).filter(
            MeshMessageModel.sent_at < cutoff
        )
    ]
    if old:
        session.query(MeshDeliveryModel).filter(MeshDeliveryModel.message_id.in_(old)).delete(
            synchronize_session=False
        )
        session.query(MeshMessageModel).filter(MeshMessageModel.message_id.in_(old)).delete(
            synchronize_session=False
        )
        session.commit()
    return len(old)


# --- mutes and quiet hours (sections 7.1, 7.2) ---------------------------------


def mute(
    session: Session, user_id: str, *, until: datetime | None = None, off: bool = False
) -> None:
    row = session.get(MeshMuteModel, user_id)
    if off:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(MeshMuteModel(user_id=user_id, until=until))
    else:
        row.until = until
    session.commit()


def muted(session: Session, *, now: datetime | None = None) -> dict[str, datetime | None]:
    moment = now or now_utc()
    return {
        row.user_id: row.until
        for row in session.query(MeshMuteModel)
        if row.until is None or row.until > moment
    }


def parse_duration(raw: str, *, now: datetime | None = None) -> datetime:
    """`8h`, `1d`, `30m`, or an ISO time, as a naive UTC moment."""
    moment = now or now_utc()
    raw = raw.strip()
    units = {"m": "minutes", "h": "hours", "d": "days"}
    if raw[:-1].isdigit() and raw[-1:] in units:
        return moment + timedelta(**{units[raw[-1]]: int(raw[:-1])})
    try:
        return _parse_stamp(raw)
    except ValueError:
        raise MessageError(
            refusals.MALFORMED, "Say how long, like 8h or 1d.", fields=["until"]
        ) from None


def _quiet_path() -> Path:
    return identity.flanner_home() / QUIET_HOURS_FILENAME


def _hhmm(raw: str) -> str:
    hours, _, minutes = raw.strip().partition(":")
    if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2):
        raise ValueError(raw)
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(raw)
    return f"{h:02d}:{m:02d}"


def set_quiet_hours(spec: str) -> dict[str, Any]:
    """`22:00-07:00` sets them on this device; `off` clears them."""
    path = _quiet_path()
    if spec.strip().lower() == "off":
        path.unlink(missing_ok=True)
        return quiet_hours()
    try:
        start_raw, end_raw = spec.split("-", 1)
        start, end = _hhmm(start_raw), _hhmm(end_raw)
    except ValueError:
        raise MessageError(
            refusals.MALFORMED, "Quiet hours look like 22:00-07:00.", fields=["set"]
        ) from None
    if start == end:
        raise MessageError(
            refusals.MALFORMED,
            "Quiet hours need a different start and end, like 22:00-07:00.",
            fields=["set"],
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"start": start, "end": end}), encoding="utf-8")
    return quiet_hours()


def quiet_hours(*, now: datetime | None = None) -> dict[str, Any]:
    """This device's quiet hours, in its local time, and whether they apply now."""
    local = (now or datetime.now()).astimezone()
    zone = local.tzname() or ""
    try:
        data = json.loads(_quiet_path().read_text(encoding="utf-8"))
        start, end = _hhmm(data["start"]), _hhmm(data["end"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"enabled": False, "start": None, "end": None, "timezone": zone, "active": False}
    clock = local.strftime("%H:%M")
    active = start <= clock < end if start < end else clock >= start or clock < end
    return {"enabled": True, "start": start, "end": end, "timezone": zone, "active": active}


# --- reading -------------------------------------------------------------------


def short_ids(ids: list[str]) -> dict[str, str]:
    """The shortest unique hex prefix of each id, at least 4 characters."""
    hexes = {i: i.split(":", 1)[-1] for i in ids}
    out = {}
    for full, hexed in hexes.items():
        length = 4
        while length < len(hexed) and any(
            other != full and o.startswith(hexed[:length]) for other, o in hexes.items()
        ):
            length += 1
        out[full] = hexed[:length]
    return out


def find_thread(session: Session, prefix: str) -> str:
    """A thread id from any unique prefix of it, with or without `sha256:`."""
    wanted = prefix.strip().split(":", 1)[-1].lower()
    if len(wanted) < 4:
        raise MessageError(refusals.MALFORMED, "Use at least 4 characters of the id.")
    threads: set[str] = {
        str(thread)
        for (thread,) in session.query(MeshMessageModel.thread_id).distinct()
        if thread.split(":", 1)[-1].startswith(wanted)
    }
    if not threads:
        raise MessageError(refusals.NOT_FOUND, f"No thread {prefix} on this device.")
    if len(threads) > 1:
        raise MessageError(
            refusals.AMBIGUOUS_ID,
            "That id matches several threads; use more characters.",
            matches=sorted(short_ids(sorted(threads)).values()),
        )
    return threads.pop()


def unread_count(session: Session) -> int:
    return int(
        session.query(func.count(MeshMessageModel.message_id))
        .filter(MeshMessageModel.outgoing.is_(False), MeshMessageModel.read_at.is_(None))
        .scalar()
        or 0
    )


def _people(rows: list[MeshMessageModel], me: str) -> tuple[list[str], str | None]:
    users: set[str] = set()
    workspace = None
    for row in rows:
        users.add(row.author_user_id)
        to = json.loads(row.recipients)
        if isinstance(to, dict):
            workspace = to.get("workspace")
        else:
            users.update(to)
    users.discard(me)
    return sorted(users), workspace


def inbox(
    session: Session,
    *,
    me: str,
    person: Any,
    include_read: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Threads, newest first, unread first unless `include_read` (section 6.1)."""
    rows = session.query(MeshMessageModel).order_by(MeshMessageModel.sent_at).all()
    threads: dict[str, list[MeshMessageModel]] = {}
    for row in rows:
        threads.setdefault(row.thread_id, []).append(row)
    ids = short_ids(list(threads))
    silenced = muted(session)
    summaries = []
    for thread_id, messages in threads.items():
        unread = sum(1 for m in messages if not m.outgoing and m.read_at is None)
        if not include_read and not unread:
            continue
        last = messages[-1]
        people, workspace = _people(messages, me)
        summaries.append(
            {
                "id": thread_id,
                "short": ids[thread_id],
                "people": [person(user) for user in people],
                "workspace": workspace,
                "refs": json.loads(messages[0].refs),
                "last": {
                    "from": person(last.author_user_id),
                    "sent_at": _stamp(last.sent_at),
                    "preview": last.body[:80],
                },
                "unread": unread,
                "muted": last.author_user_id in silenced,
            }
        )
    # Newest first, then unread ahead of read; the sort is stable.
    summaries.sort(key=lambda t: t["last"]["sent_at"], reverse=True)
    summaries.sort(key=lambda t: t["unread"] == 0)
    return {"unread": unread_count(session), "threads": summaries[:limit]}


def thread(
    session: Session,
    thread_id: str,
    *,
    me: str,
    person: Any,
    retention_days: int,
    mark_read: bool = True,
) -> dict[str, Any]:
    """Every message in a thread, oldest first, with delivery for sent ones."""
    messages = (
        session.query(MeshMessageModel)
        .filter_by(thread_id=thread_id)
        .order_by(MeshMessageModel.sent_at)
        .all()
    )
    if not messages:
        raise MessageError(refusals.NOT_FOUND, "No such thread on this device.")
    silenced = muted(session)
    people, workspace = _people(messages, me)
    out = []
    for row in messages:
        item: dict[str, Any] = {
            "id": row.message_id,
            "from": person(row.author_user_id),
            "sent_at": _stamp(row.sent_at),
            "body": row.body,
            "muted": row.author_user_id in silenced,
        }
        if row.outgoing:
            item["delivery"] = [
                {**d, "user": person(d["user_id"])}
                for d in delivery_by_person(session, row.message_id)
            ]
        out.append(item)
        if mark_read and not row.outgoing and row.read_at is None:
            row.read_at = now_utc()
    session.commit()
    return {
        "thread": {
            "id": thread_id,
            "short": short_ids([thread_id])[thread_id],
            "people": [person(user) for user in people],
            "workspace": workspace,
            "refs": json.loads(messages[0].refs),
            "complete": messages[0].message_id == thread_id,
            "messages": out,
        },
        "retention_days": retention_days,
    }


def thread_people(session: Session, thread_id: str, me: str) -> tuple[list[str], str | None, str]:
    """Who a reply goes to: everyone on the thread but the replier, or its workspace."""
    messages = session.query(MeshMessageModel).filter_by(thread_id=thread_id).all()
    if not messages:
        raise MessageError(refusals.NOT_FOUND, "No such thread on this device.")
    people, workspace = _people(messages, me)
    return people, workspace, messages[0].workspace_id
