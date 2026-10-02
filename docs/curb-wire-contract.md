# Flanner Curb wire contract (R5)

What a flanner device and the control plane exchange for Curb's team
features: org policy, the fleet view and alerts (Curb PRD §10.8–10.11,
§12.2). The client's half is `flanner/curb_wire.py`.

**Rules for both sides:**

- The control plane imports no client code for Curb. It holds every name
  below as a string literal, as `refusal.py` already does.
- Both suites check the test vectors at the end of this page. The client's
  `tests/test_curb_wire.py` recomputes them from `curb_wire`, so this page
  cannot drift from the code.
- A name here is permanent. It may be deprecated, never reused.
- Unknown fields are ignored, never refused, so either side can add one.

## Features and capabilities

A device uses a Curb team feature only when **both** hold:

1. its entitlement lists the feature, and
2. the entitlement response lists the matching capability in
   `curb_capabilities`.

| Entitlement feature | Capability | What it turns on |
|---|---|---|
| `curb_policy` | `curb-policy/1` | Policy check-in, the authority list, policy state, audit export |
| `curb_fleet` | `curb-fleet/1` | Fleet reports, and `flanner curb fleet` for admins |
| `curb_alerts` | `curb-alerts/1` | Alerts relayed to the organization's webhook or Slack |

A control plane without Curb sends no `curb_capabilities`, and the device
then uses none of these, whatever its entitlement says. Everything else in
Curb works with no account at all.

The free Curb plan carries all three features (cloud `issuer.py`), and so
does `managed_mesh`.

## Headers

Every Curb request carries:

| Header | Value |
|---|---|
| `Flanner-Client-Version` | The client's version, such as `0.16.0` |
| `Flanner-Curb-Capabilities` | The capabilities it speaks, comma-separated: `curb-policy/1, curb-fleet/1, curb-alerts/1` |

A client below a Curb endpoint's minimum version gets **HTTP 426** with:

```json
{"code": "client_too_old", "detail": "...", "minimum_version": "0.16.0"}
```

The client shows it as "update flanner to 0.16.0 or later". Endpoints that
existed before Curb keep their behaviour for older clients.

## Refusal codes

New with Curb:

| Code | HTTP | Means |
|---|---|---|
| `client_too_old` | 426 | The client is below this endpoint's minimum; `minimum_version` says which |
| `stale_sequence` | 409 | A fleet report whose sequence number is not higher than the last one accepted from that device. The device drops it: it can never be accepted |

Curb endpoints also use the existing codes: `bad_signature`, `clock_skew`,
`replayed`, `device_unknown` (including a removed device), `not_admin`
(fleet reads), `subscription_inactive` (the plan lacks the feature),
`malformed` and `throttled`.

## Requests

Every Curb endpoint is a `POST` whose body is a device-signed envelope, as
for every other device call (`device_auth.sign_request`): the device id,
the issue time, a fresh nonce and the body below, signed with the device
key. Each call is a read, or a write the control plane deduplicates, so
the client retries a connection failure up to three times.

| Endpoint | Body | Answer |
|---|---|---|
| `/v1/curb/authority` | `{}` | `{"authority": "<signed curb_authority>"}` |
| `/v1/curb/policy` | `{"current_version": 7, "current_hash": "sha256:..."}`; `0` and `""` before the first | `{"status": "unchanged"}`, `{"status": "none"}` (no policy), or `{"status": "policy", "policy": "<signed curb_policy>", "audit_export_token": "..."}` |
| `/v1/curb/policy/state` | The policy state, below | `{}` |
| `/v1/curb/reports` | `{"report": "<signed curb_report>"}` | `{}`, or `stale_sequence` |
| `/v1/curb/alerts` | `{"alerts": [<alert>, ...]}` | `{"accepted": ["evt_...", ...]}`: every id now held for delivery, including ids already delivered |
| `/v1/curb/fleet` | `{}`; admins only | `{"devices": [{"device_id", "label", "reports": ["<signed curb_report>", ...]}]}`: each device's last 30 days of reports, oldest first |

`/v1/curb/policy` answers `unchanged` when `current_version` and
`current_hash` match the organization's newest policy. It may send the
current policy anyway; a device treats a policy with the same version and
hash as a normal check-in.

