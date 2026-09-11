"""
Claude Code Integration for Flanner

Handles automatic registration of MCP server with Claude Code.
"""

import json
import logging
import platform
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def get_claude_config_path() -> Path | None:
    """
    Get the path to Claude Desktop configuration file.

    Returns:
        Path to claude_desktop_config.json or None if not found
    """
    system = platform.system()
    home = Path.home()

    # Possible Claude Desktop config locations
    possible_paths = []

    if system == "Windows":
        # Windows paths
        possible_paths = [
            home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
            home / ".claude" / "claude_desktop_config.json",
        ]
    elif system == "Darwin":  # macOS
        possible_paths = [
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            home / ".claude" / "claude_desktop_config.json",
        ]
    else:  # Linux
        possible_paths = [
            home / ".config" / "claude" / "claude_desktop_config.json",
            home / ".claude" / "claude_desktop_config.json",
        ]

    # Return first existing path
    for path in possible_paths:
        if path.exists():
            return path

    # Return default path (even if it doesn't exist yet)
    return possible_paths[0] if possible_paths else None


def read_claude_config() -> dict[str, Any]:
    """
    Read Claude Code MCP settings.

    Returns:
        Dictionary of MCP settings, or empty dict with mcpServers key
    """
    config_path = get_claude_config_path()

    if not config_path or not config_path.exists():
        return {"mcpServers": {}}

    try:
        with open(config_path, encoding="utf-8") as f:
            config: dict[str, Any] = json.load(f)

        # Ensure mcpServers key exists
        if "mcpServers" not in config:
            config["mcpServers"] = {}

        return config
    except Exception as e:
        logger.warning("Could not read Claude config: %s", e)
        return {"mcpServers": {}}


def write_claude_config(config: dict[str, Any]) -> bool:
    """
    Write Claude Code MCP settings.

    Args:
        config: Configuration dictionary

    Returns:
        True if successful, False otherwise
    """
    config_path = get_claude_config_path()

    if not config_path:
        return False

    try:
        # Ensure directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Write config with pretty formatting
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        return True
    except Exception as e:
        logger.error("Could not write Claude config: %s", e)
        return False


def get_local_server_config() -> dict[str, Any]:
    """
    Get the configuration for the local MCP server.

    Uses the absolute path of the interpreter that ran `flanner init`
    (``sys.executable``) with ``-m flanner.server``. That interpreter is the one
    flanner is installed into, so the invocation is independent of the client
    app's PATH and can't hit a "module not found" from a stray `python`. Bare
    `flanner-mcp` (or bare `python`) would need the Scripts/bin dir on the GUI
    app's PATH, which venv/pipx installs usually aren't. Re-run `flanner init`
    if the environment moves.

    Returns:
        Server configuration dictionary
    """
    return {
        "command": sys.executable,
        "args": ["-m", "flanner.server"],
        "env": {
            # Add any environment variables if needed
        },
    }


def get_cloud_server_config(server_url: str, api_key: str | None = None) -> dict[str, Any]:
    """
    Get the configuration for cloud-based MCP server (future use).

    Args:
        server_url: URL of the cloud MCP server
        api_key: Optional API key for authentication

    Returns:
        Server configuration dictionary
    """
    config = {
        "type": "cloud",
        "url": server_url,
    }

    if api_key:
        config["apiKey"] = api_key

    return config


def is_server_registered(server_name: str = "flanner") -> bool:
    """
    Check if MCP server is registered in Claude Code.

    Args:
        server_name: Name of the server to check

    Returns:
        True if server is registered, False otherwise
    """
    config = read_claude_config()
    return server_name in config.get("mcpServers", {})


def get_server_config_from_claude(server_name: str = "flanner") -> dict[str, Any] | None:
    """
    Get the current server configuration from Claude Code.

    Args:
        server_name: Name of the server

    Returns:
        Server configuration or None if not found
    """
    config = read_claude_config()
    server_config: dict[str, Any] | None = config.get("mcpServers", {}).get(server_name)
    return server_config


