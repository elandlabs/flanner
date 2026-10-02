# Flanner - Quick Reference Card

## Installation

```bash
# Install the flanner command (Python 3.10 or later)
uv tool install flanner

# Or, without uv
pipx install flanner
pip install flanner
```

Both `flanner` and `flanner-mcp` end up on your PATH.

## Core Commands

```bash
# Adopt the repository you are in, and register with your agents
flanner init

# Check status: the MCP server, the catalog, and one row per agent
flanner status

# List projects
flanner list

# List plan files for a project
flanner list --project PROJECT_NAME
```

## Plans

Agents create and revise plans through the MCP tools; a person uses the web
interface. There is no CLI command that writes a plan.

```bash
# Every version of a plan, who wrote it, and when
flanner history PLAN_NAME

# What changed between two versions (defaults to the last two)
flanner diff PLAN_NAME 2 3

# Whether a plan still matches the code, with the evidence
flanner freshness
flanner freshness PLAN_NAME
flanner why PLAN_NAME

# Re-import plan files that already carry a flanner header,
# for example after restoring .plans/ from a backup
flanner sync
flanner sync --dry-run
flanner sync --project PROJECT_NAME
```

`flanner sync` skips any Markdown file without a flanner header. To add a
plan of your own, ask your agent to save it, or paste it into the web
interface.

## What your agents can reach

```bash
# Every Claude Code and Codex launch: credentials, files, network
flanner curb map

# One launch, exactly as you run it
flanner curb map -- claude --settings ./ci-settings.json
flanner curb map -- codex exec -s workspace-write

# Names and locations, in a window on your screen
flanner curb show

# Agents, settings layers, MCP servers, hooks, skills and scheduled jobs
flanner curb inventory

# Secrets agents left behind, by exposure class (pip install 'flanner[sweep]')
flanner curb sweep
flanner curb sweep --validate     # ask each secret's issuer if it works
flanner curb show --sweep         # types and locations, in a window

# Close what each agent can reach, in its own user settings
flanner curb fix --dry-run        # the fixes, changing nothing
flanner curb fix                  # apply them after your operating system asks you
flanner curb fix --undo           # put the files back

# Prove each block: decoys, and the agent asked to read them four ways
flanner curb test                 # states the token cost and asks first
flanner curb decoys               # how many, and when they expire
flanner curb decoys --remove

# Log each tool call's metadata (agent, tool, channel, decision), signed
flanner curb log --enable         # installs the agents' hooks, after your OS says yes
flanner curb log --verify         # did anyone change a record?
flanner curb observed             # seen in use, per channel, after 14 days and 20 sessions

# After rotating a secret, replace it in a file the sweep found (no backup, no undo)
flanner curb scrub FILE --dry-run
flanner curb scrub FILE

# Delete Curb's stored reports and its digest key
flanner curb forget

# Your organization's agent policy (Flanner Mesh)
flanner curb policy               # the policy here, what waits for you, what is not met
flanner curb policy --enrol       # let signed policy make changes that only tighten (asks once)
flanner curb policy --check-in    # fetch the newest policy now; also runs at session start
flanner curb policy --approve     # review and apply a change that waits for you
flanner curb policy --withdraw    # later changes wait for your approval
flanner curb policy --export mdm  # admin-owned settings files for device management

# Admins: every device, each report checked against its own key
flanner curb fleet

# Agent steps in this repository's GitHub Actions workflows
flanner curb ci                   # who can start each, what reaches it, what it holds
flanner curb ci --sarif curb.sarif --fail-on high
flanner curb ci --fix             # the one-line fixes that are safe to make blind

# LLM calls in an application's Python code: single call, tool-using or loop
flanner curb app --sarif app.sarif
```

In CI, use the action: `uses: elandlabs/flanner/actions/curb-ci@<tag>`
(see `actions/curb-ci/README.md`). `flanner init` also installs the
`agent-blast-radius` skill, which runs the redacted commands for an agent.

The terminal never shows a credential's name or location, for anyone.
Without an account nothing leaves the machine. With Flanner Mesh, devices
send only the minimised, signed fields in
[curb-wire-contract.md](curb-wire-contract.md). The tested agent versions
are in [curb-support-matrix.md](curb-support-matrix.md).

