"""Sending messages: to the devices of the people they name, and again later.

The network half of the mesh messaging plan; `mesh_messages` is the rest.
A message is pushed straight to each recipient device with the peer
`message` operation. The receiving device's answer is the acknowledgement,
and it only answers once the message is verified and stored (section 12).
A device that cannot be reached keeps the message queued here: retried
after 1, 5 and 15 minutes, then every 30, and reported failed after 24
hours.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from . import entitlements, mesh_messages, peer, peer_iroh, refusals
from . import session as cache
from .database import MeshDeliveryModel, MeshMessageModel
from .mesh_messages import MessageError

#: `(device_id, workspace_id, held) -> RemotePeer`. Tests pass one that
#: reaches a local HTTP server; everything else dials over iroh.
Dialer = Callable[[str, str, Any], Any]

#: How many devices are dialled at once, so a large workspace does not open
#: hundreds of connections together (section 5.8).
PARALLEL = 8

#: How long a send waits for acknowledgements before answering. A device
#: still being dialled then stays queued, and its next retry is answered
#: "already held" if the first attempt did land.
WAIT_SECONDS = 10.0


def dial_device(device_id: str, workspace_id: str, held: Any) -> Any:
    return peer_iroh.peer_for(device_id, workspace_id, held)


def context(*, sending: bool) -> tuple[Any, entitlements.Roster, Any]:
    """This device's session, roster and claims, or the refusal that applies.

    Sending needs a roster that is current, not merely in its grace period:
    a team list that is out of date may still name someone who has left
    (section 5.8). Reading works in grace.
    """
    current = cache.load()
    verdict = current.status() if current is not None else None
    claims = verdict.claims if verdict is not None and verdict.usable else None
    if current is None or claims is None:
        raise MessageError(
            refusals.DEVICE_UNKNOWN, "This device is not signed in to a team. Run flanner login."
        )
    if not claims.has_feature(entitlements.TEAM_SYNC):
        raise MessageError(
            refusals.SUBSCRIPTION_INACTIVE, "Messages need Team Mesh or Organization."
        )
    if not claims.has_feature(entitlements.MESH_MESSAGES):
        raise MessageError(
            refusals.MESSAGING_OFF,
            "Messaging is off for your organization. An admin can turn it on in the console.",
        )
    roster = entitlements.verify_roster(
        current.roster,
        current.keyring,
        grace=timedelta(0) if sending else entitlements.DEFAULT_GRACE,
    )
    if roster is None:
        raise MessageError(
            refusals.ROSTER_STALE,
            "Your team list is out of date. Run flanner whoami --refresh to renew it.",
        )
    return current, roster, claims


def person_view(roster: entitlements.Roster) -> Callable[[str], dict[str, str]]:
    """A user id as `{user_id, handle, name}`, from the signed roster."""
    known = {m.user_id: m for members in roster.workspaces.values() for m in members}

    def view(user_id: str) -> dict[str, str]:
        member = known.get(user_id)
        if member is None:
            return {"user_id": user_id, "handle": "", "name": ""}
        return {"user_id": user_id, "handle": member.handle, "name": member.name}

    return view


def _refused(error: peer.PeerError) -> tuple[bool, str, str]:
    """A device's refusal as (delivered, code, detail).

    A flanner from before messaging answers "unknown operation" with no code
    of its own, which reads as `unknown` and would be retried for a day as if
    the device were unreachable. It is reachable; it needs upgrading.
    """
    if error.status == 404 and str(error).startswith("unknown operation"):
        return (
            False,
            refusals.PEER_OUTDATED,
            "Their flanner on this device is too old to receive messages. "
            "It needs 0.15.0 or later.",
        )
    return (False, error.code, str(error))


def send(
    session: Session,
    *,
    body: str,
    to: list[str] | None = None,
    workspace: str | None = None,
    thread: str | None = None,
    refs: list[dict[str, Any]] | None = None,
    confirm: bool = False,
    dial: Dialer | None = None,
) -> dict[str, Any]:
    """Preview a message, or send it and report delivery (section 6.1).

    With `confirm` false nothing is signed, stored or sent: the answer says
    exactly who it would go to, so a person can check before it leaves.
    """
    current, roster, claims = context(sending=True)
    me = claims.user_id
    thread_id = None
    if thread:
        thread_id = mesh_messages.find_thread(session, thread)
        people, workspace_to, thread_ws = mesh_messages.thread_people(session, thread_id, me)
        if workspace_to:
            addressed = mesh_messages.address(roster, me=me, workspace=workspace_to)
        else:
            addressed = mesh_messages.address(
                roster, me=me, people=people, preferred_workspace=thread_ws
            )
    elif bool(to) == bool(workspace):
        raise MessageError(
            refusals.MALFORMED, "Send to named people or to one workspace.", fields=["to"]
        )
    elif workspace:
        addressed = mesh_messages.address(roster, me=me, workspace=workspace)
    else:
        users = mesh_messages.resolve_people(roster, to or [], me)
        addressed = mesh_messages.address(roster, me=me, people=users)

    mesh_messages.check_may_send(claims.role_in(addressed.workspace_id))
    text = mesh_messages.check_body(body)
    checked_refs = mesh_messages.check_refs(refs or [])
    is_workspace = isinstance(addressed.to, dict)
    moment = mesh_messages.now_utc()
    mesh_messages.check_send_rate(session, me=me, workspace=is_workspace, now=moment)
    mesh_messages.check_outbox(session)

    view = person_view(roster)
    recipients = [view(user) for user in sorted(addressed.devices)]
    if not confirm:
        return {
            "preview": True,
            "to": recipients,
            "workspace": addressed.workspace_id if is_workspace else None,
            "count": len(recipients),
            "thread_id": thread_id,
            "body": text,
        }

    envelope, payload = mesh_messages.compose(
        workspace_id=addressed.workspace_id,
        to=addressed.to,
        body=text,
        refs=checked_refs,
        thread_id=thread_id,
        user_id=me,
        organization_id=claims.organization_id,
        sent_at=moment,
    )
    mesh_messages.record_outgoing(session, envelope, payload, addressed.devices, now=moment)
    rows = session.query(MeshDeliveryModel).filter_by(message_id=envelope.artifact_id).all()
    attempt(session, rows, dial=dial, held=cache.load)
    return {
        "message_id": envelope.artifact_id,
        "thread_id": thread_id or envelope.artifact_id,
        "short": mesh_messages.short_ids([thread_id or envelope.artifact_id])[
            thread_id or envelope.artifact_id
        ],
        "delivery": [
            {**d, "user": view(d["user_id"])}
            for d in mesh_messages.delivery_by_person(session, envelope.artifact_id)
        ],
    }


def attempt(
    session: Session,
    rows: list[MeshDeliveryModel],
    *,
    dial: Dialer | None = None,
    held: Any = None,
    wait: float = WAIT_SECONDS,
) -> None:
    """Try each delivery once, a few at a time, and record what happened.

    Dialling happens on worker threads and the database only here: a
    session is not safe to share between threads. A dial still running when
    `wait` is up leaves its row queued rather than holding the caller.
    """
    dial = dial or dial_device
    held = held or cache.load
    pending = [row for row in rows if row.state == mesh_messages.QUEUED]
    outcomes: dict[int, tuple[bool, str, str]] = {}
    lock = threading.Lock()
    slots = threading.BoundedSemaphore(PARALLEL)

    work = []
    for row in pending:
        message = session.get(MeshMessageModel, row.message_id)
        if message is None:
            continue
        body = {
            "workspace_id": message.workspace_id,
            "envelope": json.loads(message.envelope),
            "payload": message.payload,
        }
        work.append((row.id, row.device_id, message.workspace_id, body))

    def run(row_id: int, device_id: str, workspace_id: str, body: dict[str, Any]) -> None:
        with slots:
            try:
                dial(device_id, workspace_id, held)._post(peer.MESSAGE, body)
                result = (True, "", "")
            except peer.PeerError as e:
                result = _refused(e)
            except Exception as e:  # noqa: BLE001 - any failure to reach it means "try later"
                result = (False, refusals.UNKNOWN, str(e))
        with lock:
            outcomes[row_id] = result

    threads = [threading.Thread(target=run, args=job, daemon=True) for job in work]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=wait)

    with lock:
        finished = dict(outcomes)
    for row in pending:
        if row.id not in finished:
            # Still dialling. Counted as an attempt so the next is spaced.
            mesh_messages.not_delivered(
                session, row, code=refusals.UNKNOWN, detail="The device did not answer in time."
            )
            continue
        ok, code, detail = finished[row.id]
        if ok:
            mesh_messages.delivered(session, row)
        else:
            mesh_messages.not_delivered(session, row, code=code, detail=detail)


def retry_due(session: Session, *, dial: Dialer | None = None) -> int:
    """Send whatever is due again. Called by the daemon and before a send."""
    rows = mesh_messages.due(session)
    if rows:
        attempt(session, rows, dial=dial)
    return len(rows)


def expire_old(session: Session) -> int:
    """Apply the organization's retention period, from the signed roster."""
    current = cache.load()
    roster = (
        entitlements.verify_roster(current.roster, current.keyring)
        if current is not None and current.roster
        else None
    )
    days = roster.message_retention_days if roster else entitlements.DEFAULT_MESSAGE_RETENTION_DAYS
    return mesh_messages.expire(session, days)
