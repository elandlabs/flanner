# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **The web UI sets up what the CLI sets up.**
  - **Settings.** Agents now come from the same check `flanner status` runs, so the page no longer calls a Claude Code user unregistered.
  - **Setup check page.** A new page shows agents, tools, the project, capture mode, watched agents and peers. Capture mode can be changed there, writing the same policy file `flanner mem mode` writes.
  - **Registering agents.** Claude Desktop and Codex can be registered from the page. It shows the exact diff first, and a file that changed since the preview is refused. The result is byte for byte what `flanner register` or `flanner setup` leaves.
  - **Terminal-only actions.** Actions the page cannot take, such as enrolling a device or joining a workspace, are shown disabled, with the reason and the command.
  - **Team card.** Settings links to the console this device signed in to, for members, invitations, devices, workspaces and billing.
  - **Sharing and sync.** A project page shows its workspace and your role there, and whether this device accepts pushes.
- **`flanner peer pushes on|off`** replaces the `FLANNER_ACCEPT_PUSHES` environment variable as the everyday switch. The project page flips the same setting. The variable still works and, when set, decides.
- **You can see whether an agent has actually reached flanner.** "Registered" only meant a config file named flanner. Settings, the setup check and `flanner status` now show "last call 2 minutes ago", or "not yet used" with a prompt to try: ask the agent to list your flanner projects. `flanner init` ends on the same prompt.
- **The dashboard opens with what needs you.** Agent requests, memory suggestions, review items and agents that have not connected replace the project, plan and weekly counts.
- **One action history.** Every write through the CLI, the web UI or an agent
  is recorded with an id, where it came from, and the person it was for.
  `flanner actions list` and the Actions page show the same record, and an
  agent's reply carries the id. Ids and closed vocabularies are kept; a plan
  body, a memory or a piece of evidence never is.
  Commands and pages that write without the service layer, such as a skill
  rollback, are recorded too, by the command or route that made the change.
- **Agents can ask for risky actions, and only ask.** `request_action` covers
  installing, rolling back, sharing and importing a skill, and restoring a
  memory. It stores a preview and changes nothing. You apply or decline it
  with `flanner actions apply` or on the Actions page, and it is refused as
  stale if what the preview described has changed since.
- **Agents can draft skills for review.** `skills_submit_evidence`,
  `skills_propose` and `skills_revise` create evidence and drafts. Evidence is
  labelled as the agent's own account, and no tool approves or installs.
- **One setup check.** `flanner status` now also shows the tools advertised,
  the project, capture mode, whose skill use is watched, and peers.
  `project_context` gives an agent the same check.
- **A harness for automatic tool use**, in `benchmarks/agent_tool_use`. It
  scores recall, capture judgment, quietness, choosing the right project,
  self-approval and recovery for Claude Code and Codex, from the calls
  flanner logged. Both hosts pass all seven, three runs out of three.

- **`flanner init` asks whether to record which skills your agents use**, and
  Enter means yes. Opt-in made the feature useless: the counts only mean
  something over a period, so a switch nobody knew about got turned on the day
  somebody wanted an answer, and then read zero for a month. It is a question
  rather than a default because the other hook init installs protects your
  files and this one records what you do. An unattended install records
  nothing — pressing Enter is a person agreeing, and an empty pipe is not.
  `--watch-skills` and `--no-watch-skills` answer it without a prompt, and
  `flanner skills observe disable` stops it at any time.
- **The skills page is tabbed**: Skills, Usage, Versions, Team and Health,
  instead of six cards stacked under the table. Every panel is still rendered,
  so with JavaScript off the tab strip is a row of jump links and nothing is
  out of reach.
