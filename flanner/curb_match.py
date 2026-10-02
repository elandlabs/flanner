"""Whether a path falls under a Claude Code rule or a sandbox path list.

Claude Code's Read and Edit rules use gitignore patterns with four anchor
forms (E13): `//path` from the filesystem root, `~/path` from home, `/path`
from the settings source's directory, and a bare or `./` path from the
working directory. On Windows a path is compared in POSIX form, so
`C:\\Users\\a` is `/c/Users/a`. Sandbox paths use ordinary conventions
instead (`/tmp` is absolute) and the narrower of two overlapping entries
wins (E12).

Pure functions over strings and paths, so each rule shape can be tested
with a table of cases.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path, PurePath

_DRIVE = re.compile(r"^([A-Za-z]):[\\/]?")


def posix(path: PurePath | str) -> str:
    """`C:\\Users\\a` as `/c/users/a`; a POSIX path unchanged.

    A drive-letter path is lower-cased whole, because Windows compares
    paths without regard to case and a rule written in one case must still
    cover a file spelled in another.
    """
    text = str(path).replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    match = _DRIVE.match(text)
    if match:
        text = "/" + match.group(1) + "/" + text[match.end() :]
        text = text.lower()
    while "//" in text[1:]:
        text = text[0] + text[1:].replace("//", "/")
    return text.rstrip("/") or "/"


def _windows_form(text: str) -> bool:
    return len(text) > 2 and text[0] == "/" and text[2] == "/" and text[1].isalpha()


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern[str]:
    """A gitignore-style glob as a regular expression over a whole path."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(char))
                i += 1
            else:
                out.append("[" + pattern[i + 1 : end].replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_matches(pattern: str, target: str) -> bool:
    """Whether `target`, or a directory above it, matches `pattern`.

    A rule naming a directory covers what is inside it, as in gitignore.
    """
    if _windows_form(target) or _windows_form(pattern):
        pattern, target = pattern.lower(), target.lower()
    regex = _regex(pattern)
    current = target
    while True:
        if regex.match(current):
            return True
        if current in ("/", "") or "/" not in current:
            return False
        current = current.rsplit("/", 1)[0] or "/"


def _join(base: str, rest: str) -> str:
    rest = rest.lstrip("/")
    return base.rstrip("/") + "/" + rest if rest else base


def claude_pattern(spec: str, *, anchor: Path, cwd: Path, home: Path, deny: bool) -> str:
    """The absolute POSIX pattern a Read or Edit rule covers."""
    spec = spec.strip()
    if spec.startswith("//"):
        return "/" + spec[2:]
    if spec.startswith("~/"):
        return _join(posix(home), spec[2:])
    if spec.startswith("/"):
        return _join(posix(anchor), spec[1:])
    relative = spec[2:] if spec.startswith("./") else spec
    base = posix(cwd)
    body = relative.rstrip("/")
    if "/" not in body:
        return _join(base, "**/" + body)  # a bare name matches at any depth
    first, _, remainder = body.partition("/")
    if deny and remainder == "**" and not any(c in first for c in "*?["):
        return _join(base, "**/" + body)  # `secrets/**` as a deny matches nested copies
    return _join(base, body)


def claude_read_denied(
    target: Path,
    rules: Sequence[tuple[str, str | None, str, Path]],
    *,
    cwd: Path,
    home: Path,
) -> str | None:
    """The first Read deny rule that covers `target`, as written, or None.

    `rules` holds (tool, spec, source, anchor) for the deny list. A bare
    `Read` removes the tool and so covers everything. A `!` pattern carves
    out of the relative rules listed before it in the same source, and
    nothing else (E13).
    """
    path = posix(target)
    by_source: dict[str, list[tuple[str | None, Path]]] = {}
    for tool, spec, source, anchor in rules:
        if tool == "Read":
            by_source.setdefault(source, []).append((spec, anchor))
    for source_rules in by_source.values():
        hit: str | None = None
        hit_relative = False
        for spec, anchor in source_rules:
            if spec is None or spec.strip() in ("", "*", "**"):
                return "Read"
            text = spec.strip()
            if text.startswith("!"):
                carve = claude_pattern(
                    text[1:].lstrip("/~"), anchor=anchor, cwd=cwd, home=home, deny=True
                )
                if hit is not None and hit_relative and glob_matches(carve, path):
                    hit = None
                continue
            pattern = claude_pattern(text, anchor=anchor, cwd=cwd, home=home, deny=True)
            if hit is None and glob_matches(pattern, path):
                hit = f"Read({text})"
                hit_relative = not text.startswith(("/", "~"))
        if hit is not None:
            return hit
    return None


def sandbox_pattern(entry: str, *, anchor: Path, home: Path) -> str:
    entry = entry.strip()
    if entry.startswith("~/") or entry == "~":
        return _join(posix(home), entry[2:])
    if entry.startswith("/") or _DRIVE.match(entry):
        return posix(entry)
    relative = entry[2:] if entry.startswith("./") else entry
    if relative in ("", "."):
        return posix(anchor)
    return _join(posix(anchor), relative)


def _specificity(pattern: str) -> int:
    for index, char in enumerate(pattern):
        if char in "*?[":
            return index
    return len(pattern)


def sandbox_read_denied(
    target: Path,
    *,
    deny: Iterable[tuple[str, Path]],
    allow: Iterable[tuple[str, Path]],
    home: Path,
) -> str | None:
    """The sandbox entry that blocks a read of `target`, or None.

    The narrower of two overlapping entries wins, and a deny holds inside
    an equally wide allow (E12).
    """
    path = posix(target)
    best: tuple[int, bool, str] | None = None
    for entries, is_deny in ((deny, True), (allow, False)):
        for entry, anchor in entries:
            pattern = sandbox_pattern(entry, anchor=anchor, home=home)
            if not glob_matches(pattern, path):
                continue
            rank = _specificity(pattern)
            if best is None or rank > best[0] or (rank == best[0] and is_deny):
                best = (rank, is_deny, entry)
    return best[2] if best is not None and best[1] else None


def within(target: Path, roots: Iterable[Path]) -> bool:
    path = posix(target)
    for root in roots:
        base = posix(root)
        if path == base or path.startswith(base.rstrip("/") + "/"):
            return True
    return False


def codex_profile_pattern(key: str, *, home: Path, root: Path | None) -> str | None:
    """A Codex permissions-profile filesystem key as an absolute POSIX glob.

    Special tokens (`:minimal`, `:tmpdir`, ...) name system paths, never a
    credential, so they give None, as does a relative key outside
    `:workspace_roots`. Under `:workspace_roots` a key is relative to each
    workspace root.
    """
    key = key.strip()
    if not key or key.startswith(":"):
        return None
    if key.startswith("~"):
        return _join(posix(home), key[1:].lstrip("/\\"))
    if root is not None:
        return _join(posix(root), key.replace("\\", "/"))
    if key.startswith(("/", "\\")) or _DRIVE.match(key):
        return posix(key)
    return None


def codex_profile_reads(
    target: Path,
    entries: Sequence[tuple[str, str, bool]],
    *,
    roots: Sequence[Path],
    home: Path,
    base: str,
) -> str | None:
    """Whether a Codex permissions profile lets sandboxed commands read `target`.

    `entries` are (key, access, under workspace roots). The most specific
    matching entry wins, and on the same path `deny` beats `write` beats
    `read`. With no matching entry, a profile extending `:workspace` or
    `:read-only` reads inside the workspace roots and nowhere else (their
    `:minimal` paths are system ones). Returns None when readable, or what
    stops it.
    """
    path = posix(target)
    best: tuple[int, int, str, str] | None = None
    rank = {"read": 1, "write": 2, "deny": 3}
    for key, access, under_roots in entries:
        if access not in rank:
            continue
        for root in roots if under_roots else [None]:
            pattern = codex_profile_pattern(key, home=home, root=root)
            if pattern is None or not glob_matches(pattern, path):
                continue
            specific = sum(1 for part in pattern.split("/") if part and "*" not in part)
            candidate = (specific, rank[access], access, key)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is not None:
        return f"profile {best[3]} = deny" if best[2] == "deny" else None
    if base in (":workspace", ":read-only") and within(target, roots):
        return None
    return "the permissions profile does not grant it"
