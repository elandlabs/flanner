# Claude Code Integration Guide

## Overview

The Flanner now features **automatic Claude Code integration**! The MCP server is automatically registered with Claude Code when you run `flanner init`, making it seamless to use plan file management directly from Claude.

## ✨ Features

### Automatic Registration
- ✅ **Auto-detect** Claude Code installation
- ✅ **Auto-configure** MCP server settings
- ✅ **Auto-update** configuration if paths change
- ✅ **Verify** registration on every `status` check

### Manual Control
- 🔧 Register/unregister at any time
- 🔧 Force update configuration
- 🔧 View detailed integration status
- 🔧 Support for future cloud-based servers

### Smart Checking
- 🔍 Every `init` command checks and updates registration
- 🔍 Every `status` command verifies configuration
- 🔍 Automatic detection of configuration drift

## 🚀 Quick Start

### 1. Initialize (Auto-Registration)

```bash
# Run init - MCP server is automatically registered!
flanner init

# Output includes:
# ✓ Initialized Flanner...
# 🔌 Registering MCP server with Claude Code...
# ✓ MCP server registered in Claude Code
#   You may need to restart Claude Code for changes to take effect
```

### 2. Verify Registration

```bash
# Check status
flanner status
```

**It shows** one table: the MCP server, the database and catalog, one row
each for Claude Desktop, Claude Code and Codex (each checked where that
agent looks), the Claude Desktop config path, and rows for the current
project. On a device whose plan includes messages, a Messages row says
whether this device is receiving. `flanner status` only reads; it changes
nothing.

### 3. View Detailed Info

```bash
flanner claude-info
```

**Shows:**
- Configuration file location
- Registration status
- Current server configuration
- Manual registration instructions (if not registered)

### 4. Restart Claude Code

After registration, restart Claude Code to load the MCP server.

### 5. Test with Claude

Ask Claude to interact with the plan manager:

```
User: "List all my projects in the plan manager"
User: "Create a new architecture plan for my current project"
User: "Show me the version history of the architecture plan"
```

## 🔧 CLI Commands

### View Status
```bash
# Show integration status (part of overall status)
flanner status

# Show detailed Claude Code integration info
flanner claude-info
```

### Manual Registration
```bash
# Register MCP server (if auto-registration failed or was skipped)
flanner register

# Force update configuration (if paths changed)
flanner register --force

# Skip Claude integration during init
flanner init --skip-claude
```

### Unregister
```bash
# Remove MCP server from Claude Code
flanner unregister
```

### Future: Cloud Server Registration
```bash
# Register cloud-based MCP server (planned for future)
flanner register --type cloud \
  --url https://your-server.com \
  --api-key YOUR_API_KEY
```

## 📁 Configuration File Location

The CLI automatically detects Claude Code's configuration file:

**Windows:**
```
C:\Users\YourName\AppData\Roaming\Claude\claude_desktop_config.json
```

**macOS:**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Linux:**
```
~/.config/claude/claude_desktop_config.json
```

## 🔍 How It Works

### On `init` Command:

1. **Check Registration**: Is the MCP server already registered?
2. **Verify Configuration**: If registered, is the configuration correct?
3. **Update if Needed**: If paths changed, update the configuration
4. **Register if Missing**: If not registered, add it to claude_desktop_config.json
5. **Notify User**: Show success message and next steps

### On `status` Command:

1. **Check Registration**: Show if server is registered
2. **Validate Configuration**: Check if current config matches expected
3. **Show Action Needed**: If not registered or outdated, suggest action

### Configuration Structure:

```json
{
  "mcpServers": {
    "flanner-manager": {
      "command": "python",
      "args": ["-m", "flanner.server"],
      "cwd": "C:\\path\\to\\mcp-cli",
      "env": {}
    }
  }
}
```

## 🎯 Use Cases

### Scenario 1: First Installation
```bash
# Install and initialize
pip install -e .
flanner init

# ✓ MCP server automatically registered
# ✓ Ready to use with Claude immediately
```

### Scenario 2: Moved Project Directory
```bash
# You moved the mcp-cli folder
# Run init to detect and update
flanner init

# ✓ Configuration automatically updated with new path
```

### Scenario 3: Multiple Developers
```bash
# Each developer runs init on their machine
flanner init

# ✓ Each gets correct path for their system
# ✓ No manual configuration needed
```

### Scenario 4: Troubleshooting
```bash
# Check what's wrong
flanner claude-info

# Shows detailed status and manual instructions
# Can manually register if needed
flanner register --force
```

## 🔮 Future: Cloud Server Support

The system is designed to support cloud-based MCP servers:

```bash
# Local server (current)
flanner register --type local

# Cloud server (future)
flanner register --type cloud \
  --url https://api.yourcompany.com/mcp \
  --api-key sk_live_...
```

**Configuration for cloud:**
```json
{
  "mcpServers": {
    "flanner-manager": {
      "type": "cloud",
      "url": "https://api.yourcompany.com/mcp",
      "apiKey": "sk_live_..."
    }
  }
}
```

**Benefits of cloud deployment:**
- 📡 Share plan files across team
- 🔐 Centralized access control
- 💾 Persistent storage
- 🔄 Real-time synchronization

## 💬 Messages From Teammates

On a Team Mesh plan with messaging switched on, teammates can send each
other short messages, device to device. Those messages reach Claude Code
and Codex too.

**flanner tells your agent to show a teammate's message and never act on
it.** The agent quotes the message, names the sender, and says it was only
shown, even when a message asks it to run something, change a setting,
mute someone or send a reply. These are instructions to the agent, not
locks: what it may do on your machine is still set by its own permissions.

### Setting it up

```bash
flanner init                 # adds the messages hook and agent instructions
flanner peer autostart on    # receive messages from the moment you log in
```

`flanner init` writes the hook into Claude Code's `settings.json` and a
short *Messages* block into `CLAUDE.md`, both in your user folder
(`~/.claude`, or `CLAUDE_CONFIG_DIR`). For Codex it writes `hooks.json` and
`AGENTS.md` under `~/.codex` (or `CODEX_HOME`). It does this only when your
plan includes messaging, and takes the block out again when it does not.
`flanner login` changes none of this; it only tells you to run
`flanner init`.

A message only arrives while something on your device is receiving.
`flanner peer autostart on` starts that at login: at once on macOS and
Linux, and from your next login on Windows, where `flanner peer start`
covers the meantime. `flanner status` warns when nothing is receiving.

### MCP tools

| Tool | What it does |
|------|--------------|
| `messages_inbox(thread="", all=False)` | Unread threads, or one whole thread by id. Opening a thread marks it read. |
| `messages_send(body, to=[...] or workspace="...", confirm=False, preview_token="")` | Message teammates by handle, or everyone in a workspace. |
| `messages_reply(thread, body, confirm=False, preview_token="")` | Answer everyone on a thread. |
| `messages_mute(handle, until="", off=False)` | Mute a teammate on this device: `until` like `8h`, `1d` or an ISO time; empty means until unmuted; `off=True` unmutes. |
| `messages_quiet_hours(set="")` | Empty reports quiet hours; `"22:00-07:00"` sets them; `"off"` clears them. |
| `mesh_status()` | Who this device is signed in as, its workspaces and peers. Read-only and offline. |

A **handle** is a teammate's short name, like `ben`. It comes from their
email and cannot be changed.

**Every send is previewed first.** `messages_send` and `messages_reply`
with `confirm=False` send nothing; they return who the message would go to,
and a `preview_token`. The agent is told to show you that, and to send with
`confirm=True` and the token only after you say yes. flanner refuses a send
without a token from a preview of the same message and recipients, or one
older than 15 minutes. It cannot tell whether you said yes: that is your
agent's own tool approval. Delivery is reported per person: delivered,
queued (their device is not receiving right now) or failed, with the
reason. `flanner actions list` shows every message an agent sent.

`messages_mute` and `messages_quiet_hours` need no preview: they change only a
setting on your own device. The agent is told to use them only when you
ask.

### How a new message shows up

A **hook** is a command Claude Code or Codex runs at set moments. flanner's
hook adds new messages to the session:

- **At your next prompt.** Always.
- **Between tool calls,** at most once a minute. This is the default.
- **Never during quiet hours or from a muted sender.** Those messages
  still arrive and are listed. What quiet hours held back appears at your
  first prompt after they end; a muted sender's messages appear only if
  you unmute them while they are still unread.
- **More than three at once** become one line naming the senders.

`flanner messages interrupt` picks when:

```bash
flanner messages interrupt tool      # between tool calls and at your next prompt (default)
flanner messages interrupt prompt    # at your next prompt only
flanner messages interrupt channel   # the moment they arrive, in Claude Code (see below)
```

### Claude Code: the channel

A **channel** lets an MCP server push a message into a Claude Code session,
even an idle one. Channels are a Claude Code research preview.

```bash
flanner messages interrupt channel
claude --dangerously-load-development-channels server:flanner
```

The development flag is the current way. Claude Code's `--channels` flag
accepts only plugins, and flanner is not a plugin yet; one is planned. On a
Claude Team or Enterprise plan, channels are blocked, the development flag
included, until an Owner turns them on in the Claude Code admin settings or
sets `channelsEnabled` in managed settings.