## Web Interface

```bash
# Launch web UI (default port 8080)
flanner web

# Launch and auto-open browser
flanner web --open-browser

# Custom port
flanner web --port 3000
```

## Agent Integration

`flanner init` registers flanner with Claude Code, Claude Desktop and Codex,
and writes the agent guidance, the guard hook and two skills into the
repository. These repair it later:

```bash
# One row per agent, each checked where that agent looks
flanner status

# Re-run the registration with every agent
flanner setup

# Claude Desktop only: its config file
flanner claude-info
flanner register
flanner register --force
flanner unregister
```

## Project Configuration

```bash
# Configure project settings
flanner config PROJECT_NAME --plan-dir docs/plans

# Update .gitignore manually
flanner setup-gitignore PROJECT_NAME
```

## MCP Server

Most clients spawn their own copy over stdio and need none of this. Use it
for a client that only speaks http, for two editors sharing one server, or
for working with flanner on its own.

```bash
# Run it in the background on 127.0.0.1:8765 (env: FLANNER_MCP_PORT)
flanner start
flanner start --port 9000

# Is it up, and on what pid
flanner status

# Stop it
flanner stop
```

It listens on loopback only, and no option widens that. Every tool acts with
the full authority of whoever started the server and nothing authenticates a
caller, so this is a local convenience rather than a service to expose.
Output goes to `~/.flanner/server.log`.

## Messages Between Teammates

Needs a Team Mesh plan with messaging switched on. Messages go device to
device; no server holds them. A **handle** is a teammate's short name, like
`ben`. It comes from their email and cannot be changed.

```bash
# Read
flanner messages inbox                    # unread threads
flanner messages inbox --all              # read ones too (--json for scripts)
flanner messages read 7f3a                # one thread, with delivery for what you sent

# Send (always shows who it goes to and asks first)
flanner messages send ben "can you look at the migration plan?"
flanner messages send ben chen "deploying billing at 15:00"
flanner messages send ben "..." --yes     # skip the question, for scripts
flanner messages reply 7f3a "next release"
flanner messages broadcast "heads up, deploying in ten minutes"   # this repo's workspace
flanner messages broadcast --workspace WORKSPACE_ID "..."         # always asks; no --yes

# Stay undisturbed (this device only)
flanner messages mute chen                # until you unmute
flanner messages mute chen --for 8h       # or 30m, 1d
flanner messages mute chen --off
flanner messages quiet-hours              # show them
flanner messages quiet-hours 22:00-07:00  # every day, local time
flanner messages quiet-hours off
flanner messages notifications off        # no desktop notifications

# How agents show new messages
flanner messages interrupt                # show the current choice
flanner messages interrupt tool           # between tool calls and at your next prompt (default)
flanner messages interrupt prompt         # at your next prompt only
flanner messages interrupt channel        # the moment they arrive, in Claude Code started with the channel

# Follow along
flanner messages watch                    # print messages as they arrive; Ctrl+C stops
flanner messages wait --timeout 600       # print the next message and exit (for agents)

# Receive even after a reboot
flanner peer autostart                # is this device receiving, and at login?
flanner peer autostart on             # receive now, and at every login
flanner peer autostart off
```

A muted sender's messages still arrive and are listed; they never
interrupt. Quiet hours work the same way for everybody. The sender is told
neither. Messages are plain text, up to 4 KB, to up to 20 people. Your
organization's admin sets how long they are kept: 30, 90 (the default),
180 or 365 days. `flanner status` shows unread and queued messages, and
warns when nothing on this device is receiving.

Desktop notifications name the sender, never the message. They are on by
default; `flanner messages notifications off` turns them off on this
device.

## Common Workflows

### First Time Setup
```bash
cd your-project
flanner init
# Creates ~/.flanner/data.db
# Registers the MCP server with Claude Code, Claude Desktop and Codex
# Creates .plans/ and adds it to .gitignore
# Writes the guidance block into CLAUDE.md and AGENTS.md, the guard hook,
# .mcp.json, and the flanner-plan and flanner-memory skills
```

