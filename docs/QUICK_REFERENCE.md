# Flanner - Quick Reference Card

## Installation

```bash
# Install dependencies
pip install -e .

# Install flanner command (recommended)
pip install -e .
```

## Core Commands

```bash
# Initialize Flanner in your project
flanner init

# Check status
flanner status

# List projects
flanner list

# List plan files for a project
flanner list --project PROJECT_NAME
```

## Plan File Management

```bash
# Sync existing files into database
flanner sync

# Sync with preview (dry run)
flanner sync --dry-run

# Sync specific project only
flanner sync --project PROJECT_NAME
```

## Web Interface

```bash
# Launch web UI (default port 8080)
flanner web

# Launch and auto-open browser
flanner web --open-browser

# Custom port
flanner web --port 3000
```

## Claude Code Integration

```bash
# Show integration status
flanner claude-info

# Register MCP server
flanner register

# Force update configuration
flanner register --force

# Unregister from Claude
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
flanner peer autostart on
flanner peer autostart off
```

A muted sender's messages still arrive and are listed; they never
interrupt. Quiet hours work the same way for everybody. The sender is told
neither. Messages are plain text, up to 4 KB, to up to 20 people. Your
organization's admin sets how long they are kept: 30, 90 (the default),
180 or 365 days. `flanner status` shows unread and queued messages, and
warns when nothing on this device is receiving.

Desktop notifications name the sender, never the message. They are on by
default; `FLANNER_DESKTOP_NOTIFICATIONS=off` turns them off.

## Common Workflows

### First Time Setup
```bash
cd your-project
flanner init
# Creates ~/.flanner/data.db
# Registers with Claude Code
# Sets up project
```

### Creating Plan Files

**Via Claude (Recommended):**
```
Ask Claude: "Create a new architecture plan for this project"
```

**Manual Creation:**
```bash
# 1. Create file in .plans/
# 2. Add frontmatter with UUIDs
# 3. Run sync
flanner sync
```

### Updating Plan Versions

```bash
# 1. Edit the plan file
# 2. Increment version: in frontmatter
# 3. Update created_at:
# 4. Run sync
flanner sync
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
~/.flanner/              # Flanner data directory
├── data.db              # SQLite database
└── server.pid           # Server process ID

your-project/
├── .git/
├── .gitignore           # Auto-updated
├── .plans/              # Plan files (git-ignored)
│   ├── architecture.md
│   └── api-design.md
├── flanner/
└── ...
```

## Plan File Format

```yaml
---
mcp_plan_file: true
project_id: uuid-here
plan_file_id: uuid-here
plan_name: architecture
version: 1
created_at: '2025-12-25T10:00:00Z'
created_by: user
---

# Your Plan Title

Your plan content here...
```

## Troubleshooting

### Command not found
```bash
# Use Python module instead
python -m flanner.cli --help

# Or install properly
pip install -e .
```

### Database not initialized
```bash
flanner init
```

### MCP server not registered
```bash
flanner register
# Then restart Claude Code
```

### Plan files not showing
```bash
flanner sync
```

## Tips

- ✅ Run `flanner init` in each project
- ✅ Use `sync --dry-run` before syncing
- ✅ Check `status` regularly
- ✅ Keep version numbers sequential
- ✅ Restart Claude after registration

## Getting Help

```bash
# General help
flanner --help

# Command-specific help
flanner init --help
flanner sync --help
flanner web --help
```

## Documentation

- `README.md` - Full documentation
- `INSTALLATION.md` - Installation guide
- `VERSIONING_GUIDE.md` - How versioning works
- `PLAN_FILE_MANAGEMENT.md` - Plan file details
- `CLAUDE_INTEGRATION.md` - Claude Code integration

---

**Version:** 1.0.0
**Quick Reference for:** Flanner