`audit_export_token` is the bearer token for the organization's audit
collector. It travels outside the signed policy on purpose: signed policies
are kept while the organization exists, and a token must not be. The
device keeps it in the OS keychain.

`/v1/curb/fleet` is not in PRD §12.2. It is how an admin's
`flanner curb fleet` reads reports to verify them itself.

## Signed documents

A signed document is a JSON object, sent as

```
base64url(canonical bytes, no padding) "." base64(Ed25519 signature over those bytes)
```

where the canonical bytes are the object as JSON with sorted keys, no
whitespace, UTF-8, `ensure_ascii` off and no NaN (`artifacts.canonical_bytes`).
This is the form signed rosters already use. `key_id` names the signing key.

A document's **hash** is `sha256:` and the hex SHA-256 of its canonical
bytes. Hashes cover the decoded fields, not the token.

### `curb_authority`: the policy authority list

Signed by the **entitlement issuer key**, which devices already trust from
the `keyring` of every entitlement response.

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"curb_authority"` | |
| `key_id` | string | The issuer key that signed it |
| `version` | integer | Only ever rises |
| `issued_at`, `expires_at` | RFC 3339 UTC | |
| `keys` | list | Each: `key_id`, `public_key` (base64 raw Ed25519), `not_before`, `not_after`, `status` |

`status` is `active`, `retiring` or `revoked`. A device caches the highest
version that verifies and refuses an older one, or the same version with
different contents. Policy keys rotate like this: the new key is `active`;
the old one becomes `retiring` and still verifies until its `not_after`.
An expired list stops new policies being accepted; it never weakens the
current one.

### `curb_policy`: an org policy

Signed by a policy key from the authority list.

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"curb_policy"` | |
| `key_id` | string | The policy key that signed it |
| `organization_id` | string | Must match the device's organization |
| `version` | integer | Only ever rises. Re-signing always issues a new version |
| `issued_at`, `expires_at` | RFC 3339 UTC | |
| `previous_hash` | string | The hash of the version before; `""` for the first |
| `rules` | object | Below |
| `audit_export` | object, optional | `{"url": "https://...", "format": "ocsf", "openshell": true}`; `openshell` is optional |

A policy is valid when it verifies against a key that is `active` or
`retiring` and inside its window, names the device's organization, and has
not expired. Check-in outcomes (PRD §10.8):

| Received policy | Device |
|---|---|
| Higher version, valid | Applies it. When it is exactly one version higher, its `previous_hash` must equal the current policy's hash, or it is an integrity error |
| Same version, same hash | Nothing |
| Same version, different hash | Integrity error: keeps the current one, alerts |
| Lower version | Rollback: refuses it, keeps the current one, alerts |
| Bad signature, another organization, an unknown or revoked key, expired | Refuses it, keeps the current one, alerts |

A policy already in force whose key is later revoked stays in force,
flagged `revoked_key`, until a newer version signed by a current key
arrives. An expired policy stays in force, flagged `expired`.

#### Rules, schema 1

| Rule | Value | Means |
|---|---|---|
| `deny_read` | list of paths | No agent may read these by any channel. Each starts with `~/` or is absolute, has no `..` and no wildcards, and covers everything under it |
| `sandbox` | `"required"` | The agent's own sandbox, with no way around it |
| `network` | `{"allowed_domains": [...]}` | The only domains sandboxed commands may reach; `[]` means none. Needs the sandbox, so it turns the sandbox on |
| `web` | `"off"` | No web fetch and no web search |
| `mcp` | `{"allowed": [{"name", "command": [...]}` or `{"name", "url"}]}` | The only MCP servers that may load; `[]` means none |

A rule a client does not know is never ignored: the device reports it as
unmet, "update flanner", and applies the rules it knows.

### `curb_report`: a fleet report