### Creating a Plan

**Through your agent (recommended):**
```
Ask Claude or Codex: "Save this plan with flanner"
```

The agent calls `create_plan_file_tool`, which writes `.plans/NAME_v1.md`
with its header. In Claude Code, the guard hook refuses a direct write into
`.plans/`, so a plan cannot skip the versioning.

**By hand:** open `flanner web`, choose the project, and use New plan.

### Revising a Plan

Ask the agent to revise it, or edit it in the web interface. Each revision
is saved as a new version in its own file, such as `NAME_v2.md`; nothing is
edited in place, and nobody increments a version by hand.

```bash
flanner history PLAN_NAME    # every version
flanner diff PLAN_NAME       # the last two, compared
```

### Viewing Plans

**Web Interface:**
```bash
flanner web --open-browser
# Navigate to: Projects → Select Project → View Plans
```

**Via Claude:**
```
Ask Claude: "Show me all plan files"
Ask Claude: "Show version history for architecture plan"
```

## Directory Structure

```
~/.flanner/                  # Flanner data directory
├── data.db                  # SQLite catalog and search index
├── server.log               # MCP server output
└── memory/personal/         # Your personal memories

your-project/
├── .gitignore               # .plans/ and .flanner/memory/ added
├── .plans/                  # Plan files, one per version (git-ignored)
│   ├── architecture_v1.md
│   ├── architecture_v2.md
│   └── api-design_v1.md
├── .flanner/memory/         # Project memories (git-ignored on first save)
└── ...
```

## Plan File Format

The tools write the header; never write it by hand.

```yaml
---
mcp_plan_file: true
plan_manager_version: '1.0'
project_id: 3d816ecd-489a-4fa0-abe2-15ec93f60d5a
project_name: my-app
plan_file_id: 59c34f9c-8471-47fc-97f2-8dcfefa15434
plan_name: architecture
version: 2
created_at: '2026-01-15T10:30:00.000000Z'
created_by: claude
artifact_id: sha256:6f3e9b1c07a24d58b1e0c9f4a2d7e8b3c5f1a0d9e8c7b6a5f4e3d2c1b0a9f8e7
parents:
- sha256:2c9d4e1f7a0b3c6d8e5f2a1b4c7d0e9f3a6b5c8d1e4f7a0b2c5d8e1f4a7b0c3d
workspace_id: local:3d816ecd-489a-4fa0-abe2-15ec93f60d5a
actor_device_id: dev_7ab74afd93b09861
---

# Your Plan Title

Your plan content here...
```

The last four fields appear on a version saved with a device signing key.
`workspace_id` reads `local:` and the project id until the repository joins
a team.

## Troubleshooting

### Command not found
```bash
# Run it as a module instead
python -m flanner --help

# Or reinstall it as a tool
uv tool install flanner
```

### Database not initialized
```bash
flanner init
```

### Your agent does not see the MCP server
```bash
flanner status   # which agent is missing it
flanner setup    # register with every agent again
# Then restart the agent
```

### Plan files not showing
```bash
flanner sync     # imports files that carry a flanner header
flanner doctor   # the catalog against the files on disk
```

## Tips

- ✅ Run `flanner init` in each project
- ✅ Use `sync --dry-run` before syncing
- ✅ Check `flanner status` when an agent seems not to see flanner
- ✅ Let the tools number versions; they skip a number whose file exists
- ✅ Restart the agent after registering

## Getting Help

```bash
# General help
flanner --help

# Command-specific help, with worked examples
flanner init --help
flanner sync --help
flanner web --help
```

## Documentation

- [README](../README.md) - Full documentation
- [INSTALLATION.md](INSTALLATION.md) - Installation guide
- [VERSIONING_GUIDE.md](VERSIONING_GUIDE.md) - How versioning works
- [PLAN_FILE_MANAGEMENT.md](PLAN_FILE_MANAGEMENT.md) - Plan file details
- [CLAUDE_INTEGRATION.md](CLAUDE_INTEGRATION.md) - Claude Code integration
