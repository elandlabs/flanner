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

Curb reads; it never writes, so a failure costs a report, not data.

| What fails | What you see | What it means |
|---|---|---|
| A settings file will not parse | Its layer is listed with the problem, under Assumed | Every channel counts as unknown, so reach is never understated |
| The agent's version is not the tested one | Every result is marked assumed | Curb's rules were checked against the baseline only |
| A launch uses a flag Curb does not know | Every channel is unknown | The flag could have changed anything |
| No desktop session, or no Tk | `curb show` explains and exits 1 | `curb map` still gives the redacted report |
| A scheduled job's command cannot be read | The job is listed with its problem | It is not assessed |

## Known gaps

Stated rather than left to be discovered:

- **No backup command.** Copying `~/.flanner/` and `.plans/` is the backup.
- **No retry on peer sync.** A failed sync is reported and the user runs it
  again. Control-plane calls do retry a connection failure (three tries with
  jitter, in `account._post`), but only where repeating is harmless — never
  an enrolment, which spends a one-shot code.
- **`.plans/` is git-ignored by default**, so an unsynced, uncommitted plan
  that is deleted is unrecoverable.
- **No integrity check on read.** `doctor` verifies on demand, not on every
  open, so a corrupt row is noticed when somebody looks.
