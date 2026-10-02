# Flanner Curb: work other repos need, by release

Every Curb release lands in more than one repo (Curb PRD §12.2, §12.3). The
flanner client work is on `feat/curb-r1`. This page lists what the other
repos still need for each release, so nothing is left behind.

**Status: none of this is started.** Each section is filled in with the
specifics once that release's client work is done.

Rules that apply to every release (PRD §12.3):

- Use the same branch name, `feat/curb-r1`, in each repo, so meshlab can
  pair branches.
- Landing pages merge early behind their `SHIPS_IN` key and appear when
  `PYPI_VERSION` flips on release day.
- No public page uses the name "Curb" until the trademark search clears.
- Legal pages change only after the owner approves them.
- A release is done only when every repo's list below is done.

## R1: inventory and reach now

Client work is done. Commands: `flanner curb map`, `flanner curb show`,
`flanner curb inventory`.

**flanner-landing** (all gated on `SHIPS_IN.curbMap`):

- [ ] `lib/site.ts`: `SHIPS_IN.curbMap` and a `CURB_MAP_RELEASED` flag.
- [ ] Product page `app/curb/page.tsx`, with metadata from `lib/seo.ts`, and
      entries in `app/sitemap.ts`, the `app/llms.txt` route and the home page.
- [ ] Docs page `app/docs/curb/page.tsx`, added to `lib/docs-nav.ts`. Cover:
  - the channels (file tools, shell files, shell network, web, MCP, Codex
    apps) and why each is judged on its own;
  - `severity-r1` v1, rules H1, H2, M1, M2 and L1;
  - evidence levels (configured, assumed; enforced arrives in R3);
  - launch contexts: `--dir`, `--agent`, `--profile`, and a launch command
    after `--`; scheduled jobs; the stated assumption;
  - the tested versions and what is not checked, from
    `docs/curb-support-matrix.md`;
  - Codex apps count as unknown until `features.apps = false`.
- [ ] CLI rows in `app/docs/cli/page.tsx` for `curb map` (`--dir`, `--agent`,
      `--profile`, `--json`, `-- <launch command>`), `curb show` and
      `curb inventory` (`--json`, plus the same launch options).
- [ ] A note in `app/docs/agents/page.tsx` that Curb has no MCP tools, and why.
- [ ] `lib/operations.json`, regenerated from `feat/curb-r1` with
      `python -m flanner.operations`. It adds the `curb` domain, which needs a
      label in the agent tables, with two CLI-only read operations: "List the
      agents here and what each one loads" and "See what each agent launch can
      reach".
- [ ] FAQ entries in `lib/faq.ts`: why most machines rate High; why a Read
      deny rule alone does not protect a file; what "assumed" means; why there
      are no MCP tools; that nothing leaves the machine.
- [ ] Troubleshooting in `app/docs/troubleshooting/page.tsx`: `curb show`
      saying "there is no desktop session here" (no `DISPLAY` or
      `WAYLAND_DISPLAY`) or "Python's Tk support is missing" (install
      `python3-tk` on Linux).
- [ ] A section on `app/security/page.tsx`: output is redacted for every
      caller, detail only in a desktop window, all processing local.
- [ ] Release day: `PYPI_VERSION` set to the version that ships R1.

**flanner-cloud:** none. **flanner-meshlab:** none. **flanner-brand:** none.

## R2: leak sweep

Client work is done. Commands: `flanner curb sweep`, `flanner curb show
--sweep`, `flanner curb forget`. Detection needs the `flanner[sweep]` extra.

**flanner-landing** (gated on `SHIPS_IN.curbSweep`):

- [ ] `SHIPS_IN.curbSweep`.
- [ ] Docs for the sweep: where it reads (transcripts, Codex sessions and
      prompt history, CLAUDE.md and AGENTS.md, skills, MCP configs and agent
      settings, shell history, `.env` files, flanner plans and memories); the
      classes A (sent to a model provider), B (readable by an agent) and C
      (on disk but blocked); install with `pip install 'flanner[sweep]'`.
- [ ] CLI rows: `curb sweep` (`--dir`, `--json`, `--validate`),
      `curb show --sweep [--validate]`, `curb forget [--yes]`.
- [ ] `lib/operations.json` regenerated: two more `curb` operations, "Find
      secrets agents left behind" (read) and "Delete what Curb keeps on this
      machine" (destructive).
- [ ] FAQ entries: why a transcript secret means rotate now; that no command
      prints a value; what `--validate` sends, and to whom; what `forget`
      deletes.
- [ ] Troubleshooting: "the leak sweep needs Kingfisher"; files over 64 MB
      under Not checked.
- [ ] Privacy policy: an opted-in check sends a found key straight to its own
      issuer. Needs the owner's approval.