- **Every skill has a page of its own.** Click a name in the local web UI and
  you get every copy of it and which one loads, what is wrong with it, a diff
  against each shadowed copy — the question "shadowed by what, exactly?" had
  no answer before — the manifest rendered, the files its hash covers, how
  often it was used and over what window, the snapshots flanner holds, and
  what has arrived from a teammate. From there you can keep a copy, roll one
  back, send one to your workspace, install one that arrived, or follow the
  skill for updates.
- **Codex skills are read too, and every skill says which agent it belongs
  to.** `.agents/skills` in the repository, in your home directory, and the
  machine-wide directory an administrator deploys. Codex does not merge two
  skills that share a name — its own docs say both can be offered — so
  flanner reports every copy as loaded rather than inventing a winner, and
  says plainly that nothing on disk decides which one runs. A name held by
  both agents is two skills, not a collision. `--agent` narrows a listing to
  one; every agent is the default.
- **Every list in the web UI is paged.** Projects, plans, freshness, memory,
  skills and review show fifteen rows at a time, with Previous and Next and
  a rows-per-page menu of 15, 30, 50 or 100. The choice is remembered, and
  a page fetches only the rows it shows. The freshness table, which fills in
  as each plan is judged, pages in the browser as rows arrive.
- **`--limit` on the listing commands.** `list`, `mem list`, `mem pending`,
  `skills list`, `skills installs` and `skills evidence list` show fifty rows
  unless told otherwise (`--limit 0` for everything) and say when the list
  was cut. A table taller than the terminal is shown through the system
  pager when stdout is a terminal; piped output is unchanged.

### Changed
- The Linear and Jira page is now called Issue trackers. The old name clashed with the agent integration settings.
- **Evaluations say what they are.** `flanner skills eval` stores comparison
  results that somebody else produced, and every report now says flanner ran
  none of them. Running comparisons is on the roadmap.

### Security
- **Applying what an agent asked for takes more than typing the command.**
  Signed in, flanner actions apply waits for a console confirmation bound to
  that action and its preview. The web UI never applies one: it shows the
  command to run, and still lets you decline, which grants nothing. Without
  an account, applying refuses inside a shell an agent host started, and
  otherwise asks for the action's code to be typed at a terminal. A
  determined agent can unset a variable and allocate a terminal, so that is
  a hurdle rather than proof; an account is the proof on offer.
- **A teammate's review counts, and only as the person who signed it.** A
  device knew its own role and nobody else's, so every proposal and approval
  a teammate made was dropped. It now reads a roster the control plane signs
  with each entitlement. An event also has to be signed by a device the
  roster gives to the person it names, so one person's device cannot act in
  another's name. A session cached before rosters existed renews once.
- **Approving in a team workspace is confirmed in the console.** Nothing on a
  machine can show that a person ran a command rather than an agent with a
  shell. `flanner review decide approve` now prints a link and a code, and
  records the approval once you confirm it in a browser signed in as you.
  Peers refuse an approval without that signed confirmation.
- **Nobody approves their own proposal while somebody else could.** Team
  workspaces use a policy that refuses self-approval unless no other
  maintainer exists. Solo review is unchanged.
- **A removed teammate stops being served within a day, not a week.** A
  device serving plans now requires the caller's device to be on the signed
  roster it holds, and requires both that roster and the caller's
  entitlement to be current rather than in their offline grace period. A
  device whose own entitlement has lapsed renews once and otherwise serves
  nobody. An unknown caller triggers one rate-limited renewal first, so a
  new teammate is not turned away. A device's own reads of what it already
  holds keep the grace period.

### Fixed
- **Entitlements renew on use.** Nothing renewed one except
  `flanner whoami --refresh`, so a device that only synced lost push after a
  day and everything else after the grace period. `peer pull`, `peer push`
  and `peer serve` now renew when needed, and a renewal also fetches the
  organization's device keys instead of keeping the old ones. Offline, the
  cached entitlement is used as before.
