# Flanner Curb: work other repos need, by release

Every Curb release lands in more than one repo (Curb PRD §12.2, §12.3). The
flanner client work is on `feat/curb-r1`. This page lists what the other
repos still need for each release, so nothing is left behind.

**Status (2026-10-07): the R1 to R7 work is built in every repo, each on
its own `feat/curb-r1`, and none of it is pushed.** The unticked items
below are what remains. The legal pages went live on 2026-10-07 with the
owner's approved wording. The web UI screenshots went up on 2026-10-07, taken
against a machine made up for them. The control plane's lock update moved
with 0.16.0. Release day sets
`PYPI_VERSION`, the seven `SHIPS_IN.curb*` versions, which are
unscheduled until a release is named, and the control plane's
`curb.MINIMUM_VERSION`.

Rules that apply to every release (PRD §12.3):

- Use the same branch name, `feat/curb-r1`, in each repo, so meshlab can
  pair branches.
- Landing pages merge early behind their `SHIPS_IN` key and appear when
  `PYPI_VERSION` flips on release day.
- No public page uses the name "Curb" until the trademark search clears.
  Cleared on 2026-10-06: `CURB_NAME_CLEARED` is on, and each page still
  waits for its `SHIPS_IN` version.
- Legal pages change only after the owner approves them.
- A release is done only when every repo's list below is done.

## R1: inventory and reach now

Client work is done. Commands: `flanner curb map`, `flanner curb show`,
`flanner curb inventory`.

**flanner-landing** (all gated on `SHIPS_IN.curbMap`):

- [x] `lib/site.ts`: `SHIPS_IN.curbMap` and a `CURB_MAP_RELEASED` flag.
- [x] Product page `app/curb/page.tsx`, with metadata from `lib/seo.ts`, and
      entries in `app/sitemap.ts`, the `app/llms.txt` route and the home page.
- [x] Docs page `app/docs/curb/page.tsx`, added to `lib/docs-nav.ts`. Cover:
  - the channels (file tools, shell files, shell network, web, MCP, Codex
    apps) and why each is judged on its own;
  - `severity-r1` v1, rules H1, H2, M1, M2 and L1;
  - evidence levels (configured, assumed; enforced arrives in R3);
  - launch contexts: `--dir`, `--agent`, `--profile`, and a launch command
    after `--`; scheduled jobs; the stated assumption;
  - the tested versions and what is not checked, from
    `docs/curb-support-matrix.md`;
  - Codex apps count as unknown until `features.apps = false`.
- [x] CLI rows in `app/docs/cli/page.tsx` for `curb map` (`--dir`, `--agent`,
      `--profile`, `--json`, `-- <launch command>`), `curb show` and
      `curb inventory` (`--json`, plus the same launch options).
- [x] A note in `app/docs/agents/page.tsx` that Curb has no MCP tools, and why.
- [x] `lib/operations.json`, regenerated from `feat/curb-r1` with
      `python -m flanner.operations`. It adds the `curb` domain, which needs a
      label in the agent tables, with two CLI-only read operations: "List the
      agents here and what each one loads" and "See what each agent launch can
      reach".
- [x] FAQ entries in `lib/faq.ts`: why most machines rate High; why a Read
      deny rule alone does not protect a file; what "assumed" means; why there
      are no MCP tools; that nothing leaves the machine.
- [x] Troubleshooting in `app/docs/troubleshooting/page.tsx`: `curb show`
      saying "there is no desktop session here" (no `DISPLAY` or
      `WAYLAND_DISPLAY`) or "Python's Tk support is missing" (install
      `python3-tk` on Linux).
- [x] A section on `app/security/page.tsx`: output is redacted for every
      caller, detail only in a desktop window, all processing local.
- [ ] Release day: `PYPI_VERSION` set to the version that ships R1.

**flanner-cloud:** none. **flanner-meshlab:** none. **flanner-brand:** none.

## R2: leak sweep

Client work is done. Commands: `flanner curb sweep`, `flanner curb show
--sweep`, `flanner curb forget`. Detection needs the `flanner[sweep]` extra.

**flanner-landing** (gated on `SHIPS_IN.curbSweep`):

- [x] `SHIPS_IN.curbSweep`.
- [x] Docs for the sweep: where it reads (transcripts, Codex sessions and
      prompt history, CLAUDE.md and AGENTS.md, skills, MCP configs and agent
      settings, shell history, `.env` files, flanner plans and memories); the
      classes A (sent to a model provider), B (readable by an agent) and C
      (on disk but blocked); install with `pip install 'flanner[sweep]'`.
- [x] CLI rows: `curb sweep` (`--dir`, `--json`, `--validate`),
      `curb show --sweep [--validate]`, `curb forget [--yes]`.