- [ ] The date in `lib/legal.ts`.

**flanner-cloud:** none. **flanner-meshlab:** none.

## R3: fixes, tester, scrubbing and approvals

Client work is done. Commands: `flanner curb fix [--dry-run|--undo]`,
`flanner curb test`, `flanner curb decoys [--renew|--remove]`, `flanner curb
scrub FILE [--dry-run]`, `flanner curb forget --backups`. Web: the Agent
reach page at `/curb`.

**flanner-landing** (gated on `SHIPS_IN.curbFix`):

- [ ] Docs for fixes (what each kind writes, backups for 7 days, `--undo`),
      the tighten-only rule, tester outcomes (blocked, allowed, inconclusive,
      not tested, unsupported, "passed in scratch context"), decoys and
      scrubbing (no backup, rotate first).
- [ ] Approvals: Windows Hello or the account password, Touch ID or the
      password, polkit on a Linux desktop; grants single use for two minutes;
      the pause after three refusals; no method means read-only.
- [ ] CLI rows for the five commands above.
- [ ] `lib/operations.json` regenerated: "Fix what each agent can reach"
      (write, also `POST /curb/fix`), "Prove a block by asking the agent to
      get past it", "List, renew or remove the tester's decoys", "Scrub
      rotated secrets out of a file" (destructive); `GET /curb` and
      `POST /curb/show` added to "See what each agent launch can reach".
- [ ] `app/docs/web/page.tsx`: the Agent reach page.
- [ ] Troubleshooting: "No approval method on this machine", "Not approved",
      the pause, "inconclusive" results, a Codex config edited by hand.
- [ ] Security page: approvals and grants, backups denied to agents.
- [ ] `app/disclaimer/page.tsx`: what Curb's tests do and don't prove. Needs
      the owner's approval.

**flanner-cloud:** none. **flanner-meshlab:** none.

## R4: action log and observed use

Client work is done. Commands: `flanner curb log [--enable|--disable|--verify]`,
`flanner curb observed [--json]`; the hidden hook `flanner hook curb-record`.

**flanner-landing** (gated on `SHIPS_IN.curbObserve`):

- [ ] Docs: what a record holds (metadata, never content), the hash chain
      and device signature, 30-day retention, the three evidence states, the
      14-day and 20-session window, what the hooks cannot see.
- [ ] CLI rows for `curb log` and `curb observed`.
- [ ] `lib/operations.json` regenerated: "Turn Curb's action log on or off,
      or check it" (write) and "See what each agent has been seen using"
      (read).
- [ ] FAQ: why "not seen" is not "not needed"; does the log hold my prompts
      (no); what happens if a hook fails (the agent carries on).

**flanner-cloud:** none. **flanner-meshlab:** none.

## R5: team features on Flanner Mesh

Client work is done. Commands: `flanner curb policy [--check-in|--enrol|--withdraw|
--approve|--export DIR|--json]`, `flanner curb fleet [--json]`; the hidden hook
`flanner hook curb-session`. The wire format, with test vectors, is
`docs/curb-wire-contract.md`: build the control plane against that page, not
against client code.

**flanner-cloud** (PRD §12.2 R5 has the full contract):

- [ ] Wire contract: hold every name in `docs/curb-wire-contract.md` as a
      string literal, and check its test vectors in the cloud suite.
- [ ] Plans: a free Curb plan in `issuer.py` (`curb_policy`, `curb_fleet`,
      `curb_alerts`); `managed_mesh` gains them. Lapsed trials drop to the free
      plan (`billing.py`); `sign_up.html` says what stays free.
- [ ] Entitlement responses list `curb_capabilities`: `curb-policy/1`,
      `curb-fleet/1`, `curb-alerts/1`. The client uses nothing without them.
- [ ] `relay.py` refuses relay to free-plan orgs; sync stays off for them.
- [ ] Models and Alembic migrations: policy versions, device policy states,
      fleet reports, alert destinations, alert outbox, delivered event ids,
      policy authority list.
- [ ] Policy authority: separate signing key behind `signing.py`; the signed
      `curb_authority` list at `POST /v1/curb/authority`, signed by the
      entitlement issuer key; rotation (active, retiring, revoked) and
      revocation rules; yearly rotation in the ops console.
- [ ] Policies: each `curb_policy` carries `previous_hash`; re-signing always
      issues a new version; `POST /v1/curb/policy` answers `unchanged` for the
      current version and hash, `none` with no policy, and sends
      `audit_export_token` outside the signed policy.
- [ ] Version negotiation: the `Flanner-Client-Version` and
      `Flanner-Curb-Capabilities` headers, `client_too_old` (HTTP 426) with
      `minimum_version`, mixed-version tests.
