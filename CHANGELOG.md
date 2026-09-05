# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **The local web UI is 27-120x faster to first byte.** Every page carried a
  freshness walk: git subprocesses for every plan in every project, computed
  inline, including on pages showing no freshness at all. One cold `/projects`
  ran 133 git processes and took 9 seconds. Evidence is now cached on the
  repository's HEAD and the plan's content hash, computed once per request
  rather than twice, and the two views that genuinely need the walk — the
  sidebar badge and the projects freshness column — fetch it after the page
  is up instead of blocking it.
- **The freshness page fills in as each plan is judged**, rather than after
  all of them are. The scan is the same work; what changes is that the page
  arrives at once and the first drifted plan is readable in about a tenth of
  a second instead of nine seconds. A count says how far along it is, and
  `?full=1` still renders everything server-side for the noscript case.
- `git grep` is asked once per plan rather than once per symbol, and `git log
  --all -S` — the most expensive call this module makes, ~370ms each — is
  memoized on the repository's HEAD.
- `flanner login` says what to do next. It printed one line and stopped,
  which is silence at the moment somebody setting up a team for the first
  time has least idea what to type. What it says depends on what the account
  holds: an account with no workspace access is pointed at the console and
  `whoami --refresh`, and one that holds a grant is given `init`, `join` and
  `peer serve`.

## [0.10.0] - 2026-09-04

### Added

- `flanner start` runs the MCP server in the background for real, over http
  on 127.0.0.1, and `flanner stop` stops it. Both previously described a
  process that was never created: `start` printed a config snippet, and
  `stop` and `status` read a pid file nothing ever wrote. This is for a
  client that cannot spawn its own copy over stdio, for two editors sharing
  one server, or for using flanner without an agent at all.
- A version arriving from a peer becomes a file and a version record, rather
  than being stored and reported as `accepted` with nothing to open.
- `flanner status` shows one row per agent — Claude Desktop, Claude Code,
  Codex — each checked where that agent actually looks. It used to read
  Claude Desktop's config and call the result "Claude Code", so a correct
  setup read as "not registered". `flanner setup` now prints the Codex
  registration lines it cannot write.
- `flanner peer pull` reports a plan that was stored but could not be
  written on its own line, and exits 1 for it. A second pull writes what the
  first could not; before, an artifact already held was never looked at
  again, so such a plan was verified, stored and stuck.
- An end-to-end test of the advertised workflow: enrol, join, pull over http
  between two real device identities, and open the file. Nothing covered this
  before, which is how a missing production call survived two green suites.
- `flanner list` names each plan's owner, and sorts plans with a teammate's
  version waiting to the top.

- Peer requests now spend their nonce, so a captured request cannot be
  answered twice. The control plane has done this since revocation shipped;
  between two devices the freshness window was the only defence, which meant
  it could not be widened for drifting clocks without widening the replay
  window by the same amount.
- `flanner doctor` reports how far this machine's clock is from the server's,
  before the drift is large enough to make peers refuse each other.
- Property-based tests over the protocol invariants, and fault-injection
  tests over the deserialisation boundaries.

### Changed

- Where a pulled plan goes is now a rule rather than `.first()`: a plan
  already held goes where it lives, then the project named with `--project`
  or run from, then a workspace's only project. Two repositories in one
  workspace with nothing to say which is reported, not guessed.
- `flanner join` checks access before it binds. It used to bind, commit,
  re-sign every plan into the workspace, and only then say this device holds
  no role there — so a mistyped id cost a repository its plans' history in a
  workspace nobody can reach. A refusal now changes nothing.
- `flanner init` with nothing on stdin takes the offered project name instead
  of dying with "Aborted!", so it works from a script or CI.
- CI runs the suite on Windows and macOS as well as Linux, and every test has
  a five-minute timeout so a hang fails instead of stalling the job.
- A version arriving from a peer no longer moves the plan's current version.
  Your file was never overwritten, but `flanner show`, the web UI and every
  agent read the pointer, so a teammate pushing changed what you had open. A
  plan this device has only ever received still tracks along, and accepting a
  baseline through `flanner review` moves it.
- The http peer transport listens on loopback by default rather than every
  interface, and refuses a request body over `MAX_REQUEST_BYTES` before
  parsing it. The existing limits all run after the body is a dict.
