"""Why the control plane said no, in a word a program can match on.

Part of the public wire format, and here rather than in the control plane
for the same reason `entitlements` is: a client has to understand what it is
told, and anything a client must understand cannot live somewhere only we
can read.

## The problem this solves

Until now a refusal carried an English sentence and nothing else. That reads
well and is useless to a program: the only way to tell "your subscription
lapsed" from "your clock is wrong" was to match on prose. So improving the
wording of any message was, strictly, a breaking change — and the wording
most worth improving is the wording that is currently confusing somebody.

A code fixes the direction of that pressure. The sentence can be rewritten
whenever it would help a person, because no program depends on it.

## The rules

**A code is permanent.** It may be deprecated and it may stop being sent,
but it must never be reused to mean something else. A client from two years
ago that still matches on it must not be wrong.

**A code says what happened, not what to do.** `SUBSCRIPTION_INACTIVE`, not
`GO_AND_PAY`: the advice depends on who is asking, and the client is better
placed to decide that than we are.

**Unknown codes are normal.** A newer control plane will send codes an older
client has never heard of, so every consumer must have a path for "I do not
recognise this" that falls back to the message. `UNKNOWN` exists to be that
path's value and is never sent over the wire.
"""

from __future__ import annotations

from typing import Final

# --- the caller is not who they say they are --------------------------------

#: The signature did not verify against the device's recorded key.
BAD_SIGNATURE: Final = "bad_signature"

#: The request is outside the freshness window. Almost always a clock, not an
#: attack, which is why it is told apart from a bad signature.
CLOCK_SKEW: Final = "clock_skew"

#: This exact signed request has already been used.
REPLAYED: Final = "replayed"

#: No such device, or it is no longer enrolled. Deliberately one code for
#: both: telling them apart would say whether a device id exists.
DEVICE_UNKNOWN: Final = "device_unknown"

#: The device is enrolled but its membership is not active any more.
MEMBERSHIP_INACTIVE: Final = "membership_inactive"

# --- the caller is who they say, and still may not ---------------------------

#: The action needs an organization admin.
NOT_ADMIN: Final = "not_admin"

#: The subscription does not cover this. A `plan` field says what is held.
SUBSCRIPTION_INACTIVE: Final = "subscription_inactive"

#: No workspace grant covers what was asked for.
NO_GRANT: Final = "no_grant"

# --- bootstrap ---------------------------------------------------------------

#: An invitation or enrolment code that is unknown, spent, or expired. One
#: code for all three on purpose: distinguishing them would let an
#: unauthenticated caller learn that a code exists.
CODE_UNUSABLE: Final = "code_unusable"

# --- the request itself ------------------------------------------------------

#: The body did not have the shape this route requires. A `fields` list names
#: what was wrong.
MALFORMED: Final = "malformed"

#: Too many requests. `Retry-After` says when to come back.
THROTTLED: Final = "throttled"

#: No such route, or no such thing at the route. Coarse on purpose: it is
#: what an unmatched path produces, and inventing precision for a case
#: nobody wrote a message for would imply precision that is not there.
NOT_FOUND: Final = "not_found"

# --- our end -----------------------------------------------------------------

#: Something this deployment has not configured, such as billing.
NOT_CONFIGURED: Final = "not_configured"

#: A dependency we need is not answering. Worth retrying; the others are not.
UPSTREAM_UNAVAILABLE: Final = "upstream_unavailable"

# --- messages between members (the mesh messaging plan) ----------------------

#: The organization has switched messaging off.
MESSAGING_OFF: Final = "messaging_off"

#: A handle or user id that is not on the team roster.
MEMBER_UNKNOWN: Final = "member_unknown"

#: A message arrived at a device whose person it does not address.
NOT_A_RECIPIENT: Final = "not_a_recipient"

#: More named recipients than one message may have.
TOO_MANY_RECIPIENTS: Final = "too_many_recipients"

#: A message body over the size limit.
BODY_TOO_LARGE: Final = "body_too_large"