Signed by the **device key**. `key_id` is the device id, so a verifier
looks the key up in the organization's device keyring.

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"curb_report"` | |
| `key_id`, `device_id` | string | The device |
| `organization_id` | string | |
| `client_version` | string | |
| `sequence` | integer | Per device, starting at 1, always rising |
| `created_at` | RFC 3339 UTC | |
| `previous_hash` | string | The hash of the device's previous report; `""` for the first |
| `agents` | list | `{"agent": "claude" or "codex", "version"}` |
| `policy` | object | The policy state, below |
| `severity` | object | `{"high", "medium", "low"}`: counts of launches |
| `exposure` | object or null | `{"A", "B", "C"}`: secrets by exposure class from the last leak sweep; null before one |
| `checked_at` | RFC 3339 UTC | When settings were last read |

No paths, usernames, hostnames, repository names or fingerprints. The
control plane refuses a report whose `sequence` is not higher than the last
it accepted from that device (`stale_sequence`), and one from a removed
device (`device_unknown`). A missing sequence number, or a newest report
over 24 hours old, shows the device as stale.

## Policy state

Sent to `/v1/curb/policy/state`, and inside each fleet report as `policy`:

| Field | Type | Meaning |
|---|---|---|
| `received` | integer or null | The newest valid policy version received |
| `hash` | string or null | Its hash |
| `applied` | integer or null | The version written and read back |
| `approved_by` | string or null | `delegation`, `person`, or `already in place` |
| `pending` | integer or null | A version waiting for the person's approval |
| `rejected` | integer or null | The last refused version |
| `rejected_outcome` | string or null | `integrity`, `rollback` or `rejected` |
| `delegation` | boolean | Whether the person's delegation is on |
| `flags` | list | Any of `expired`, `revoked_key`, `unverified`, `authority_expired` |
| `compliance_hash` | string or null | The same on every device whose effective settings meet the same policy |
| `drift` | boolean | Some rule is not met by the effective settings |

`compliance_hash` is the hash of `{"policy": <policy hash>, "unmet":
{<agent>: [<unmet rule>, ...]}}`, listing only agents with an unmet rule.

## Alerts

Each alert the device sends to `/v1/curb/alerts`:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | string | Stable: `evt_` and the first 32 hex characters of the SHA-256 of the canonical bytes of `{"device_id", "finding", "sequence"}` |
| `type` | string | `mcp_server_added`, `deny_rule_removed`, `sandbox_off`, `secret_class_a`, `policy_refused`, `policy_integrity`, `policy_rollback`, `authority_refused` |
| `agent` | string | `claude`, `codex`, or `""` |
| `severity` | string | `high` or `medium` |
| `digest` | string | The per-device keyed digest of what changed, or `""` |
| `location` | string | A category, such as "Claude Code MCP settings"; never a path |
| `created_at` | number | Unix seconds |

`finding` is `type:agent:digest:location`, and `sequence` is the device's
change counter for the pass that found it. Retries resend the same ids.
The relay adds the device label and delivers each id at least once; it
drops an id it delivered in the last 7 days. Each webhook delivery carries:

| Header | Value |
|---|---|
| `Flanner-Event-Id` | The event id, so receivers can drop repeats |
| `Flanner-Signature` | `t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`. For 24 hours after a secret rotation, two `v1=` values, new first |

## Audit export

Not the control plane: the device sends records straight to the collector
named in the policy's `audit_export.url`, over https, with
`Authorization: Bearer <audit_export_token>`. The body is a JSON list of
OCSF 1.9.0 records. Each tool call in Curb's action log is one API Activity
event (class 6003, with the AI Operation profile): the agent, its local
session id, the tool, the decision, the target's kind and keyed digest, and
the time. With `"openshell": true`, OpenShell's own OCSF lines are passed
through, with command lines, paths, queries and bodies replaced by keyed
digests.

## Test vectors

Every key is derived from a 32-byte seed (`Ed25519PrivateKey.from_private_bytes`).
Ed25519 signatures are deterministic, so each token is exact. Never use
these keys outside tests.

```json
{
  "keys": {
    "issuer": {
      "seed": "0101010101010101010101010101010101010101010101010101010101010101",
      "key_id": "iss_test",
      "public_key": "iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w="
    },
    "policy_current": {
      "seed": "0202020202020202020202020202020202020202020202020202020202020202",
      "key_id": "pol_2026",
      "public_key": "gTl3Dqh9F19Wo1Rmw0x+zMuNipG07jeiXfYPW4/Js5Q="
    },
    "policy_revoked": {
      "seed": "0303030303030303030303030303030303030303030303030303030303030303",
      "key_id": "pol_2025",
      "public_key": "7UkoxijRwsbq6QM4kFmVYSlZJzpcY/k2NsFGFKyHN9E="
    },
    "device": {
      "seed": "0404040404040404040404040404040404040404040404040404040404040404",
      "device_id": "dev_c5b940ed3f65c391",
      "public_key": "ypOsFwUYcHHWe4PH/w7+gQjo7EUwV113JoeTM9vavnw="
    }
  },
  "authority": {
    "fields": {
      "kind": "curb_authority",
      "key_id": "iss_test",
      "version": 2,
      "issued_at": "2026-10-01T00:00:00Z",
      "expires_at": "2027-10-01T00:00:00Z",
      "keys": [
        {
          "key_id": "pol_2026",
          "public_key": "gTl3Dqh9F19Wo1Rmw0x+zMuNipG07jeiXfYPW4/Js5Q=",
          "not_before": "2026-01-01T00:00:00Z",
          "not_after": "2027-01-31T00:00:00Z",
          "status": "active"
        },
        {
          "key_id": "pol_2025",
          "public_key": "7UkoxijRwsbq6QM4kFmVYSlZJzpcY/k2NsFGFKyHN9E=",
          "not_before": "2025-01-01T00:00:00Z",
          "not_after": "2026-01-31T00:00:00Z",
          "status": "revoked"
        }
      ]
    },
    "canonical": "{\"expires_at\":\"2027-10-01T00:00:00Z\",\"issued_at\":\"2026-10-01T00:00:00Z\",\"key_id\":\"iss_test\",\"keys\":[{\"key_id\":\"pol_2026\",\"not_after\":\"2027-01-31T00:00:00Z\",\"not_before\":\"2026-01-01T00:00:00Z\",\"public_key\":\"gTl3Dqh9F19Wo1Rmw0x+zMuNipG07jeiXfYPW4/Js5Q=\",\"status\":\"active\"},{\"key_id\":\"pol_2025\",\"not_after\":\"2026-01-31T00:00:00Z\",\"not_before\":\"2025-01-01T00:00:00Z\",\"public_key\":\"7UkoxijRwsbq6QM4kFmVYSlZJzpcY/k2NsFGFKyHN9E=\",\"status\":\"revoked\"}],\"kind\":\"curb_authority\",\"version\":2}",
    "hash": "sha256:f0d3dcb6b58c8da6e97251e887a081cf7dafa42b33105c277aa88d2521a19d93",
    "token": "eyJleHBpcmVzX2F0IjoiMjAyNy0xMC0wMVQwMDowMDowMFoiLCJpc3N1ZWRfYXQiOiIyMDI2LTEwLTAxVDAwOjAwOjAwWiIsImtleV9pZCI6Imlzc190ZXN0Iiwia2V5cyI6W3sia2V5X2lkIjoicG9sXzIwMjYiLCJub3RfYWZ0ZXIiOiIyMDI3LTAxLTMxVDAwOjAwOjAwWiIsIm5vdF9iZWZvcmUiOiIyMDI2LTAxLTAxVDAwOjAwOjAwWiIsInB1YmxpY19rZXkiOiJnVGwzRHFoOUYxOVdvMVJtdzB4K3pNdU5pcEcwN2plaVhmWVBXNC9KczVRPSIsInN0YXR1cyI6ImFjdGl2ZSJ9LHsia2V5X2lkIjoicG9sXzIwMjUiLCJub3RfYWZ0ZXIiOiIyMDI2LTAxLTMxVDAwOjAwOjAwWiIsIm5vdF9iZWZvcmUiOiIyMDI1LTAxLTAxVDAwOjAwOjAwWiIsInB1YmxpY19rZXkiOiI3VWtveGlqUndzYnE2UU00a0ZtVllTbFpKenBjWS9rMk5zRkdGS3lITjlFPSIsInN0YXR1cyI6InJldm9rZWQifV0sImtpbmQiOiJjdXJiX2F1dGhvcml0eSIsInZlcnNpb24iOjJ9.jrZP+1Oft7AAyxmencD9HHYzkD66ov0JMerYSwZ/2eXzQEHABZwfxAGrywik3LhvNyG5vNuGHN5RJdRU6B+cAQ=="
  },
  "policy": {
    "fields": {
      "kind": "curb_policy",
      "key_id": "pol_2026",
      "organization_id": "org_test",
      "version": 7,
      "issued_at": "2026-10-01T00:00:00Z",
      "expires_at": "2026-11-01T00:00:00Z",
      "previous_hash": "sha256:6666666666666666666666666666666666666666666666666666666666666666",
      "rules": {
        "deny_read": [
          "~/.aws",
          "~/.ssh"
        ],
        "sandbox": "required",
        "network": {
          "allowed_domains": [
            "github.com",
            "pypi.org"
          ]
        },
        "web": "off",
        "mcp": {
          "allowed": [
            {
              "name": "docs",
              "command": [
                "docs-mcp",
                "--stdio"
              ]
            }
          ]
        }
      },
      "audit_export": {
        "url": "https://collector.example.com/ocsf",
        "format": "ocsf"
      }
    },
    "canonical": "{\"audit_export\":{\"format\":\"ocsf\",\"url\":\"https://collector.example.com/ocsf\"},\"expires_at\":\"2026-11-01T00:00:00Z\",\"issued_at\":\"2026-10-01T00:00:00Z\",\"key_id\":\"pol_2026\",\"kind\":\"curb_policy\",\"organization_id\":\"org_test\",\"previous_hash\":\"sha256:6666666666666666666666666666666666666666666666666666666666666666\",\"rules\":{\"deny_read\":[\"~/.aws\",\"~/.ssh\"],\"mcp\":{\"allowed\":[{\"command\":[\"docs-mcp\",\"--stdio\"],\"name\":\"docs\"}]},\"network\":{\"allowed_domains\":[\"github.com\",\"pypi.org\"]},\"sandbox\":\"required\",\"web\":\"off\"},\"version\":7}",
    "hash": "sha256:aba3a27321f041614612c56b493555967664c83dc192f283fb0737c139b96a07",
    "token": "eyJhdWRpdF9leHBvcnQiOnsiZm9ybWF0Ijoib2NzZiIsInVybCI6Imh0dHBzOi8vY29sbGVjdG9yLmV4YW1wbGUuY29tL29jc2YifSwiZXhwaXJlc19hdCI6IjIwMjYtMTEtMDFUMDA6MDA6MDBaIiwiaXNzdWVkX2F0IjoiMjAyNi0xMC0wMVQwMDowMDowMFoiLCJrZXlfaWQiOiJwb2xfMjAyNiIsImtpbmQiOiJjdXJiX3BvbGljeSIsIm9yZ2FuaXphdGlvbl9pZCI6Im9yZ190ZXN0IiwicHJldmlvdXNfaGFzaCI6InNoYTI1Njo2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2IiwicnVsZXMiOnsiZGVueV9yZWFkIjpbIn4vLmF3cyIsIn4vLnNzaCJdLCJtY3AiOnsiYWxsb3dlZCI6W3siY29tbWFuZCI6WyJkb2NzLW1jcCIsIi0tc3RkaW8iXSwibmFtZSI6ImRvY3MifV19LCJuZXR3b3JrIjp7ImFsbG93ZWRfZG9tYWlucyI6WyJnaXRodWIuY29tIiwicHlwaS5vcmciXX0sInNhbmRib3giOiJyZXF1aXJlZCIsIndlYiI6Im9mZiJ9LCJ2ZXJzaW9uIjo3fQ.VNDwVlonGEYe1mk8ly+4Yl30nZCSyDIEDGxMcOkE681T8UPmGTh+gJry+/j84i5VfOKyWNZpmhqvTkgRSP5vDg=="
  },
  "report": {
    "fields": {
      "kind": "curb_report",
      "key_id": "dev_c5b940ed3f65c391",
      "device_id": "dev_c5b940ed3f65c391",
      "organization_id": "org_test",
      "client_version": "0.16.0",
      "sequence": 3,
      "created_at": "2026-10-02T09:00:00Z",
      "previous_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
      "agents": [
        {
          "agent": "claude",
          "version": "2.1.287"
        }
      ],
      "policy": {
        "received": 7,
        "hash": "sha256:7777777777777777777777777777777777777777777777777777777777777777",
        "applied": 7,
        "approved_by": "delegation",
        "pending": null,
        "rejected": null,
        "rejected_outcome": null,
        "delegation": true,
        "flags": [],
        "compliance_hash": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "drift": false
      },
      "severity": {
        "high": 1,
        "medium": 0,
        "low": 0
      },
      "exposure": {
        "A": 0,
        "B": 2,
        "C": 1
      },
      "checked_at": "2026-10-02T09:00:00Z"
    },
    "canonical": "{\"agents\":[{\"agent\":\"claude\",\"version\":\"2.1.287\"}],\"checked_at\":\"2026-10-02T09:00:00Z\",\"client_version\":\"0.16.0\",\"created_at\":\"2026-10-02T09:00:00Z\",\"device_id\":\"dev_c5b940ed3f65c391\",\"exposure\":{\"A\":0,\"B\":2,\"C\":1},\"key_id\":\"dev_c5b940ed3f65c391\",\"kind\":\"curb_report\",\"organization_id\":\"org_test\",\"policy\":{\"applied\":7,\"approved_by\":\"delegation\",\"compliance_hash\":\"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\",\"delegation\":true,\"drift\":false,\"flags\":[],\"hash\":\"sha256:7777777777777777777777777777777777777777777777777777777777777777\",\"pending\":null,\"received\":7,\"rejected\":null,\"rejected_outcome\":null},\"previous_hash\":\"sha256:2222222222222222222222222222222222222222222222222222222222222222\",\"sequence\":3,\"severity\":{\"high\":1,\"low\":0,\"medium\":0}}",
    "hash": "sha256:2467793fe125b3ebc08b36f920bfeb8ef99e0a88185313c1cc701de6438d6522",
    "token": "eyJhZ2VudHMiOlt7ImFnZW50IjoiY2xhdWRlIiwidmVyc2lvbiI6IjIuMS4yODcifV0sImNoZWNrZWRfYXQiOiIyMDI2LTEwLTAyVDA5OjAwOjAwWiIsImNsaWVudF92ZXJzaW9uIjoiMC4xNi4wIiwiY3JlYXRlZF9hdCI6IjIwMjYtMTAtMDJUMDk6MDA6MDBaIiwiZGV2aWNlX2lkIjoiZGV2X2M1Yjk0MGVkM2Y2NWMzOTEiLCJleHBvc3VyZSI6eyJBIjowLCJCIjoyLCJDIjoxfSwia2V5X2lkIjoiZGV2X2M1Yjk0MGVkM2Y2NWMzOTEiLCJraW5kIjoiY3VyYl9yZXBvcnQiLCJvcmdhbml6YXRpb25faWQiOiJvcmdfdGVzdCIsInBvbGljeSI6eyJhcHBsaWVkIjo3LCJhcHByb3ZlZF9ieSI6ImRlbGVnYXRpb24iLCJjb21wbGlhbmNlX2hhc2giOiJzaGEyNTY6Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjYyIsImRlbGVnYXRpb24iOnRydWUsImRyaWZ0IjpmYWxzZSwiZmxhZ3MiOltdLCJoYXNoIjoic2hhMjU2Ojc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3NzciLCJwZW5kaW5nIjpudWxsLCJyZWNlaXZlZCI6NywicmVqZWN0ZWQiOm51bGwsInJlamVjdGVkX291dGNvbWUiOm51bGx9LCJwcmV2aW91c19oYXNoIjoic2hhMjU2OjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIiLCJzZXF1ZW5jZSI6Mywic2V2ZXJpdHkiOnsiaGlnaCI6MSwibG93IjowLCJtZWRpdW0iOjB9fQ.YKblgrFL1KbOeKoYp8lMbT4RbYdhXAV7QsgR8SztfKNa1rVDhVRr5GdScJ5kNXiO0LWcvw4ZRUCpQQeNc39bCw=="
  },
  "event_id": {
    "device_id": "dev_c5b940ed3f65c391",
    "finding": "mcp_server_added:claude:dddddddddddddddddddddddddddddddd:Claude Code MCP settings",
    "sequence": 4,
    "id": "evt_9d43fd015e75089ba2c5820ab6dde949"
  },
  "headers": {
    "Flanner-Client-Version": "0.16.0",
    "Flanner-Curb-Capabilities": "curb-policy/1, curb-fleet/1, curb-alerts/1"
  }
}
```