- [x] `lib/operations.json` regenerated: two more `curb` operations, "Find
      secrets agents left behind" (read) and "Delete what Curb keeps on this
      machine" (destructive).
- [x] FAQ entries: why a transcript secret means rotate now; that no command
      prints a value; what `--validate` sends, and to whom; what `forget`
      deletes.
- [x] Troubleshooting: "the leak sweep needs Kingfisher"; files over 64 MB
      under Not checked.
- [x] Privacy policy: an opted-in check sends a found key straight to its own
      issuer. Needs the owner's approval.
- [x] The date in `lib/legal.ts`.

**flanner-cloud:** none. **flanner-meshlab:** none.

## The web UI

Client work is done (PRD §11.3). One section at `/curb`, built from
`curb_page` (what each page shows), `curb_do` (what each button does),
`curb_live` (what the process holds in memory) and `curb_reveal` (which
browser may see names). It ships part by part with the release each part
belongs to. Nothing in the control plane changes: the pages never reach it.

**flanner-landing:**

- [x] `app/docs/web/page.tsx`: the Overview, the three scopes and their
      parts, the project picker, and "Add a project".
- [x] Names and locations: the four-digit code, the 5 minutes, "Hide names",
      and that a Linux desktop keeps them in `flanner curb show`.
- [x] The review before each change, and that nothing changes without the
      operating system's prompt.
- [x] What stays in the terminal and why: check-in, `flanner curb fleet`,
      policy export, a launch command, and adding keys to GitHub.
- [x] `lib/operations.json` regenerated: every Curb operation now lists its
      web routes, and "See each change that widened what an agent can
      reach" is new, on the web only.
- [x] Screenshots of the Overview, Leaks and Fixes, in both themes.

## R3: fixes, tester, scrubbing and approvals

Client work is done. Commands: `flanner curb fix [--dry-run|--undo]`,
`flanner curb test`, `flanner curb decoys [--renew|--remove]`, `flanner curb
scrub FILE [--dry-run]`, `flanner curb forget --backups`. Web: the Curb
section at `/curb` (see "The web UI" below).

**flanner-landing** (gated on `SHIPS_IN.curbFix`):

- [x] Docs for fixes (what each kind writes, backups for 7 days, `--undo`),
      the tighten-only rule, tester outcomes (blocked, allowed, inconclusive,
      not tested, unsupported, "passed in scratch context"), decoys and
      scrubbing (no backup, rotate first).
- [x] Approvals: Windows Hello or the account password, Touch ID or the
      password, polkit on a Linux desktop; grants single use for two minutes;
      the pause after three refusals; no method means read-only.
- [x] CLI rows for the five commands above.
- [x] `lib/operations.json` regenerated: "Fix what each agent can reach"
      (write), "Prove a block by asking the agent to get past it", "List,
      renew or remove the tester's decoys", "Scrub rotated secrets out of a
      file" (destructive), each with its web routes under `/curb`.
- [x] `app/docs/web/page.tsx`: the Curb section (see "The web UI" below).
- [x] Troubleshooting: "No approval method on this machine", "Not approved",
      the pause, "inconclusive" results, a Codex config edited by hand.
- [x] Security page: approvals and grants, backups denied to agents.
- [x] `app/disclaimer/page.tsx`: what Curb's tests do and don't prove. Needs
      the owner's approval.

**flanner-cloud:** none. **flanner-meshlab:** none.

## R4: action log and observed use

Client work is done. Commands: `flanner curb log [--enable|--disable|--verify]`,
`flanner curb observed [--json]`; the hidden hook `flanner hook curb-record`.

**flanner-landing** (gated on `SHIPS_IN.curbObserve`):

- [x] Docs: what a record holds (metadata, never content), the hash chain
      and device signature, 30-day retention, the three evidence states, the
      14-day and 20-session window, what the hooks cannot see.
- [x] CLI rows for `curb log` and `curb observed`.
- [x] `lib/operations.json` regenerated: "Turn Curb's action log on or off,
      or check it" (write) and "See what each agent has been seen using"
      (read).
- [x] FAQ: why "not seen" is not "not needed"; does the log hold my prompts
      (no); what happens if a hook fails (the agent carries on).

**flanner-cloud:** none. **flanner-meshlab:** none.

## R5: team features on Flanner Mesh

Client work is done. Commands: `flanner curb policy [--check-in|--enrol|--withdraw|
--approve|--export DIR|--json]`, `flanner curb fleet [--json]`; the hidden hook
`flanner hook curb-session`. The wire format, with test vectors, is
`docs/curb-wire-contract.md`: build the control plane against that page, not
against client code.

**flanner-cloud** (PRD §12.2 R5 has the full contract):

- [x] Wire contract: hold every name in `docs/curb-wire-contract.md` as a
      string literal, and check its test vectors in the cloud suite.