#: A message body holding control characters or escape sequences.
BODY_INVALID: Final = "body_invalid"

#: More plan references than one message may carry.
TOO_MANY_REFS: Final = "too_many_refs"

#: Too many messages already waiting to be delivered from this device.
OUTBOX_FULL: Final = "outbox_full"

#: Sending with a team roster that is in its grace period or older.
ROSTER_STALE: Final = "roster_stale"

#: A message older than the organization's retention period.
MESSAGE_EXPIRED: Final = "message_expired"

#: A short id that matches more than one thread.
AMBIGUOUS_ID: Final = "ambiguous_id"

#: The receiving device runs a flanner that predates the operation, so it
#: answered "unknown operation". Retrying cannot help until it upgrades.
PEER_OUTDATED: Final = "peer_outdated"

#: The recipient is on the team but has no enrolled device, so nothing
#: can take the message. Retrying cannot help until they enrol one.
NO_DEVICES: Final = "no_devices"

# --- Flanner Curb team features (docs/curb-wire-contract.md) -----------------

#: The client is older than this endpoint's minimum. Sent with HTTP 426 and
#: a `minimum_version` field; retrying cannot help until flanner is updated.
CLIENT_TOO_OLD: Final = "client_too_old"

#: A fleet report whose sequence number is not higher than the last one
#: accepted from that device: a replay, or a report sent out of order.
STALE_SEQUENCE: Final = "stale_sequence"

#: An attribution key already registered to another device. Ownership of a
#: key never moves, so no retry can succeed.
KEY_OWNED: Final = "key_owned"

#: An attribution key's proof of possession did not verify.
BAD_PROOF: Final = "bad_proof"

#: A replacement that names no active attribution key of the same device.
REPLACES_UNKNOWN: Final = "replaces_unknown"

#: Never sent. What a client uses for a code it does not recognise, so that
#: "an old client met a new server" is a case with a name rather than a
#: KeyError.
UNKNOWN: Final = "unknown"


#: Every code this version can send. A client may use it to notice that it is
#: older than the server it is talking to; it must not use it to reject a
#: code that is missing from it.
KNOWN: Final = frozenset(
    {
        BAD_SIGNATURE,
        CLOCK_SKEW,
        REPLAYED,
        DEVICE_UNKNOWN,
        MEMBERSHIP_INACTIVE,
        NOT_ADMIN,
        SUBSCRIPTION_INACTIVE,
        NO_GRANT,
        CODE_UNUSABLE,
        MALFORMED,
        THROTTLED,
        NOT_FOUND,
        NOT_CONFIGURED,
        UPSTREAM_UNAVAILABLE,
        MESSAGING_OFF,
        MEMBER_UNKNOWN,
        NOT_A_RECIPIENT,
        TOO_MANY_RECIPIENTS,
        BODY_TOO_LARGE,
        BODY_INVALID,
        TOO_MANY_REFS,
        OUTBOX_FULL,
        ROSTER_STALE,
        MESSAGE_EXPIRED,
        AMBIGUOUS_ID,
        PEER_OUTDATED,
        NO_DEVICES,
        CLIENT_TOO_OLD,
        STALE_SEQUENCE,
        KEY_OWNED,
        BAD_PROOF,
        REPLACES_UNKNOWN,
    }
)

#: Codes where trying the same request again could succeed without the caller
#: changing anything. Everything else needs a human or a different request.
#:
#: `CLOCK_SKEW` is in here because a clock can be corrected between attempts
#: and often is, by the operating system, without anybody noticing.
RETRYABLE: Final = frozenset({THROTTLED, UPSTREAM_UNAVAILABLE, CLOCK_SKEW})


def is_retryable(code: str) -> bool:
    """Whether trying again could work without the caller changing anything.

    An unrecognised code is treated as *not* retryable. A client that retried
    everything it did not understand would hammer a control plane that had
    just learned to refuse something new, which is the worst moment for it.
    """
    return code in RETRYABLE
