# Flanner Curb support matrix

What `flanner curb` is tested against, and what it does not check. Curb PRD
§8.1 and §16 set the rules; this page is the list each release publishes.

## Tested agents (R1)

| Agent | Tested version | Surfaces | Operating systems |
|---|---|---|---|
| Claude Code | 2.1.287 | The CLI. The IDE extensions and desktop app read the same settings files, so their settings are covered too | macOS 14+, Ubuntu 22.04+ / Debian 12+, Windows 11 |
| Codex | CLI 0.154.0 | The CLI. The IDE extension and desktop app read the same config files | macOS 14+, Ubuntu 22.04+, Windows 11 |

- **Any other version** is still analysed, but every result is marked
  assumed and every channel "unsupported" until it passes the corpus below
  and the tested version moves.
- **Claude Code's sandbox does not run on native Windows.** On Windows its
  sandbox settings count for nothing; under WSL2 they count as on Linux.
- **Other agents** are not analysed.

## How it is tested

`tests/test_curb_corpus.py` holds a labelled corpus. Each case is one launch
(settings files plus launch flags), one set of planted credentials, MCP on
or off, on one operating system. Its expected result comes from the
launch's hand-written labels, taken from each agent's documentation, and the
`severity-r1` table. Curb's code computes none of them.

| | Claude Code | Codex |
|---|---|---|
| Launches | 35 | 37 |
| Credential sets | 9 | 9 |
| Cases (× MCP on/off × 3 OSes) | 1,890 | 1,998 |

The corpus is split into strata: agent × OS × channel × expected severity,
99 in all. The release gates (PRD §16):

| Gate | Target | At this commit |
|---|---|---|
| Cases per stratum | At least 20 | Smallest has 30 |
| Recall and precision of open channels, per stratum | At least 95% | 100% in every stratum |
| Agreement per agent, every channel and the severity | At least 98% | 100% for both |
| Critical invariants 1, 2, 3, 5 and 6 | Every case | Hold on every case |

Invariant 4, no credential name or location in any terminal or JSON output,
is tested in `tests/test_curb_cli.py`, with agent markers set and removed and
with terminal settings varied.

Each operating system is applied as its documented rules, so all three are
measured on any test machine. CI runs the suite on Linux, macOS and Windows.

## Leak sweep (R2)

`tests/test_curb_sweep_corpus.py` plants secrets in every artifact type
the sweep reads: 20 per stratum, half on a machine whose agent can read
everything and half on one whose agent is denied the whole test folder.
A stratum is agent × OS × artifact type, 54 in all, 1,080 secrets.

| Gate | Target | At this commit |
|---|---|---|
| Planted secrets per stratum | At least 20 | 20 in each |
| Recall, found in the right exposure class | At least 95% | 100% in every stratum |
| False positives | At most 5% | None |
| Planted values in any output | None | None |

These numbers come from a stand-in detector that knows the planted
format, so they measure where the sweep looks and how it classes what it
finds. The same corpus also runs through Kingfisher itself, with
real-format GitHub tokens carrying valid checksums:

| Detector | Planted | Found in the right class | False positives | Values in any output |
|---|---|---|---|---|
| Kingfisher 1.0.1, development laptop, 2026-10-03 | 1,080 in 54 strata | 1,080 (100% in every stratum) | None | None |

CI runs it on Linux, macOS and Windows, with the `sweep` extra installed.

Not read: plans in a plan folder other than `.plans`, and files over
64 MB (counted under Not checked).

## Fixes, tests, scrubbing and approvals (R3)

| Gate | How it is checked | At this commit |
|---|---|---|
| 100% rollback, zero config corruption | `tests/test_curb_fix_corpus.py` applies the planned fixes for every R1 corpus launch that sets user settings, on each OS, then undoes them | 106 fixes (37 Claude Code, 69 Codex): every file parsed as meant, no channel broader, every undo byte for byte |
| A write without a grant fails | `tests/test_curb_fix.py`, `tests/test_curb_scrub.py` | Fails, file unchanged |
| Backups denied to every agent | `tests/test_curb_fix.py` | Claude Code always; Codex only with a permissions profile, which its older sandbox cannot express |
| The tighten-only test rejects every known trap | `tests/test_curb_tighten.py` | The three MCP allowlist traps in PRD §10.8, each a test |
| Tester outcomes | `tests/test_curb_tester.py`, with recorded agent output | Deny rules alone: Read blocked, cat, grep and script allowed. Sandbox read denial on: all blocked. Declined, failed or prompt-stopped: inconclusive. One existing home file: not tested. Scratch pass: never enforced |
| A failing scrub leaves the file unchanged | `tests/test_curb_scrub.py` | Unchanged, and no copy of the secret anywhere |
| Synthetic input cannot complete an approval | By hand, on each OS, before each release | **Not checked yet.** On the development laptop, Windows Hello reports DeviceNotPresent, so the password prompt is what would be checked |