- [x] Plans: a free Curb plan in `issuer.py` (`curb_policy`, `curb_fleet`,
      `curb_alerts`); `managed_mesh` gains them. Lapsed trials drop to the free
      plan (`billing.py`); `sign_up.html` says what stays free.
- [x] Entitlement responses list `curb_capabilities`: `curb-policy/1`,
      `curb-fleet/1`, `curb-alerts/1`. The client uses nothing without them.
- [x] `relay.py` refuses relay to free-plan orgs; sync stays off for them.
- [x] Models and Alembic migrations: policy versions, device policy states,
      fleet reports, alert destinations, alert outbox, delivered event ids,
      policy authority list.
- [x] Policy authority: separate signing key behind `signing.py`; the signed
      `curb_authority` list at `POST /v1/curb/authority`, signed by the
      entitlement issuer key; rotation (active, retiring, revoked) and
      revocation rules; yearly rotation in the ops console.
- [x] Policies: each `curb_policy` carries `previous_hash`; re-signing always
      issues a new version; `POST /v1/curb/policy` answers `unchanged` for the
      current version and hash, `none` with no policy, and sends
      `audit_export_token` outside the signed policy.
- [x] Version negotiation: the `Flanner-Client-Version` and
      `Flanner-Curb-Capabilities` headers, `client_too_old` (HTTP 426) with
      `minimum_version`, mixed-version tests.
- [x] API in `api.py`, limits in `ratelimit.py`: `/v1/curb/authority`,
      `/v1/curb/policy`, `/v1/curb/policy/state`, `/v1/curb/reports`
      (`stale_sequence`, HTTP 409, for a sequence not higher than the last),
      `/v1/curb/alerts` (answers `accepted` with every id it holds).
- [x] **Not in PRD §12.2, needed by the client:** `POST /v1/curb/fleet` for
      admins (`not_admin` otherwise), returning each device's label and its
      last 30 days of signed reports, oldest first. `flanner curb fleet`
      verifies them itself with the device keyring.
- [x] Alert relay: adds the device label, delivers each event id at least
      once, drops ids delivered in the last 7 days, and sends the
      `Flanner-Event-Id` and `Flanner-Signature` headers defined in the
      contract.
- [x] Console: policy editor with preview ("device evaluation required"
      where settings outside the policy decide), fleet page, alert
      destinations with a test send, audit entries.
- [x] Customer secrets encrypted at rest (envelope encryption with Vault
      transit), shown once, rotated yearly; `docs/secrets.md`. Audit collector
      tokens join webhook and Slack secrets here.
- [x] Jobs: alert delivery with retries, pruning, report deletion on device
      removal.
- [x] Operations: metrics, `docs/alerts.md`, `docs/failure-model.md`,
      benchmarks.
- [x] `scripts/seed_curb_policy.py`: publish the dev team's next policy
      version from a rules file, for the client's
      `scripts/mesh_dev_scenarios.py` cases `curb-policy`, `curb-fleet` and
      `curb-alerts`.
- [x] Tests and seeds; console privacy page; ADRs 0007 and 0008; changelog.
- [x] Lock update after the client release.

**flanner-meshlab:**

- [x] Scenarios `curb-policy`, `curb-fleet`, `curb-alerts`, `curb-tier`, and
      Curb cases in `mixed-versions`, covering every R5 exit criterion. The
      client's single-device halves are in `tests/test_curb_team.py`. One
      criterion is covered by the control plane's own tests instead: the
      lab's relay runs in development mode and asks nobody who may use it,
      so the free plan being refused the relay is not a lab case.
- [x] A webhook sink that checks `Flanner-Signature` and can fail on purpose.
- [x] An approval stand-in in lab device images only, never in the wheel,
      replacing `curb_approval.method()` so `flanner curb policy --enrol`
      and `--approve` can run unattended.
- [x] Replay steps (a captured policy, authority list or report), stored-data
      checks through `lab-control`, writable managed-settings paths, README
      rows and step tests.

**flanner-landing** (gated on `SHIPS_IN.curbTeam`):

- [x] `app/pricing/page.tsx`: the free Curb plan and what paid adds; a "start
      free" link.
- [x] `app/mesh/page.tsx`, `app/docs/mesh/page.tsx`: policy rules, the
      delegation and `--approve`, `--export` for device management, drift,
      the fleet view, alerts and audit export.
- [x] CLI rows for `curb policy` (each option) and `curb fleet`.
- [x] `lib/operations.json` regenerated: "See, apply or approve your
      organization's agent policy" (write), "Check your organization's
      devices: policy, drift and exposure" (read), and "Log tool calls and
      re-check org policy from an agent hook" (write).
- [x] Privacy policy and terms of service, with dates in `lib/legal.ts`. Need
      the owner's approval.
