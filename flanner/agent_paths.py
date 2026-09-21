"""Where Claude Code and Codex keep their user-level configuration.

Both agents let a person move it: `CLAUDE_CONFIG_DIR` relocates Claude
Code's `settings.json`, `CLAUDE.md` and `.claude.json`, and `CODEX_HOME`
relocates Codex's `config.toml`, `hooks.json` and `AGENTS.md`. flanner
writes where the agent will read, which is also what lets the mesh
messaging plan's section 21 test against an isolated agent without
touching the person's real one.

Standard library only, so every module that touches an agent's files can
ask the same question and get the same answer.
"""

from __future__ import annotations

import os
from pathlib import Path


def claude_config_dir() -> Path:
    """Claude Code's user directory: `CLAUDE_CONFIG_DIR`, or `~/.claude`."""
    moved = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(moved) if moved else Path.home() / ".claude"


def claude_user_config() -> Path:
    """Where `claude mcp add -s user` records servers.

    `~/.claude.json` sits beside the directory, not inside it, unless the
    directory has been moved, in which case it moves inside.
    """
    moved = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(moved) / ".claude.json" if moved else Path.home() / ".claude.json"


def codex_home() -> Path:
    """Codex's directory: `CODEX_HOME`, or `~/.codex`."""
    moved = os.environ.get("CODEX_HOME")
    return Path(moved) if moved else Path.home() / ".codex"
