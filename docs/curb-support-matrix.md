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
finds. The same corpus runs through Kingfisher itself, with real-format
GitHub tokens, wherever the `sweep` extra is installed. It has not run
yet: Kingfisher was not installed on the development laptop.

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

## Scan time

Inventory plus reach now must take under 10 seconds at the 95th percentile
on a reference laptop.

| Machine | Runs | Median | p95 |
|---|---|---|---|
| Windows 11 Home development laptop, both agents installed, 2026-10-02 | 20 | 1.61 s | 1.95 s |

Measured as `flanner curb map --json`, wall clock, from process start.

The leak sweep must take under 2 minutes at the 95th percentile, at low
CPU priority, in under 500 MB. It lowers its own priority and reads one
file at a time, skipping files over 64 MB. Its time has not been measured
yet, because that needs Kingfisher installed.

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