- `cryptography` widens to `<51`, taking 50.x, which clears PYSEC-2026-3552.
- The signed-request freshness window is 5 minutes, up from 2. Now that the
  nonce refuses replays, the window only has to tolerate clock drift — and on
  Windows the time service ships stopped, so minutes of drift is the default
  state rather than an edge case.
- A system failure exits 2; a user error still exits 1. Both are documented
  in the README. Previously everything exited 1 and a disk error arrived as a
  traceback, so a script could not tell "fix your command" from "retrying
  will not help".
- `server.json` no longer has to be remembered on release: a test fails if it
  disagrees with `pyproject.toml`.

### Fixed

- `flanner init` could hang indefinitely if `claude mcp add` blocked. It is
  now bounded at 30 seconds.
- `flanner init` no longer replaces a `.mcp.json` or `.claude/settings.json`
  it cannot parse. Both are read, merged into and written back, and an
  unparseable file was read as `{}`, so the write discarded every other MCP
  server, hook and permission the repo had declared. A trailing comma was
  enough. The file is now left alone and the reason reported, and the rest of
  the integration still installs.
- Creating a plan is failure-atomic. The plan row was committed before its
  first version was written, so a failed write left a plan with no versions
  holding the name, and every retry afterwards was refused as a duplicate —
  permanently, even once the cause was fixed.
- A keychain that cannot be read no longer costs this machine its identity.
  Once the signing key moves to the keychain the file is deleted, so a locked
  keychain looked exactly like a machine that had never run flanner, and the
  answer was to generate a new key — silently making it a different device,
  whose signatures peers reject and whose plans are stranded. It now refuses
  and names the device it should be. Installs that migrated under an earlier
  version are caught up on their first successful read.
- Enrolling now learns the organisation's device keys, so the first sync can
  verify a peer instead of rejecting everything it receives.
- Whether a process is running is no longer judged with `os.kill(pid, 0)`. On
  Windows that reports a process as alive for as long as a handle to it can
  be opened, which outlives the process, so a crashed server would have been
  reported as up for good.
- `server.json` said 0.7.1 while the package was 0.9.3 — four releases of
  drift in the file the MCP registry reads. The same class of bug 0.9.1 fixed
  for `__version__`, in the one place that fix did not reach.

## [0.9.3] - 2026-09-01

### Fixed

- The console listed every Windows machine as `nt`, because enrolment sent
  `os.name`. That value is `nt` on Windows and `posix` everywhere else, so it
  could not tell Linux from macOS at all. It now sends `platform.system()`.

## [0.9.2] - 2026-09-01

### Fixed

- `flanner peer serve` never initialised the database. It printed that it was
  serving while its catch-up thread died on the first query, and a real
  request would have failed the same way. It is the only command that hands
  `get_session` to something else instead of calling it, so it was also the
  only one that never opened the store.
- `flanner join` in a repository flanner had not seen said "run this from
  inside a project" — advice to go elsewhere, when the answer is to adopt
  where you are. It now names `flanner init`, as does `join --help`.
- A refusal over clock skew reported a number and no cause. It now says the
  two machines' clocks disagree, and what to do about it.

### Added

- `flanner doctor` reports enrollment: whether this device is enrolled, the
  state of its entitlement, which workspaces it may enter, and whether this
  repository is bound to one of them. The last check finds a project bound to
  a workspace the account may not enter, which no other command notices.
- `doctor --output json` returns an object with `project`, `catalog` and
  `enrollment` rather than a bare array of catalog findings.

## [0.9.1] - 2026-08-22

### Fixed

- `flanner.__version__` reported `0.7.1`, two releases behind. It was a
  literal that had to be remembered on release, and it had not been. It
  reaches the web UI footer and the settings page, so it was wrong on
  screen rather than merely wrong in principle. It is now read from
  installed package metadata, leaving one source of truth, and two tests
  fail if anybody types it out again.


## [0.9.0] - 2026-08-22

Team sync. Everything below the local plan manager is unchanged: flanner
still runs with no account, no network and no daemon, and everything new
here is opt-in. Plan content is never uploaded — the hosted control plane
holds accounts, devices and access, and nothing else.

### Added

- **Device identity and signed artifacts.** Every plan version, proposal,
  decision and comment is an append-only, content-addressed artifact signed
  by an Ed25519 key that never leaves the machine. A device id is the hash
  of its public key, so nothing assigns it.