- **Agents offer a settled decision for approval.** Codex saved decisions nobody asked it to keep, and Claude Code acknowledged them without offering anything. The tool descriptions, the managed instructions and the memory skill now say which tool is for what, and when. Both hosts then passed both capture checks three times out of three.
- **A form that fails keeps what you typed.** Two new-project errors came back empty, a plan edit whose save failed lost the edit, and a refused skill draft revision dropped the edited body.
- A project's plan list counted hidden plans out and listed them anyway; the
  count and the rows now agree.
- **`remember` is refused when capture is off**, from the CLI and from the
  agent's tool. The policy file always said so; only suggestions honoured it.

## [0.12.0] - 2026-09-09

### Added
- **Skills: see what your agents actually load.** A skill is a directory
  holding a `SKILL.md`, and they arrive from three places at once — the
  project, your home directory, and every installed plugin. Nothing on the
  machine could say which copy an agent loads when two share a name.
  `flanner skills scan` reads them all and `flanner skills doctor` says
  what is wrong: a frontmatter name that disagrees with its directory,
  copies that differ and shadow each other, a plugin revision the agent no
  longer lists. On the machine this was built against, 110 packages were
  installed and 57 were in effect. It is a read: packages stay where their
  owner put them, and nothing inside one is executed by a scan.
- **Watching which skills get used, if you turn it on.** Off until you do,
  per agent and per repository. What is recorded is that a named skill was
  invoked, when, and by which local session — not your prompts, not the
  agent's replies. Only explicit invocations are visible: Claude Code shows
  every skill's description to the model without reporting it, so a "loads"
  number would be invented and there is not one. Every report carries the
  window it covers and whether anything was watching during it, because
  zero uses and zero coverage are different facts.
- **Changing a skill without breaking it.** `flanner skills adopt` keeps a
  copy where flanner can put it back; an install snapshots whatever it
  replaces, so `flanner skills rollback` always has something to restore. A
  directory flanner did not install, or one edited by hand since, is
  refused rather than overwritten.
- **Proposing a skill from work you hand over.** Nothing is harvested and
  no conversation history is read; evidence exists because somebody
  submitted it. Three related pieces across two sessions with something
  saying the work succeeded opens a proposal — a product default, not a
  discovered threshold, and the wording says so. An approval covers the
  exact draft that was read: editing it afterwards sends it back for
  another look rather than shipping the edit under the old approval.
- **Comparing a candidate against a baseline.** Recorded, never run:
  nothing here calls a model provider. The matrix walks the full grid, so a
  combination nobody ran reads as not run rather than as a zero, and every
  cell names its fixture, its model and its harness — an endpoint result
  says nothing about behaviour inside an agent.
- **Sending a skill to a teammate.** `flanner skills share` signs the
  package files and nothing else: no recorded uses, no evidence, no session
  references. Receiving is not installing — a package arrives as a transfer
  and waits. A package that does not hash to what its sender claimed, or
  one built for another agent, is refused. Following a skill tells you
  about a new version and never installs one. Needs a `skill_sync`
  entitlement, which an organization admin can switch off for everybody.
- **Memory: durable context, as Markdown files you can read.** A plan says
  what you decided to build; memory is the smaller, longer-lived stuff
  around it — the constraint that rules out an approach, the reason a
  library was rejected, the fact that the staging cluster is rebuilt on
  Sundays. `flanner mem remember` writes one, `flanner mem recall` searches
  them and says why each result matched, and a later session finds them
  without being told. Each memory is a file under `.flanner/memory/`; the
  database is an index over those files, so `flanner mem rebuild` reads
  everything back if it is lost.
- **Capture modes, because a tool that stores what it likes is not one you
  keep.** `off`, `explicit` (the default), `suggest` and `auto-safe`. In
  `suggest`, an agent proposes and `flanner mem pending` shows what is
  waiting on you. A project's `.flanner/memory.yml` may only tighten what
  the machine-wide policy allows, never loosen it.
