# What breaks, what it costs, and how to get back

Written per component, in the shape Amazon's operational readiness review
asks for: what can fail, what the user loses, and what the recovery path is.
Nothing here is aspirational — where there is no recovery path, it says so.

The organising fact is that **the markdown files are the product and the
database is an index over them**. Most failures are therefore recoverable,
because the thing that matters is on disk in a format any editor can read.

---

## The catalog (`~/.flanner/data.db`)

**Soft failure — locked.** Another flanner process holds it, usually
`flanner web` or a stuck MCP server. Commands report the lock rather than
waiting forever. Recovery: close the other process. No data is at risk;
SQLite refuses the write rather than half-applying it.

**Hard failure — corrupt or deleted.** The catalog is gone.

*Blast radius:* version history, review state, comments, and the artifact
store. **Not** the plans themselves: every version is a real file in
`.plans/`, named `name_v1.md`, `name_v2.md`, and readable without flanner.

*Recovery:* `flanner init` recreates the store, `flanner sync` re-imports the
files on disk. What does not come back: review decisions, comments, and
signed artifacts, because those live only in the database. A teammate who
holds them can push them back — that is what makes peer sync a partial
backup rather than only a sync.

*Prevention:* the store is append-only and there is no cleanup command, so
nothing routinely deletes from it.

---

## Plan files (`.plans/`)

**Soft failure — edited outside flanner.** Common and expected: an agent or
a person writes to the file directly. `flanner doctor` reports the hash
mismatch; `--repair` adopts the new content as a version.

**Hard failure — deleted.** `flanner doctor` reports `missing_file`. The
catalog still holds the metadata but the content is gone, and flanner cannot
reconstruct it: it never keeps a second copy. Recovery is git, if the file
was committed, or a teammate who synced it.

*This is the one place where loss is real.* `.plans/` is git-ignored by
default, so a deleted plan that was never synced has no copy anywhere.

---

## Device identity (`~/.flanner/device_key`)

**Hard failure — lost.** The private key is gone.

*Blast radius:* this device's identity. A device id is the hash of its public
key, so a new key is a new device. It cannot inherit the old one's history,
and artifacts it already signed stay valid — they were signed by a key that
existed.

*Recovery:* enrol again. `flanner accept` or `flanner login` creates a new
key and a new device. An admin revokes the old one from the console.

*No recovery exists for the key itself.* It is generated locally and never
leaves the machine, so nobody — including us — holds a copy to restore.
That is the same property that makes the design worth having.

---

## The control plane

**Soft failure — unreachable.** Network down, or the service is.

*Blast radius:* nothing local. Plan work needs no account. Peer sync keeps
working while the held entitlement is valid, because peers authorise each
other against a signed note rather than by asking us — that is the point of
issuing entitlements instead of checking sessions.

*Recovery:* none needed. It resumes.

**Hard failure — down past the entitlement lifetime.** Entitlements last a
day. Past that a device enters grace: reads still work, pushes are refused.
Past the grace window team features stop entirely, and local work is
untouched.

---

## Peer sync

**Soft failure — no direct route.** Two machines cannot reach each other.
Falls back to an encrypted relay automatically. `flanner peer status` says
whether a connection was direct or relayed.

**Soft failure — clocks disagree.** Requests are refused past
`device_auth.MAX_SKEW`. See [clock-skew.md](clock-skew.md).

**Hard failure — a peer sends a bad artifact.** Rejected, not stored. Every
artifact is verified against its *author's* key from the org keyring, not
the key of whoever handed it over, so relaying does not launder anything.

*Blast radius:* none. A failed sync leaves both sides as they were.

---

## Retirement is not deletion

`flanner retire` asks peers to stop showing a plan. It is a claim other
devices honour, not an erasure.

A teammate offline at the time keeps the content until they next sync, and
anyone already holding the bytes keeps them. That is the strongest promise
an append-only store spread across machines we do not control can honestly
make, and pretending otherwise would be the actual failure.

---

## Restart from zero

After any crash:

```bash
flanner doctor            # catalog against the files, and enrollment state
flanner doctor --repair   # adopt orphans, fix stale version counters
```

`doctor` is the integrity check. It reports what disagrees and which
disagreements it can fix, so the answer to "is my data okay?" is a command
rather than a judgement call.

---

## Exit codes, for scripts recovering automatically

| Code | Means | Retry? |
|------|-------|--------|
| 0 | Worked | — |
| 1 | The request cannot be satisfied as asked | Only after changing it |
| 2 | The machine underneath failed | No |

---

## Curb (`flanner curb`)

Curb reads; the only files it writes are its own redacted reports and digest key, so a failure costs a report, not data.