- **Peer sync.** `flanner peer serve` and `flanner peer pull <device-id>`
  exchange artifacts directly between machines over iroh, with NAT
  traversal and a relay fallback. No listening port, no VPN and no
  administrator rights. Artifacts are verified against their *author's*
  key, not against the peer that handed them over.
- **Push.** `flanner peer push` sends a peer what it lacks, rather than
  waiting to be asked. Bounded by the sender's workspace role per artifact,
  by size and rate, and refusable outright with `FLANNER_ACCEPT_PUSHES=0`.
  Receiving adds to your history; it never moves your working copy.
- **Catch-up pull** from known peers when the daemon starts, so a machine
  that was asleep does not need to be pushed to.
- **Review.** `flanner review propose`, `decide` and `status` record signed
  proposals and decisions, with an accepted baseline that a synced proposal
  cannot replace and a conflict state when two people accept offline.
- **Comments.** `flanner review comment` attaches a note to a *quotation*
  rather than a line number. A comment whose text has since changed says it
  lost its place instead of sliding onto a sentence nobody commented on.
- **Review packets.** `flanner review pack` writes a self-contained HTML
  file for somebody with no account and no client; `flanner review import`
  reads their notes back in, recorded as received rather than authored.
- **Retiring a plan.** `flanner retire` records a claim that peers hide the
  plan and stop serving it. Deliberately not a deletion: nothing is erased,
  and `--restore` brings it back.
- **Accounts and access.** `flanner login`, `flanner join`, `flanner
  devices` and `flanner whoami`. Entitlements are short-lived, signed, and
  checked offline, so a device keeps working on a train.
- **Workspace roles** — `reader`, `commenter`, `editor`, `maintainer` — now
  enforced rather than advisory, in review, in assurance and on push.
- **Plan assurance and workspace policy**, so an agent can state the exact
  artifact, freshness evidence and approval it relied on.
- **New CLI commands**: `history`, `diff`, `why`, `doctor`.
- **A local daemon** with authenticated IPC, atomic writes and
  cross-process locking, so two MCP clients cannot corrupt shared state.
- **Provider-neutral mesh seam** and a portability conformance suite.

- **The device key moves into the OS keychain.** It falls back to the file
  for an existing install, and generates one only when neither has it.
  Machines with no keychain skip rather than fail.
- **Locks are per plan, not per project.** Two people editing different
  plans in the same project no longer wait for each other. Keyed by plan id
  rather than name, so a rename cannot move a lock out from under whoever
  is holding it.
- **A citation that drifted is told apart from one that was never there.**
  The first is a plan going stale; the second is a reference to something
  outside the repository, and it is not evidence of anything.
- **A solo project now says when an approval binds nobody.** It already
  warned when a joined project could not authorise at all; this is the
  mirror of that check.
- Refusals say which failures belong to the platform and which are
  decisions, instead of wording the same fact two different ways.

### Changed

- **The command line has one look.** A single palette, borderless tables
  and consistent status glyphs across every command, degrading to ASCII on
  a console that cannot encode them rather than crashing.
- **The local web UI is rebuilt**: new shell and stylesheet, self-hosted
  variable fonts, three-state theming, navigation that swaps in place, and
  new Mesh, Review, Freshness and Settings pages.
- **Sync is no longer pull-only**, so documentation that described it that
  way has been corrected.
- Settings now reports what this device is holding, and states plainly that
  flanner never prunes.

### Fixed

- Saving a plan from the browser created an identical new version every
  time, because textareas submit CRLF and the comparison hashed raw bytes.
- The projects list ignored its own sort control.
- Filtered table rows stayed visible: `[hidden]` lost to `display: grid`.
- A filled-circle glyph crashed the CLI on a Windows console still running
  cp1252.
- Several stylesheet rules existed only inside the mobile media query, so
  command blocks, filter controls, footnotes and notices rendered unstyled
  on a wide screen.
- The wheel shipped without its stylesheets, because `package-data` did not
  include `static/`.


## [0.8.0] - 2026-08-05

### Added
- Plan freshness: evidence-based drift detection. Every plan version gets a
  status (`fresh | aging | suspect | stale`) derived from checkable evidence:
  the paths and symbols it cites, whether those still exist in the repo, an
  anchor commit resolved from the version's authored time, and how many
  commits touched the cited files since. Nothing is stored; git access is
  read-only and fails open (no git degrades to age-only judgment).