- [x] Security page sections; FAQ entries (what a fleet report holds, why an
      expired policy stays, why some changes wait).

## R6: CI check, app audit and the skill

Client work is done. Commands: `flanner curb ci [PATH] [--sarif FILE]
[--fail-on LEVEL] [--fix] [--json]`, `flanner curb app [PATH] [--sarif FILE]
[--json]`. The GitHub Action is `actions/curb-ci/` in this repo, used as
`elandlabs/flanner/actions/curb-ci@<tag>`, with its own CI job. `flanner init`
installs the `agent-blast-radius` skill.

**flanner (release day):** the action is used by tag, so the release tag
that ships R6 is what users pin. Pushing it needs the owner's go-ahead, like
every tag.

**flanner-landing** (gated on `SHIPS_IN.curbCi`):

- [x] `SHIPS_IN.curbCi`.
- [x] `app/docs/integrations/page.tsx`: the action, with the workflow from
      `actions/curb-ci/README.md` (checkout without persisted credentials,
      the action, then `github/codeql-action/upload-sarif`), its inputs, the
      `ci-r1` table, and what `fix: true` changes.
- [x] CLI rows for `curb ci` and `curb app`.
- [x] Docs for `curb app`: the libraries it reads, the three shapes, the two
      flags, and that every result is assumed (Python only).
- [x] `app/docs/skills/page.tsx` and `app/docs/agents/page.tsx`: the
      `agent-blast-radius` skill, its commands, and why it never sees names
      or locations.
- [x] `lib/operations.json` regenerated: "Check the agent steps in a
      repository's CI workflows" (write) and "Find the LLM calls in an
      application's code, and their shapes" (read).
- [x] FAQ entries: why an issue-triggered agent step rates High; what the
      fixes change; why the app audit says "assumed".

**flanner-cloud:** none. **flanner-meshlab:** none.

## R7: commit attribution

Client work is done. Commands: `flanner curb attribution [--setup|--rotate]
[--github] [--json]`, `flanner curb verify [REVISION] [--json]`; git's signing
program `flanner-curb-sign` (a new console script). Registration, the
registry and the five states are in `docs/curb-wire-contract.md`, with test
vectors; the design and threat model are ADR 0008.

**flanner (before release):** the threat model in ADR 0008 needs a review by
a person, an R7 exit criterion.

**flanner-cloud:**

- [x] Plans: add `curb_attribution` to the free Curb plan and `managed_mesh`,
      and `curb-attribution/1` to `curb_capabilities`.
- [x] `POST /v1/curb/attribution-keys`: verify the device envelope and the
      key's `proof` over the canonical `curb_attribution_proof` document;
      refuse a reused nonce through `replay.py` (`replayed`), a bad proof
      (`bad_proof`), a fingerprint registered to another device
      (`key_owned`, enforced by a unique constraint), and a replacement that
      names no active key of the same device (`replaces_unknown`). Mark the
      replaced key retired.
- [x] Model and migration for keys: fingerprint (OpenSSH `SHA256:` form),
      public key, agent, device, status and when it changed.
- [x] Revocation: removing a device revokes every key it registered,
      retired ones included, and no other device's; an admin can revoke one
      key from the console; revocations are never undone.
- [x] `POST /v1/curb/attribution-registry`: the signed
      `curb_attribution_registry`, scoped to the caller's organization,
      signed by the issuer key; the version rises with every change; re-signed
      daily and on each change, with a 7-day expiry.
- [x] Console privacy page, docs, changelog.

**flanner-meshlab:**

- [x] A `curb-attribution` scenario: A and B register keys and A rotates
      one; a fresh C verifies A's active and retired keys; removing A revokes
      A's keys and no one else's; A cannot register or replace B's key; with
      the control plane stopped and the registry expired, B's commits show
      "key status unknown" and A's stay revoked, and a device that never
      fetched one shows "key status unknown"; a replayed older registry and
      one that drops A's revocation are refused; a fresh registry resolves
      the unknown results.
- [x] The lab-only approval stand-in also covers
      `flanner curb attribution --setup`.

**flanner-landing** (gated on `SHIPS_IN.curbAttribution`):

- [x] Docs: setup (one approval; the agent settings it writes; `--github`),
      rotation, `curb verify` and its five states, what a signature does and
      does not show, and that the developer or malware running as the same
      user can still call the broker.
- [x] CLI rows for `curb attribution` and `curb verify`.
- [x] `lib/operations.json` regenerated: "Give each agent its own
      commit-signing key, rotate or list them" (write) and "See which agent
      key signed each commit" (read).
- [x] Privacy policy lines on public keys registered through the person's
      own GitHub sign-in, and on the key registry. Need the owner's approval.
- [x] A security page section.
