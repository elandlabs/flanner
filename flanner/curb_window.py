"""The one place Curb shows full detail: a window on the person's screen (Curb PRD §11.1).

Terminal output is redacted for every caller, because an agent can unset
its markers and fake a terminal. Full detail goes to a window instead:
`flanner curb show` starts a separate process that works the report out
again and draws it, and nothing comes back to the command that asked. An
agent that runs `show` gets a notice; the person gets the window.

Nothing with full detail is written to disk. The window uses Python's own
tkinter, so it needs no new dependency.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def unavailable() -> str | None:
    """Why a window cannot open here, or None if it can."""
    platform: str = sys.platform  # a plain str, so mypy checks every branch on every OS
    if platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return "there is no desktop session here (no DISPLAY or WAYLAND_DISPLAY)"
    try:
        import tkinter  # noqa: F401 - only asking whether it imports
    except ImportError:
        if platform.startswith("linux"):
            return "Python's Tk support is missing; install your distribution's python3-tk package"
        return "Python's Tk support is missing from this Python"
    return None


def launch(arguments: Sequence[str], *, popen: Callable[..., Any] = subprocess.Popen) -> None:
    """Start the window in its own process, detached, and return at once."""
    command = [sys.executable, "-m", "flanner", "curb", "show", "--in-window", *arguments]
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        options["start_new_session"] = True
    popen(command, **options)


def render(reports: Sequence[Mapping[str, Any]], inventory: Sequence[Mapping[str, Any]]) -> str:
    """The full report as plain text, for the window. Pure, so it can be tested."""
    lines: list[str] = []
    for report in reports:
        severity = report["severity"]
        lines += [
            f"{report['label']}: {severity['level']} ({severity['rule']}, {severity['evidence']})",
            f"  {severity['reason']}",
            f"  Launch: {report['launch']}",
            f"  Directory: {report['directory']}",
        ]
        if report.get("launch_command"):
            lines.append("  Command: " + " ".join(report["launch_command"]))
        lines.append(
            f"  Version: {report['version'] or 'unknown'}"
            + ("" if report["supported"] else f" (tested: {report['baseline']})")
        )
        lines.append("")
        lines.append("  Channels")
        for channel in report["channels"]:
            lines.append(
                f"    {channel['channel']}: {channel['state']} ({channel['evidence']}, "
                f"{channel['disposition']})"
            )
            lines.append(f"      {channel['why']}")
            if channel.get("fix"):
                lines.append(f"      Fix: {channel['fix']}")
        lines.append("")
        lines.append("  Credentials")
        for item in report["credentials"].get("items", []):
            through = ", ".join(item["readable_through"]) or "no channel"
            lines.append(f"    {item['label']} [{item['category']}]: readable through {through}")
            for path in item["paths"]:
                lines.append(f"      {path}")
            if item["names"]:
                lines.append("      Names: " + ", ".join(item["names"]))
            if item["identity"]:
                lines.append(f"      Identity: {item['identity']}")
            if item["expires"]:
                lines.append(f"      Expires: {item['expires']}")
            if item["blocked_by"]:
                lines.append("      Blocked by: " + "; ".join(item["blocked_by"]))
        if report.get("mcp_servers"):
            lines.append("")
            lines.append("  MCP servers")
            for server in report["mcp_servers"]:
                target = " ".join(server["command"]) or server["url_host"] or ""
                lines.append(
                    f"    {server['name']} ({server['transport']}, {target}) "
                    f"from {server['configured_in']}, controlled by {server['controlled_by']}"
                )
        if report.get("settings_layers"):
            lines.append("")
            lines.append("  Settings layers")
            for layer in report["settings_layers"]:
                state = "present" if layer["present"] else "absent"
                problem = f", {layer['problem']}" if layer["problem"] else ""
                lines.append(f"    {layer['name']}: {layer['where']} ({state}{problem})")
        for heading, key in (("Not checked", "not_checked"), ("Assumed", "assumed")):
            if report.get(key):
                lines.append("")
                lines.append(f"  {heading}")
                lines += [f"    {entry}" for entry in report[key]]
        lines.append("")
        lines.append(f"  {report['assumption']}")
        lines.append("")
    for agent in inventory:
        lines.append(f"{agent['label']} inventory")
        for job in agent.get("scheduled_jobs", []):
            lines.append(f"  Job {job['name']} ({job['scheduler']}): " + " ".join(job["command"]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_sweep(sweep: Mapping[str, Any]) -> str:
    """The leak sweep's findings as plain text: types and locations, never a value."""
    by_class = sweep["by_class"]
    lines = [
        f"Leak sweep: {sweep['secrets']} secret(s) in {sweep['files_scanned']} file(s) read",
        *(f"  {key}, {name}: {by_class[key]}" for key, name in sweep["classes"].items()),
        "",
    ]
    for item in sweep["findings"]:
        lines.append(f"{item['class']} ({item['class_name']}): {item['type']}")
        lines.append(f"  {item['category']}: {item['path']}, line {item['line']}")
        if item["readable_by"]:
            lines.append("  Readable by: " + ", ".join(item["readable_by"]))
        if item["validation"]:
            lines.append(f"  Issuer says: {item['validation']}")
        lines.append(f"  {item['advice']}")
        lines.append("")
    if sweep["launches"]:
        lines.append("Agent launches checked for B and C")
        lines += [f"  {launch}" for launch in sweep["launches"]]
    else:
        lines.append("No supported agent was found, so nothing counts as readable by one.")
    if sweep["not_checked"]:
        lines.append("")
        lines.append("Not checked")
        lines += [f"  {entry}" for entry in sweep["not_checked"]]
    return "\n".join(lines).rstrip() + "\n"


def show(title: str, text: str) -> None:  # pragma: no cover - draws on a real screen
    """Draw the report in a scrollable, read-only window and wait for it to close."""
    import tkinter
    from tkinter import scrolledtext

    root = tkinter.Tk()
    root.title(title)
    view = scrolledtext.ScrolledText(root, wrap="word", width=110, height=42)
    view.insert("1.0", text)
    view.configure(state="disabled")
    view.pack(fill="both", expand=True)
    root.mainloop()