The tester's classifier reads Claude Code's `stream-json` and Codex's
`exec --json` events. Its tests use recorded-style output; a run against
both real agents, and the exact denial wording each returns, is still to
check by hand.

## Action log and observed use (R4)

| Gate | How it is checked | At this commit |
|---|---|---|
| Tamper detection | `tests/test_curb_log.py` edits, removes, reorders and re-signs records | Each fails `curb log --verify`, naming the record |
| Both agents share one record format | Claude Code and Codex hook payloads in `tests/test_curb_log.py` | Same fields; Codex's `bash -lc` commands read for the program inside |
| Gap flags | Sessions without the hooks, a short window, channels the hooks cannot see | "partial evidence", "no evidence", and per-channel coverage |
| No restriction for an uncovered channel | `tests/test_curb_log.py` | None suggested |

Codex's hooks are taken to cover shell commands and patches only; web
search, MCP and apps count as unseen by them. The hook payload fields were
read from each agent's documentation and have not yet been checked against
live hooks.

## Org policy, fleet view, alerts and audit export (R5)

| Gate | How it is checked | At this commit |
|---|---|---|
| Each agent's compiled output matches its saved files | `tests/test_curb_compile.py` against `tests/data/curb_policy/` | Claude Code `managed-settings.json`, Codex `requirements.toml` and the OpenShell policy match |
| Every check-in outcome in PRD §10.8 | `tests/test_curb_policy.py` | Each row, plus key rotation, revocation, an expired authority list and a successor that does not chain |
| Tighten-only changes apply under the delegation, nothing broader | `tests/test_curb_policy.py` | Applied and read back; the mixed-allowlist trap and an untested agent version wait for approval; a withdrawn delegation leaves every change pending |
| Drift by the next reconciliation | `tests/test_curb_policy.py`, `tests/test_curb_team.py` | After a local edit and after managed settings arrive |
| A replayed report is refused | `tests/test_curb_fleet.py`, `tests/test_curb_team.py` | Caught by the admin's own check, and dropped on `stale_sequence` |
| One logical alert per change | `tests/test_curb_alerts.py`, `tests/test_curb_team.py` | A retried alert keeps its id and is delivered once |
| Audit records validate and hold no secret or path | `tests/test_curb_export.py` | Against OCSF 1.9.0 API Activity's required attributes, as published at schema.ocsf.io |
| The wire contract's test vectors | `tests/test_curb_wire.py` | Every vector recomputed from the client |
| The two-device scenarios in PRD §15 | meshlab, and `scripts/mesh_dev_scenarios.py` | **Not run yet:** the control plane's R5 work is not built |

Not checked:

- The OpenShell policy has not been loaded by OpenShell, which is not
  installed here. The Codex `requirements.toml` was parsed, not loaded by
  Codex.
- A device writes each agent's user settings. Admin-owned settings come
  from `flanner curb policy --export` and device management.
- Team checks read each agent's launch from the home folder, so a
  project's own settings are not part of drift, the fleet view or alerts.

## CI check, app audit and the skill (R6)

| Gate | How it is checked | At this commit |
|---|---|---|
| Fixtures modelled on E1, E2 and E6 are flagged | `tests/test_curb_ci.py`, and the `curb-ci-action` CI job running the action itself | All three High; a workflow with none of their weaknesses Low |
| The fixture app's shapes are labelled correctly | `tests/test_curb_app.py` against `tests/data/curb_app/expected.json` | 9 calls: every shape and flag as expected |
| Skill transcripts are free of locations | `tests/test_curb_skill.py`, the skill's commands over planted secrets | No planted value or location in any output; `curb show` gives back only a notice |

Not checked:

- Agent steps other than Claude Code's, Codex's and Gemini CLI's actions
  and commands; CI other than GitHub Actions.
- JavaScript and TypeScript apps, and model calls through wrappers or
  libraries outside the list.
- A real agent running the skill: by hand, before each release.

## Commit attribution (R7)