| What fails | What you see | What it means |
|---|---|---|
| A settings file will not parse | Its layer is listed with the problem, under Assumed | Every channel counts as unknown, so reach is never understated |
| The agent's version is not the tested one | Every result is marked assumed | Curb's rules were checked against the baseline only |
| A launch uses a flag Curb does not know | Every channel is unknown | The flag could have changed anything |
| No desktop session, or no Tk | `curb show` explains and exits 1 | `curb map` still gives the redacted report |
| A scheduled job's command cannot be read | The job is listed with its problem | It is not assessed |
| A Codex config sets both `default_permissions` and `sandbox_mode` | Listed under Assumed; every channel unknown | Codex documents that the two do not combine, so Curb cannot tell which applies |
| Kingfisher is not installed | `curb sweep` names the `flanner[sweep]` extra and exits 1 | Nothing was read |
| A file is over 64 MB, or cannot be read | Counted under Not checked | That file's secrets are not counted |
| You answer no to validation, or nobody is there to answer | No issuer is contacted | Findings are counted unvalidated |
| No OS keychain | The digest key is a user-only file in `~/.flanner/curb/` | Same fingerprints, weaker custody |
| The digest key is lost | A new one is made | Older reports' fingerprints stop matching |
| No approval method (no desktop, no Hello, Touch ID or polkit) | `curb fix` lists the changes and exits 1 | Nothing is written; make them by hand |
| You refuse or ignore the approval prompt | "Not approved", exit 1 | Nothing is written. Three in ten minutes pause requests for an hour |
| A fixed file does not read back as written | Every file is put back, exit 1 | The settings are as they were |
| `curb fix --undo` finds a file edited since the fix | That file is left as it is | Putting the copy back would lose the edit |
| A Codex config cannot be edited line by line | The changes become steps for you | Nothing is written to it |
| The agent declines, a call never runs, or a prompt stops it | That method is "inconclusive" | Nothing is proved; it is never counted as blocked |
| The agent is not installed or signed in | Every method is "inconclusive" (the run failed) | Nothing is proved |
| A rule names one existing file outside the project | "not tested" | Curb never moves or edits a real file to test it |
| The credential's folder is inside a git working tree | "not tested" | A decoy there could be committed |
| Settings change after a test | The channel goes back to "configured" | A proof holds only for the settings it ran against |
| A scrubbed line would no longer parse, a secret also appears escaped, or the file changed since it was read | `curb scrub` says why and exits 1 | The file is left exactly as it was |
| A scrub succeeds | The secrets are placeholders | There is no undo: no copy of the secret is kept anywhere |
| The action log cannot be written (locked, full disk, no key) | Nothing; the hook exits quietly | The tool call goes ahead unlogged, and observed use counts that session as unlogged |
| A record is edited, removed or reordered | `curb log --verify` names the record and exits 1 | The log can no longer be trusted from that record on |
| Sessions ran without the hooks | `curb observed` says "partial evidence" and how many | Their tool calls are unknown |
| The control plane cannot be reached | `curb policy --check-in` says so | The policy in force stays in force. Reports wait up to 7 days; alerts are retried with the same event ids |
| A policy fails its signature, names another organization, uses an unknown or revoked key, or has expired | Refused and alerted | The policy in force stays in force |
| The same policy version arrives with other contents, or an older version arrives | An integrity error or a rollback, alerted | The policy in force stays in force |
| The policy authority list is older than the cached one, or has expired | Older: refused. Expired: no new policy is accepted | The policy in force is never weakened |
| The policy in force expires, or its key is revoked later | Flagged expired, or signed by a revoked key | It stays in force until a newer policy arrives |
| A policy change would broaden a channel, or its effect cannot be established (an agent version Curb has not tested) | It waits for `curb policy --approve` | Nothing broader is written without your yes |
| The delegation is off, or the machine has no approval method | Every policy change waits | The fleet view shows it pending |
| A policy rule this flanner does not know | Reported as unmet: "update flanner" | The rules it knows still apply |
| Settings change after a policy applied (by hand, by MDM, from the console) | Drift at the next reconciliation | The fleet view shows drift, and growth in reach raises an alert |
| The control plane refuses a fleet report as replayed (`stale_sequence`) | Nothing | The report is dropped: it could never be accepted |
| The audit collector refuses records or is down | `curb policy --check-in` says the records wait | They stay in the action log; past 10,000 waiting, the oldest are skipped and counted as dropped |
| flanner is older than a Curb endpoint's minimum | "update flanner to X or later" | Nothing else changes |

## Known gaps

Stated rather than left to be discovered:

- **No backup command.** Copying `~/.flanner/` and `.plans/` is the backup.
- **No retry on peer sync.** A failed sync is reported and the user runs it
  again. Control-plane calls do retry a connection failure (three tries with
  jitter, in `account._post`), but only where repeating is harmless — never
  an enrolment, which spends a one-shot code.
- **`.plans/` is git-ignored by default**, so an unsynced, uncommitted plan
  that is deleted is unrecoverable.
- **Curb's team checks read each agent's launch from the home folder.** A
  project's own settings are not part of the policy's drift, the fleet
  view or alerts.
- **No integrity check on read.** `doctor` verifies on demand, not on every
  open, so a corrupt row is noticed when somebody looks.