- `flanner freshness [PLAN_NAME]` CLI command: status table for all plans, a
  full evidence breakdown for one plan, and `--output json` for scripting.
- `get_plan_freshness_tool` MCP tool so agents can check whether a plan is
  still likely true before trusting it.

## [0.7.1] - 2026-07-10

### Added
- Brand favicon (inline SVG, three-rule document mark) and `theme-color` meta for
  the light and dark palettes, so the browser chrome matches the page.
- Craft details: selection uses the accent wash, scrollbars are theme-aware,
  numeric data (counts, versions, dates) uses tabular figures, and a print
  stylesheet renders a plan as a clean document (drops the app chrome).
- Keyboard-shortcuts help sheet: press `?` (outside a text field) for a native
  dialog listing the shortcuts.
- Snappier navigation: internal links are prefetched on hover, and supporting
  browsers get a smooth cross-page transition (disabled under reduced motion).
- Inline duplicate-name check on the new-project and new-plan forms: typing a
  name that is already taken warns immediately (reusing the search index)
  instead of waiting for the server to reject the submit.
- Filter and sort on the projects list and a project's plan list: a search box
  narrows the visible rows and a sort control orders by name, created, or last
  updated. Client-side over the loaded page (global search is the palette).
- Command palette (Ctrl/Cmd+K, or the nav "Search" button): a native `<dialog>`
  that fuzzy-filters every project and plan and jumps to it. Arrow keys move the
  selection, Enter opens, Esc closes; the index is served by a new `/api/search`
  endpoint. Focus trap, Esc, and backdrop dismissal come from the native dialog,
  so it adds no library.
- Web UI design tokens: a 4px-based spacing scale, three elevation tiers, and
  motion tokens, so spacing and shadows are systematic rather than ad hoc.
- A real toast component for client-side notifications (bottom-right, aria-live,
  per-status left accent bar, dismiss button, reduced-motion aware), replacing
  the previously unstyled notification and built entirely on the tokens.

### Changed
- Static assets are now stamped `version-<mtime>` (newest file under `static/`)
  so an edit-and-restart busts the browser cache even within a release;
  previously the tag was the version alone, so mid-release CSS/JS edits could be
  served stale.
- The dashboard's third stat is now "Updated this week" (plans touched in the
  last 7 days), a real signal, instead of the length of the recent-activity list
  (which was capped at 10 and so plateaued as a vanity number).

### Security
- Rendered plan markdown is sanitized (nh3) before being inserted with `|safe`,
  stripping `<script>`, event handlers, and `javascript:` URLs while keeping the
  formatting and code-highlight markup. Adds the `nh3` dependency.

### Fixed
- Web UI review pass:
  - Mobile: the projects grid no longer forces horizontal page scroll (its
    `minmax` minimum exceeded the viewport), and rendered markdown tables scroll
    within their own box instead of the page.
  - Short form fields (version notes, descriptions) no longer stretch to 320px;
    only the main content editor is tall.
  - Reading presets (Book/Night) now style code, tables, and quotes consistently
    regardless of the OS light/dark theme (they set a full local palette).
  - Dark mode: the "Disabled" badge and the reading-settings popover shadow are
    theme-aware instead of hardcoded light values.
  - Consistency: info/version grids lay out in even columns; card padding is
    uniform; the first markdown heading no longer gets a stray top gap; the
    history file-path is a plain code span, not a dead link.
  - A11y: reading-settings groups are `role="group"` and labelled, and the
    version selector has a real `<label>`. The reading popover now closes on Esc
    (returning focus to its button) and its segmented controls move with the
    arrow keys.
  - No layout shift when the plan editor upgrades: the plain textarea reserves
    the same height (60vh) as the CodeMirror that mounts over it.
  - Mobile: dashboard stats stack (no orphaned third card) and small action
    buttons get a 44px touch target.
  - Cosmetic/cleanup: empty project dates show `-` consistently; the recent-
    activity stat is relabelled; dead `.form-card` and duplicate form-input CSS
    removed.
  - Plan editor: the Version Information card was nested inside the form card
    with its top border flush against the Save button; it is now a separate
    section below the form, so the button no longer looks joined to it.

## [0.7.0] - 2026-07-10

### Added
- Web plan editor upgraded to a real code editor (vendored CodeMirror 5, no
  build step, fully offline): line numbers, markdown syntax highlighting,
  active-line, and list continuation, themed to match the light/dark palette.
  It mounts over the existing textarea as progressive enhancement, so editing
  still works with JavaScript disabled and the form contract is unchanged.