| Gate | How it is checked | At this commit |
|---|---|---|
| An agent commit verifies with its key | `tests/test_curb_attribution.py`, in a real git repository | Attributed; git's own object id matches the one the broker logs |
| The broker refuses a commit outside an agent session | `tests/test_curb_attribution.py` | Refused, with no signature written, and the refusal logged |
| Registration carries a proof of possession | `tests/test_curb_attribution.py`, `tests/test_curb_wire.py` | The key's signature over device, key, nonce and replacement verifies; the vector matches |
| The registry's acceptance rules | `tests/test_curb_attribution.py`, `tests/test_curb_team.py` | Older, same-version-changed, key-moving and revocation-dropping registries refused |
| Known revocations hold offline; no fresh registry is "key status unknown" | `tests/test_curb_attribution.py` | As stated |
| Signatures interoperate | `ssh-keygen -Y verify` accepts the broker's signatures, and Curb verifies ssh-keygen's | Checked where ssh-keygen is installed |
| The threat model is reviewed | ADR 0008 | **Not yet**: a review by a person, before R7 ships |
| The meshlab `curb-attribution` scenario | meshlab | **Not run yet:** the control plane's R7 work is not built |

Not checked:

- Signing inside Codex's sandbox, which must reach the OS credential store.
- GitHub showing "Verified", which needs each public key added there.

## The web UI

| Gate | How it is checked | At this commit |
|---|---|---|
| No page names a credential or a location without a reveal | `tests/test_curb_web.py`, with a planted credential and a planted secret | All 13 pages; no secret value on any page, with or without a reveal |
| A reveal is one browser's, for five minutes, after a yes to the page's code | `tests/test_curb_reveal.py`, `tests/test_curb_web.py` | A second browser sees nothing; a prompt that cannot show the code is never asked |
| Nothing changes without the operating system's yes | `tests/test_curb_web.py` | A fix, an undo, logging, signing, a policy approval, the delegation, a CI fix, a test run and "forget" each change nothing without it. A removal also needs a live reveal |
| A fix is written only if the settings are as the page read them | `tests/test_curb_web.py` | A settings file edited after the check is left alone, and nobody is asked |
| No link can put words in a page | `tests/test_curb_web.py` | A message in the address is not shown, and is not carried into the page's links |
| Each part has at most one main button | `tests/test_curb_web.py` | All 13 pages |
| Text contrast is at least 4.5:1, in both themes | `tests/browser/journeys/test_accessibility.py` | All 13 pages, in a real browser |
| On a phone, every scope tab is in view and every control is 40px tall | `tests/browser/journeys/test_curb_journeys.py`, at 320px | All 13 pages |
| A review opens with scripts on, and with scripts off | `tests/browser/journeys/test_curb_journeys.py` | Over the page with scripts; as the same page with the review open without |
| No page reaches the control plane | `tests/test_architecture.py` | `web`, `curb_page`, `curb_do` and `curb_ops` cannot import `account` |

Not checked:

- The operating system's own prompt, for a reveal or for a change from a
  page. The tests use a stand-in. On Windows, the page was run against this
  machine's real agents and a real scan, without approving a prompt.
- A screen reader, and keyboard-only use of each part.
- The progress redraw and the names countdown in the page script. They
  were watched in a browser during a real scan, and have no test.
- Agent states the test machine does not have, such as an agent on a
  version Curb was tested with, or a scheduled job.

## Scan time

Inventory plus reach now must take under 10 seconds at the 95th percentile
on a reference laptop.

| Machine | Runs | Median | p95 |
|---|---|---|---|
| Windows 11 Home development laptop, both agents installed, 2026-10-02 | 20 | 1.61 s | 1.95 s |

Measured as `flanner curb map --json`, wall clock, from process start.

The leak sweep must take under 2 minutes at the 95th percentile, at low
CPU priority, in under 500 MB. It lowers its own priority and reads one
file at a time, skipping files over 64 MB.

| Machine | Files | Runs | Median | p95 |
|---|---|---|---|---|
| Windows 11 Home development laptop, Kingfisher 1.0.1, otherwise idle, 2026-10-03 | 1,915 | 10 | 80.2 s | 115.2 s |

Measured as `flanner curb sweep --json`, wall clock, from process start.
With 10 runs, the 95th percentile is the slowest run. It is inside the
target with little room: under load, one run took 165 s. Memory use has
not been measured yet.

## Not checked

Each report lists these under "not checked":

- Claude Code managed settings delivered by MDM, the registry or the
  claude.ai console. Files in the managed settings folder are read.
- Plugin-provided MCP servers and hooks, and claude.ai connectors.
- Codex cloud-managed defaults.
- Codex apps connected to the ChatGPT account. Apps are on by default in
  Codex 0.154.0, so they count as unknown until `features.apps` is false.
- Codex system config and `requirements.toml` on Windows, which have no
  documented location there.
- Running sessions and shell aliases. Every report states its launch
  context and assumes no other flags, profiles or settings files.