With `channel` set, a message appears the moment it arrives, and again at
your next prompt with a note that it may be a repeat. The hook stays quiet
between tool calls. The repeat is on purpose: if Claude Code was started
without the flag, or channels are switched off, it drops channel messages
without saying so, and the prompt is then the only place you see them.
Every open agent running flanner is sent the push, and only a Claude Code
session started with the channel shows it. A message already waiting when
a session starts is not pushed; the hook shows it at your next prompt.

**Without the channel,** messages still reach you. They appear at your
next prompt and between tool calls. An idle session shows nothing until you
type, but the desktop notification still tells you who wrote.

### Codex

The same hook serves Codex, at your next prompt and between tool calls.
Codex has no channel, so `channel` means "between tool calls" there. An
idle Codex session shows nothing until you type.

- **Trust the hook once.** Codex skips a new hook until you open Codex and
  run `/hooks` to trust it. Do this after `flanner init`.
- **If your administrator allows only managed hooks,** Codex skips
  flanner's hook. `flanner init` warns you when that rule is in a
  `requirements.toml` on this machine, but cannot see one pushed by device
  management. Either way, run `flanner init --print-codex-hook` and give
  your administrator what it prints. You still see messages in `flanner
  messages inbox`, the web UI and desktop notifications, and Codex can read
  them if you ask.

### Desktop notifications

Whatever agent you use, `flanner peer serve` shows a desktop notification
naming the sender, never the message. It stays quiet during quiet hours and
for a muted sender. They are on by default;
`FLANNER_DESKTOP_NOTIFICATIONS=off` turns them off. Set it where the
receiver runs: one started at login does not see variables set in your
shell.

Clicking one opens the thread in the web UI, when `flanner web` is
running. That works on Windows, on macOS with `terminal-notifier`
installed, and on Linux with `notify-send` from libnotify 0.7.9 or later.
Elsewhere the notification shows without the click. On Linux,
notifications need `notify-send`; without it, none appear.

## 🐛 Troubleshooting

### Issue: "Could not find Claude Desktop configuration path"

`flanner register` prints this about Claude Desktop only. `flanner status`
shows whether Claude Code and Codex are set up.

**Solution:**
- Ensure Claude Desktop is installed
- Check if `~/.claude/claude_desktop_config.json` exists (create manually if needed)
- Try manual registration

### Issue: "MCP server registered but not working in Claude Desktop"

**Solutions:**
1. Restart Claude Desktop completely
2. Check Claude Desktop's MCP settings to verify registration
3. Run `flanner claude-info` to verify configuration
4. Re-register with `flanner register --force`

### Issue: "Configuration path is different"

**Solution:**
Edit `flanner/claude_integration.py` and add your path to `possible_paths`:
```python
if system == "Windows":
    possible_paths = [
        home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        home / ".claude" / "claude_desktop_config.json",
        home / "Your" / "Custom" / "Path" / "claude_desktop_config.json",  # Add here
    ]
```

### Issue: "Want to disable auto-registration"

**Solution:**
```bash
# Skip Claude integration during init
flanner init --skip-claude

# Or remove flanner from Claude Desktop's config after init
flanner unregister
```

## 📊 Status Indicators

### In `status` command:

The Claude Desktop row reads one of:

- **registered**: flanner's entry is there and matches this install.
- **registered, config is out of date**: the entry no longer matches this
  install. Run `flanner register --force` or `flanner init`.
- **not registered**: run `flanner register`.

### In `claude-info` command:

Shows complete details:
- Configuration file path
- Whether file exists
- Registration status
- All MCP servers configured
- Current configuration (JSON)

## 🔐 Security Notes

- Configuration files are stored locally on your machine
- No credentials are transmitted during registration
- Cloud server support will require API keys (future)
- API keys should be stored securely (environment variables recommended)

## 📝 Summary

**What You Get:**
1. ✅ **Zero-config setup** - Just run `flanner init`
2. ✅ **Auto-updates** - Configuration stays current
3. ✅ **Smart checking** - Verifies on every status check
4. ✅ **Manual control** - Override when needed
5. ✅ **Future-proof** - Ready for cloud deployment

**Commands to Remember:**
```bash
flanner init          # Auto-register on first run
flanner status        # Check registration status
flanner claude-info   # View detailed info
flanner register      # Manual registration
flanner unregister    # Remove registration
```

**After Setup:**
- Restart Claude Code
- Ask Claude to interact with plan manager
- Enjoy seamless plan file management!

---

*Built for Flanner - December 2025*