- Reading customization on the plan viewer: an "Aa" popover to choose a preset
  (Default / Book / Night / Plain), font, size, and width. Presentation-only and
  client-side (CSS variables + `data-*` attributes persisted to localStorage),
  so the server keeps caching one canonical HTML and the render cache is never
  invalidated per preference.
- Self-adoption for new projects. `flanner setup` (one-time, global) registers
  the MCP server for Claude Desktop and Claude Code (user scope) and adds a
  narrow nudge to `~/.claude/CLAUDE.md`, so Claude offers to adopt a repo when
  you write a plan doc in a project that is not yet flanner-managed. A new
  `initialize_project_tool` lets the agent do the adoption itself (create the
  project and install the CLAUDE.md/AGENTS.md block, guard hook, skill, and
  `.mcp.json`) without leaving the chat.
- Plans can live in subdirectories of the plan directory. A plan name may be a
  subpath (`auth/login-flow` -> `.plans/auth/login-flow_v1.md`); parent
  directories are created, `sync` discovers nested plans, and the guard hook
  still protects them. Names are sanitized per segment and path traversal
  (`..`) is rejected.

### Changed
- Static assets (CSS/JS) are version-stamped (`?v=<version>`) so a released
  upgrade busts the browser cache instead of serving stale files.
- `flanner web` checks the port first and, if it is taken, prints an actionable
  message (how to pick another port / set `FLANNER_WEB_PORT`) and exits 1,
  instead of letting a raw bind error scroll past. `--open-browser` now opens
  once the server is actually accepting connections, on a background thread, so
  it never delays startup.

### Fixed
- Editing a plan in the web UI no longer corrupts its line endings. Browser
  forms submit CRLF; the file was written in text mode on Windows, doubling the
  carriage returns (`\r\r\n`) and gaining a blank line on every save. Plans are
  now normalized to LF and written without OS newline translation, and the body
  is normalized before hashing so an unchanged plan is not seen as modified.
- `flanner init` now also registers the MCP server with **Claude Code** (the
  CLI) by writing a project `.mcp.json`, not only Claude Desktop. Claude Code
  reads `.mcp.json`, so previously CLI users ran `init` and never saw the flanner
  tools under `/mcp`. Existing entries in `.mcp.json` are preserved; the portable
  `flanner-mcp` command is used so the file is shareable across a team.
- Web `--port` is typed as an integer; a CLI-provided port previously arrived as
  a string, which the port check would have crashed on.
- Button heights are consistent: `<button class="btn">` used the browser default
  line-height while `<a class="btn">` inherited the body's, so buttons rendered
  shorter than link-styled buttons.

## [0.6.0] - 2026-07-10

### Added
- Linear integration. Link plan files to Linear issues (`ENG-123`) via a
  `flanner linear` CLI group and matching MCP tools, mirroring the JIRA link
  surface:
  - Link-only by default: stores the issue id and builds a `linear.app` URL, no
    network or credentials.
  - API sync when `LINEAR_API_KEY` is set: `link` verifies the issue exists and
    caches its title/state, `--attach-url` attaches a URL to the issue, and
    `flanner linear refresh` re-pulls live status. The key is read from the
    environment only, never stored on disk. The GraphQL client uses the standard
    library, so it adds no runtime dependency.
  - `flanner linear auth` validates the key against Linear and prints the MCP
    server config snippet (with `LINEAR_API_KEY` in its `env`) so the agent's
    server process gets the same access.
  - Web UI: the plan viewer shows a "Linked Linear issues" panel (id, cached
    state, title, link), and the project page marks linked plans with a
    `Linear ×N` badge.
  - See docs/LINEAR_INTEGRATION.md.

## [0.5.0] - 2026-07-10

### Added
- `flanner-mcp` console script to run the MCP server (equivalent to
  `python -m flanner.server`); cleaner for client configs and registry listings
- `server.json` manifest for submitting to the MCP registry