def register_mcp_server(
    server_name: str = "flanner",
    server_type: str = "local",
    server_url: str | None = None,
    api_key: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Register MCP server with Claude Code.

    Args:
        server_name: Name to register the server under
        server_type: "local" or "cloud"
        server_url: URL for cloud servers
        api_key: API key for cloud servers
        force: If True, overwrite existing configuration

    Returns:
        Tuple of (success: bool, message: str)
    """
    # Get current config
    config = read_claude_config()

    # Check if already registered
    if server_name in config["mcpServers"] and not force:
        return True, f"Server '{server_name}' is already registered in Claude Code"

    # Get server configuration based on type
    if server_type == "local":
        server_config = get_local_server_config()
    elif server_type == "cloud":
        if not server_url:
            return False, "Cloud server requires a URL"
        server_config = get_cloud_server_config(server_url, api_key)
    else:
        return False, f"Invalid server type: {server_type}"

    # Add server to config
    config["mcpServers"][server_name] = server_config

    # Write config
    if write_claude_config(config):
        action = "Updated" if force else "Registered"
        return True, f"{action} MCP server '{server_name}' in Claude Code"
    else:
        return False, "Failed to write Claude Code configuration"


def unregister_mcp_server(server_name: str = "flanner") -> tuple[bool, str]:
    """
    Unregister MCP server from Claude Code.

    Args:
        server_name: Name of the server to unregister

    Returns:
        Tuple of (success: bool, message: str)
    """
    config = read_claude_config()

    if server_name not in config.get("mcpServers", {}):
        return False, f"Server '{server_name}' is not registered in Claude Code"

    # Remove server
    del config["mcpServers"][server_name]

    # Write config
    if write_claude_config(config):
        return True, f"Unregistered MCP server '{server_name}' from Claude Code"
    else:
        return False, "Failed to write Claude Code configuration"


def verify_server_config() -> tuple[bool, str, dict[str, Any] | None]:
    """
    Verify that the registered MCP server configuration is correct.

    Returns:
        Tuple of (is_valid: bool, message: str, current_config: Optional[Dict])
    """
    server_name = "flanner"

    # Check if registered
    if not is_server_registered(server_name):
        return False, "MCP server is not registered in Claude Code", None

    # Get current config
    current_config = get_server_config_from_claude(server_name)
    expected_config = get_local_server_config()

    # Compare configurations
    if current_config == expected_config:
        return True, "MCP server configuration is correct", current_config
    else:
        return False, "MCP server configuration has changed", current_config


def get_claude_config_info() -> dict[str, Any]:
    """
    Get information about Claude Code configuration.

    Returns:
        Dictionary with configuration information
    """
    config_path = get_claude_config_path()

    info = {
        "config_path": str(config_path) if config_path else "Not found",
        "config_exists": config_path.exists() if config_path else False,
        "server_registered": is_server_registered(),
        "total_servers": 0,
        "our_server_config": None,
    }

    if config_path and config_path.exists():
        config = read_claude_config()
        info["total_servers"] = len(config.get("mcpServers", {}))
        info["our_server_config"] = config.get("mcpServers", {}).get("flanner")

    return info


def auto_register_on_init() -> tuple[bool, str]:
    """
    Automatically register MCP server on first init.
    This is called during 'mcp-plan init' command.

    Returns:
        Tuple of (success: bool, message: str)
    """
    # Check if already registered
    if is_server_registered():
        # Verify configuration is correct
        is_valid, msg, current_config = verify_server_config()

        if is_valid:
            return True, "MCP server already registered and up-to-date"
        else:
            # Configuration changed, update it
            success, update_msg = register_mcp_server(force=True)
            if success:
                return True, "MCP server configuration updated"
            else:
                return False, f"Failed to update configuration: {update_msg}"
    else:
        # Not registered, register it
        return register_mcp_server()


def check_server_status() -> dict[str, Any]:
    """
    Check the status of MCP server registration.
    This is called during 'mcp-plan status' command.

    Returns:
        Dictionary with status information
    """
    status = {
        "registered": False,
        "config_valid": False,
        "config_path": None,
        "message": "",
        "action_needed": None,
    }

    config_path = get_claude_config_path()
    status["config_path"] = str(config_path) if config_path else "Not found"

    if not config_path:
        status["message"] = "Claude Code configuration path not found"
        status["action_needed"] = "Please ensure Claude Code is installed"
        return status

    if not is_server_registered():
        status["message"] = "MCP server is not registered in Claude Code"
        status["action_needed"] = "Run: flanner init (or manually register)"
        return status

    status["registered"] = True

    # Verify configuration
    is_valid, msg, current_config = verify_server_config()
    status["config_valid"] = is_valid
    status["message"] = msg

    if not is_valid:
        status["action_needed"] = "Run: flanner init (to update configuration)"

    return status


def registration_instructions() -> str:
    """Manual registration steps for Claude Code, as text.

    Returns rather than prints. A library that writes to stdout has decided
    for its caller how the output is styled, whether `--quiet` applies, and
    where it goes — and this one has a caller that already owns all three.
    """
    server_config = get_local_server_config()
    block = json.dumps({"mcpServers": {"flanner": server_config}}, indent=2)
    return "\n".join(
        [
            "To add the MCP server to Claude Code by hand:",
            "",
            "  1. Open Claude Code settings",
            "  2. Add this to your MCP settings:",
            "",
            block,
            "",
            "  3. Restart Claude Code",
        ]
    )


# --- the other two agents ---------------------------------------------------
#
# Everything above is Claude Desktop's config file. `flanner status` read it
# and reported the result as "Claude Code", which is a different program with
# different files: `init` writes the project's .mcp.json and `setup` runs
# `claude mcp add`, and neither touches claude_desktop_config.json. So a
# correct setup was reported as "not registered", on the one command a new
# user runs to find out whether it worked.


def claude_code_user_config_path() -> Path:
    """Where `claude mcp add -s user` records servers."""
    return Path.home() / ".claude.json"


def claude_code_registration(start: Path, server_name: str = "flanner") -> str:
    """How Claude Code will find the server from `start`, or "" if it will not.

    Two scopes count. User scope is one file for every project; project
    scope is a .mcp.json at the repository root, which Claude Code looks
    for upward from where it was launched, so this walks the same way.
    """
    try:
        user = json.loads(claude_code_user_config_path().read_text(encoding="utf-8"))
        if server_name in (user.get("mcpServers") or {}):
            return "user scope"
    except (OSError, ValueError):
        pass

    here = start.resolve()
    for candidate in (here, *here.parents):
        manifest = candidate / ".mcp.json"
        if not manifest.exists():
            continue
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if server_name in (declared.get("mcpServers") or {}):
            # Short on purpose: this lands in a status column, and a full
            # temp-style path there wraps or is cut, taking the answer with it.
            return f".mcp.json in {candidate.name or candidate}"
        return ""
    return ""


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


CODEX_SNIPPET = """[mcp_servers.flanner]
command = "flanner-mcp"
"""


def _toml_loads() -> Any:
    """`tomllib.loads`, or None on Python 3.10, which ships no TOML reader."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.10 only
        return None
    return tomllib.loads


def codex_registration(server_name: str = "flanner") -> bool:
    """Whether Codex has the server in its config.

    Parsed where Python can, so a server declared as a dotted key or an
    inline table counts as well as a `[mcp_servers.flanner]` header. On
    3.10 it falls back to looking for the header, which is the form this
    package writes.
    """
    try:
        text = codex_config_path().read_text(encoding="utf-8")
    except OSError:
        return False
    loads = _toml_loads()
    if loads is not None:
        try:
            return server_name in (loads(text).get("mcp_servers") or {})
        except Exception:
            return False
    return f"[mcp_servers.{server_name}]" in text


def codex_installed() -> bool:
    """Whether Codex looks installed: its home directory, or its binary."""
    import shutil

    return codex_config_path().parent.is_dir() or shutil.which("codex") is not None


def ensure_codex_registration(server_name: str = "flanner") -> tuple[str, str]:
    """Add flanner to Codex's config, or say exactly why not.

    Returns `(outcome, detail)`, where outcome is `registered`, `already`,
    `not-installed` or `left-alone`.

    This package once clobbered an agent's config by editing it by hand,
    which is why Codex used to be report-only. The fix is to make the one
    edit that cannot clobber anything and to prove it before keeping it:
    flanner's table is appended, never merged; the result is parsed before
    it replaces the file; and the replacement is atomic. A file that does
    not parse to begin with is left exactly as it was.

    Nothing is created for somebody who does not use Codex. `init` sets up
    every agent by default, and a config directory appearing for a tool
    that is not installed would be litter.
    """
    import os
    import tempfile

    path = codex_config_path()
    if not codex_installed():
        return "not-installed", "Codex was not found on this machine"

    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        return "left-alone", f"{path} could not be read ({error})"

    loads = _toml_loads()
    if text.strip():
        if loads is None:
            if f"[mcp_servers.{server_name}]" in text:
                return "already", str(path)
            return "left-alone", "Python 3.10 cannot check a TOML file, so it was not edited"
        try:
            existing = loads(text)
        except Exception:
            return "left-alone", f"{path} does not parse, so it was not edited"
        if server_name in (existing.get("mcp_servers") or {}):
            return "already", str(path)

    separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    updated = text + separator + CODEX_SNIPPET
    if loads is not None:
        try:
            added = loads(updated)["mcp_servers"][server_name]
        except Exception:
            return "left-alone", "adding the entry would not have parsed, so nothing changed"
        if added.get("command") != "flanner-mcp":
            return "left-alone", "adding the entry did not read back as written"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        os.replace(temp, path)
    except OSError as error:
        Path(temp).unlink(missing_ok=True)
        return "left-alone", f"{path} could not be written ({error})"
    return "registered", str(path)