- **Nothing that looks like a credential is stored.** Fifteen vendor
  patterns, a generic assignment catch and an entropy check, run on
  anything written here — your own writing, an agent's suggestion, and
  anything a teammate sends. A peer's signature proves who wrote something;
  it does not make this device store it.
- **Attachments.** A screenshot, a PDF or a recording kept beside a memory
  as evidence, stored by content hash outside the database so the same file
  attached twice is stored once.
- **Sharing, one memory at a time.** `flanner mem share` signs a memory into
  the workspace its project joined; `flanner mem withdraw` asks peers to
  stop recalling it. Joining a workspace shares nothing on its own, and
  personal memory can never be shared at all. A withdrawal is a request
  peers honour, not an erasure: a device that was switched off already
  holds the text, and no design without a central copy can change that.
  Needs a `mem_sync` entitlement, which an organization admin can switch
  off for everybody.

### Fixed
- The memory list's grid was never defined in the stylesheet, so every row
  collapsed to one column and each memory appeared to be listed twice.
- A long path in a table held its column open and squeezed the flexible
  column beside it to nothing, which is why skill descriptions rendered as
  a few stray pixels.

## [0.11.0] - 2026-09-05

### Changed
- **`flanner init` registers the agents itself.** This was a separate
  `flanner setup`, and a machine that never ran it had a store no agent
  could reach. The local MCP server is not a trimming you opt into later:
  it is how an agent talks to flanner at all, so the command that creates
  the store now wires it up. `setup` still exists for repairing the
  registration on its own, and `--skip-claude` now opts out of all of it
  rather than only the Claude Desktop half.
- **`flanner init --setup` picks which agents that means**, and repeats:
  `--setup codex` registers Codex and nothing else. One option rather than
  a flag per agent, because a flag per agent cannot say whether naming one
  means that agent instead of the default or as well as it. The choices are
  the same three rows `flanner status` reports.
- **`flanner init --sync` imports the plan files already in the repository.**
  Adopting a repository somebody else set up and then listing none of its
  plans reads as flanner having lost them. The import keys on each plan's
  own id and attaches it to the local project, so a clone carrying another
  machine's ids works.
- **`flanner mesh join` is now `flanner mesh connect`.** It shared a word
  with `flanner join` and nothing else. That one binds a repository to a
  workspace, which is what makes review count and what peer sync is scoped
  by. This one puts the machine on a VPN and touches nothing flanner owns.
  Most teams need the first and never need the second.

### Fixed
- `flanner login` creates this machine's store, as `flanner accept` already
  did. The two enrol a device identically, so leaving only one of them to
  create the store made the very next instruction either work or refuse,
  depending on which command you had been sent to.

### Added
- **`flanner peer start` and `flanner peer stop`**, so serving does not mean
  keeping a terminal open. Serving had quietly become one person's job, and
  nothing in the protocol says it should be: any device holding a role in the
  workspace may answer, and several may answer at once. The only real
  constraint was uptime. `flanner peer status` now also says whether this
  device is actually serving, because being reachable and answering are
  different things and it reported only the first.
- **Work arriving from a peer shows up without a reload.** The catalog has
  several writers and they are separate processes: the web UI, `flanner peer
  serve` taking a push, the MCP server acting for an agent, a `flanner sync`
  in a terminal. None share a call stack with the page you are looking at, so
  none could notify it, and a version from a teammate stayed invisible until
  you happened to reload. The server now watches the catalog instead of
  waiting to be told, and reports what moved over server-sent events.
  Counting versions is part of that signature, because a version arriving
  from a peer deliberately does not move the current-version pointer -- the
  rule that stops a teammate changing what you have open -- and watching the
  pointer alone would have been blind to exactly the event this reports.
  A plan page offers a link rather than swapping itself, since it may be
  half-read; the dashboard, projects, project detail, plans list, plan
  history and mesh pages re-render; the editor is deliberately excluded and
  never re-renders under somebody typing.

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