### Changed
- `flanner init` and `flanner start` register/print the MCP server config with
  the absolute interpreter path (`sys.executable -m flanner.server`) instead of
  a bare command, so the client app spawns it regardless of its PATH (venv/pipx
  installs are not on the GUI app's PATH)

## [0.4.1] - 2026-07-09

### Fixed
- PyPI project page showed `pip install -e .`; the rendered description now uses
  `pip install flanner` (0.4.0 was built before the README install line was updated).

### Added
- README explains the skill and guard-hook enforcement layer on top of the MCP tools.

## [0.4.0] - 2026-07-09

### Added
- Schema migration runner: a versioned MIGRATIONS registry upgrades an existing
  database in-place when SCHEMA_VERSION rises, instead of only stamping the
  version (see docs/adr/0003-schema-migrations.md)

### Changed
- Extracted the shared plan write-path (frontmatter, filename, save, hash,
  version record) into flanner/plan_ops.write_version; the MCP server and web
  UI both call it so the four create/update copies cannot drift

### Fixed
- Bump pytest to >=9.0.3 so pip-audit passes (PYSEC-2026-1845)
- Replace deprecated datetime.utcnow() with a naive-UTC helper
- Refresh the stale docs/INSTALLATION.md

### Security
- flanner web warns when binding a non-local host (the web UI has no auth)

## [0.3.0] - 2026-07-09

### Added
- Claude Code agent integration so plan files reliably land in the managed
  directory with the standard header, without the user reminding Claude:
  - `flanner hook guard-write`: a PreToolUse hook that denies raw Writes into
    a project's plan directory and steers Claude to create_plan_file_tool
    (fails open; the MCP tools write outside the Write tool so they are never
    blocked)
  - `flanner init` now writes a managed block into CLAUDE.md and AGENTS.md
    (the cross-tool file Codex reads), merges the guard-write hook into
    .claude/settings.json, and installs a flanner-plan skill

## [0.2.0] - 2026-07-08

### Added
- Web UI redesigned: drafting-paper light / blueprint-night dark mode
  (prefers-color-scheme), monospace chrome around a serif reading column,
  path breadcrumbs on every page, empty-state illustrations, fluid type
  scale for wide displays; fully offline (CDN dependencies removed)
- Footer credit linking to the author's GitHub
- Pagination on the projects list and project detail pages (50 per page)
- MCP: list_plan_files_tool pages results (default 50, cap 200); get_plan_file_tool
  truncates content past max_chars (default 100k) with truncated/total_chars fields
- Plans past 1M characters are served as plain text instead of rendered markdown
- Styled HTML error pages (400/404/500) for browser routes; /api/* keeps JSON;
  unhandled exceptions log the traceback and never leak it to the page
- Flash messages: project deletion confirms with a success banner; saving a plan
  with unchanged content explains why no new version was created

### Changed
- Markdown rendering runs off the event loop and is cached by content hash;
  a multi-megabyte plan no longer freezes the server for all clients (8.7s -> 57ms)
- Dashboard stats computed in SQL instead of loading every plan file (fixes N+1)

### Fixed
- MCP server startup never initialized the database, so every DB-backed tool
  failed in a fresh server process (added end-to-end stdio regression test)
- 'list --project X --output json' printed a table instead of JSON
- Emoji in CLI output crashed cp1252 Windows consoles
- API returned 200 with an empty list for a nonexistent project's plans (now 404)

## [0.1.0] - 2026-07-07

### Added
- `flanner --version`, `--verbose`/`--quiet` global flags
- `flanner list --output json` for machine-readable output
- Configuration via environment variables: `FLANNER_HOME`, `FLANNER_DB_PATH`, `FLANNER_WEB_PORT`
- Structured exception hierarchy (`FlannerError` and subclasses)
- Schema version stamping (`PRAGMA user_version`) for future migrations
- Reproducible benchmark (`benchmarks/bench.py`) with numbers in the README
- Pytest suite with an enforced import-boundary test; CI on Python 3.10/3.12
- MIT license

### Changed
- Package restructured: `src/` is now the installable `flanner` package with
  web assets inside it; `setup.py`/`requirements.txt` replaced by `pyproject.toml`
- Version reset to 0.1.0 (1.0.0 was never released)
- CLI error paths exit with code 1 (previously 0); exit codes: 0 success, 1 error
- Web UI binds 127.0.0.1 by default (previously 0.0.0.0)
- Library modules log via `logging` instead of printing

### Fixed
- Web routes crashed on current Starlette (old `TemplateResponse` signature)
- SQLite connection-pool exhaustion after ~15 rapid MCP tool calls (NullPool)
- Database writes now roll back on failure instead of leaving the session dirty
- `sync` reported wrong old version in its update messages