- [ ] API in `api.py`, limits in `ratelimit.py`: `/v1/curb/authority`,
      `/v1/curb/policy`, `/v1/curb/policy/state`, `/v1/curb/reports`
      (`stale_sequence`, HTTP 409, for a sequence not higher than the last),
      `/v1/curb/alerts` (answers `accepted` with every id it holds).
- [ ] **Not in PRD §12.2, needed by the client:** `POST /v1/curb/fleet` for
      admins (`not_admin` otherwise), returning each device's label and its
      last 30 days of signed reports, oldest first. `flanner curb fleet`
      verifies them itself with the device keyring.
- [ ] Alert relay: adds the device label, delivers each event id at least
      once, drops ids delivered in the last 7 days, and sends the
      `Flanner-Event-Id` and `Flanner-Signature` headers defined in the
      contract.
- [ ] Console: policy editor with preview ("device evaluation required"
      where settings outside the policy decide), fleet page, alert
      destinations with a test send, audit entries.
- [ ] Customer secrets encrypted at rest (envelope encryption with Vault
      transit), shown once, rotated yearly; `docs/secrets.md`. Audit collector
      tokens join webhook and Slack secrets here.
- [ ] Jobs: alert delivery with retries, pruning, report deletion on device
      removal.
- [ ] Operations: metrics, `docs/alerts.md`, `docs/failure-model.md`,
      benchmarks.
- [ ] `scripts/seed_curb_policy.py`: publish the dev team's next policy
      version from a rules file, for the client's
      `scripts/mesh_dev_scenarios.py` cases `curb-policy`, `curb-fleet` and
      `curb-alerts`.
- [ ] Tests and seeds; lock update after the client release; console privacy
      page; ADRs 0007 and 0008; changelog.

**flanner-meshlab:**

- [ ] Scenarios `curb-policy`, `curb-fleet`, `curb-alerts`, `curb-tier`, and
      Curb cases in `mixed-versions`, covering every R5 exit criterion. The
      client's single-device halves are in `tests/test_curb_team.py`.
- [ ] A webhook sink that checks `Flanner-Signature` and can fail on purpose.
- [ ] An approval stand-in in lab device images only, never in the wheel,
      replacing `curb_approval.method()` so `flanner curb policy --enrol`
      and `--approve` can run unattended.
- [ ] Replay steps (a captured policy, authority list or report), stored-data
      checks through `lab-control`, writable managed-settings paths, README
      rows and step tests.

**flanner-landing** (gated on `SHIPS_IN.curbTeam`):

- [ ] `app/pricing/page.tsx`: the free Curb plan and what paid adds; a "start
      free" link.
- [ ] `app/mesh/page.tsx`, `app/docs/mesh/page.tsx`: policy rules, the
      delegation and `--approve`, `--export` for device management, drift,
      the fleet view, alerts and audit export.
- [ ] CLI rows for `curb policy` (each option) and `curb fleet`.
- [ ] `lib/operations.json` regenerated: "See, apply or approve your
      organization's agent policy" (write), "Check your organization's
      devices: policy, drift and exposure" (read), and "Log tool calls and
      re-check org policy from an agent hook" (write).
- [ ] Privacy policy and terms of service, with dates in `lib/legal.ts`. Need
      the owner's approval.
- [ ] Security page sections; FAQ entries (what a fleet report holds, why an
      expired policy stays, why some changes wait).

## R6: CI check, app audit and the skill

**flanner-landing** (gated on `SHIPS_IN.curbCi`):

- [ ] The action in `app/docs/integrations/page.tsx`.
- [ ] The skill in `app/docs/skills/page.tsx` and `app/docs/agents/page.tsx`.
- [ ] FAQ entries.

**flanner-cloud:** none. **flanner-meshlab:** none.

## R7: commit attribution

**flanner-cloud:**

- [ ] `POST /v1/curb/attribution-keys`: both signatures checked, nonces
      through `replay.py`, each fingerprint bound to one device for good.
- [ ] Model and migration for keys; revocation on device removal; console
      revocation; revocations never undone.
- [ ] `POST /v1/curb/attribution-registry`: signed registry, version always
      rising, re-signed daily, 7-day expiry.
- [ ] Console privacy page, docs, changelog.

**flanner-meshlab:**

- [ ] A `curb-attribution` scenario covering registration, rotation,
      revocation, refused cross-device registration, expiry and replay.

**flanner-landing** (gated on `SHIPS_IN.curbAttribution`):

- [ ] Docs, including the verification states.
- [ ] Privacy policy lines on keys registered through GitHub, and on the key
      registry. Need the owner's approval.
- [ ] A security page section.
