"""What a project will let be remembered, and on whose say-so.

Two files, merged. A global one under the flanner home sets the defaults a
person wants everywhere; a project one in the repository narrows them for
work that needs narrowing. Neither is required, and with neither the
defaults below apply.

**A project may only tighten.** This is the rule the whole module exists
for. A repository is a thing you clone from somebody else, and a policy
file that could loosen would be a way to talk a stranger's machine into
capturing more than they agreed to. So a project file may move capture
toward off and never toward automatic, may lower a size cap and never
raise one, and may not turn on sharing at all. A value that would loosen
is ignored, and `explain` names it rather than letting it fail silently.

Pure: no database, no filesystem writes, no network. Reading two files and
deciding what they mean is the whole of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ValidationError

#: Capture modes, ordered from strictest to most permissive. The order is
#: the merge rule: a project may move left, never right.
OFF = "off"
EXPLICIT = "explicit"
SUGGEST = "suggest"
AUTO_SAFE = "auto_safe"
MODES = (OFF, EXPLICIT, SUGGEST, AUTO_SAFE)

#: Retrieval modes. Recorded and read by the agent instructions rather than
#: enforced here: nothing can stop an agent choosing not to search.
RETRIEVAL_MODES = ("off", "on_demand", "contextual")

#: The filename, in both places it can appear.
POLICY_FILENAME = "memory-policy.yml"

#: Every category, so a policy file naming one that does not exist is an
#: error rather than a rule that silently matches nothing.
CATEGORIES = (
    "fact",
    "decision",
    "preference",
    "constraint",
    "lesson",
    "relationship",
    "task_context",
)


@dataclass(frozen=True)
class Policy:
    """The effective rules for one project.

    Defaults are the shipped answer to "what should this do if nobody has
    said?", and they are deliberately cautious: suggest rather than
    capture, personal memory searchable but not written by an agent, and
    workspace sharing off because it does not exist yet.
    """

    capture_mode: str = SUGGEST

    # --- scope ---
    default_scope: str = "project"
    allow_personal: bool = False
    allow_workspace: bool = False

    # --- capture ---
    allow_categories: tuple[str, ...] = CATEGORIES
    require_approval: tuple[str, ...] = ("preference", "relationship")
    deny_sources: tuple[str, ...] = ()
    max_suggestions_per_session: int = 5
    max_auto_commits_per_day: int = 10

    # --- sensitivity ---
    #: Not configurable downward. A policy file that could permit storing a
    #: credential would be the one setting worth attacking.
    secrets: str = "reject"
    personal_data: str = "require_approval"

    # --- retention ---
    task_context_days: int = 30
    decisions_expire: bool = False

    # --- retrieval ---
    retrieval_mode: str = "contextual"
    include_personal: bool = True
    max_results: int = 8
    max_context_chars: int = 8000

    #: Which file each value came from, for `flanner mem policy show`. A
    #: person changing a setting and seeing no effect needs to be told
    #: which file is winning, not left to guess.
    provenance: dict[str, str] = field(default_factory=dict)

    #: Project values that were ignored because they would have loosened.
    #: Reported, never silently dropped.
    refused: tuple[str, ...] = ()

    def allows(self, category: str) -> bool:
        return category in self.allow_categories

    def needs_approval(self, category: str) -> bool:
        return category in self.require_approval

    @property
    def captures_automatically(self) -> bool:
        return self.capture_mode == AUTO_SAFE

    @property
    def suggests(self) -> bool:
        return self.capture_mode in (SUGGEST, AUTO_SAFE)


#: How a policy file's keys map onto the flat dataclass. Nested in the file
#: because that is how a person groups them, flat in the object because
#: nothing benefits from walking a tree at every gate.
_FIELDS: tuple[tuple[str, tuple[str, ...], type], ...] = (
    ("capture_mode", ("capture_mode",), str),
    ("default_scope", ("scope", "default"), str),
    ("allow_personal", ("scope", "allow_personal"), bool),
    ("allow_workspace", ("scope", "allow_workspace"), bool),
    ("allow_categories", ("capture", "allow_categories"), tuple),
    ("require_approval", ("capture", "require_approval"), tuple),
    ("deny_sources", ("capture", "deny_sources"), tuple),
    ("max_suggestions_per_session", ("capture", "max_suggestions_per_session"), int),
    ("max_auto_commits_per_day", ("capture", "max_auto_commits_per_day"), int),
    ("secrets", ("sensitivity", "secrets"), str),
    ("personal_data", ("sensitivity", "personal_data"), str),
    ("task_context_days", ("retention", "task_context_days"), int),
    ("decisions_expire", ("retention", "decisions_expire"), bool),
    ("retrieval_mode", ("retrieval", "mode"), str),
    ("include_personal", ("retrieval", "include_personal"), bool),
    ("max_results", ("retrieval", "max_results"), int),
    ("max_context_chars", ("retrieval", "max_context_chars"), int),
)


def read_file(path: Path) -> dict[str, Any]:
    """One policy file as a dict, or empty when there is none.

    A file that exists and cannot be parsed is an error. A file that does
    not exist is not: most projects will never have one.
    """
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValidationError(f"{path} is not valid YAML: {e}") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValidationError(f"{path} should be a mapping, not {type(loaded).__name__}")
    version = loaded.get("version", 1)
    if version != 1:
        raise ValidationError(f"{path} declares version {version}; this flanner understands 1")
    return dict(loaded)


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Follow a nested key, or return None if any step is missing."""
    current: Any = data
    for step in path:
        if not isinstance(current, dict) or step not in current:
            return None
        current = current[step]
    return current


def _coerce(name: str, value: Any, kind: type) -> Any:
    """One file value as the type the dataclass wants, or an error."""
    if kind is tuple:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValidationError(f"{name} should be a list, not {type(value).__name__}")
        return tuple(str(item) for item in value)
    if kind is bool:
        if not isinstance(value, bool):
            raise ValidationError(f"{name} should be true or false, not {value!r}")
        return value
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{name} should be a whole number, not {value!r}")
        if value < 0:
            raise ValidationError(f"{name} cannot be negative")
        return value
    return str(value)


def validate(data: dict[str, Any], *, where: str) -> list[str]:
    """Everything wrong with one policy file, as sentences.

    Returns rather than raises, so `flanner mem policy validate` can report
    every problem at once instead of one per run.
    """
    problems: list[str] = _unknown_keys(data, where)
    for name, path, kind in _FIELDS:
        raw = _dig(data, path)
        if raw is None:
            continue
        try:
            value = _coerce(name, raw, kind)
        except ValidationError as e:
            problems.append(f"{where}: {e}")
            continue
        problems += _check_value(name, value, where)
    return problems


def _unknown_keys(data: dict[str, Any], where: str) -> list[str]:
    """Keys that are not settings, named rather than ignored.

    A setting in the wrong section reads as configured and does nothing,
    which is the same failure as a rule naming a category that does not
    exist: it looks like it is working. Cheaper to refuse it than to let
    somebody spend an afternoon on it.
    """
    known: dict[str, set[str] | None] = {"version": None}
    for _name, path, _kind in _FIELDS:
        if len(path) == 1:
            known.setdefault(path[0], None)
        else:
            section = known.get(path[0])
            if not isinstance(section, set):
                section = set()
                known[path[0]] = section
            section.add(path[1])

    problems: list[str] = []
    for key, value in data.items():
        if key not in known:
            problems.append(f"{where}: {key!r} is not a setting")
            continue
        allowed = known[key]
        if allowed is None or not isinstance(value, dict):
            continue
        for inner in value:
            if inner not in allowed:
                problems.append(f"{where}: {key}.{inner} is not a setting")
    return problems


def _check_value(name: str, value: Any, where: str) -> list[str]:
    """Whether one already-typed value is in the vocabulary it belongs to."""
    if name == "capture_mode" and value not in MODES:
        return [f"{where}: capture_mode must be one of {', '.join(MODES)}, not {value!r}"]
    if name == "retrieval_mode" and value not in RETRIEVAL_MODES:
        return [f"{where}: retrieval mode must be one of {', '.join(RETRIEVAL_MODES)}"]
    if name == "default_scope" and value not in ("project", "personal"):
        return [f"{where}: scope default must be project or personal, not {value!r}"]
    if name in ("allow_categories", "require_approval"):
        unknown = [item for item in value if item not in CATEGORIES]
        if unknown:
            return [f"{where}: unknown categor{'y' if len(unknown) == 1 else 'ies'} {unknown}"]
    if name == "secrets" and value != "reject":
        return [f"{where}: secrets can only be 'reject'; storing one is never a policy choice"]
    return []


#: Which project values may only move one way, and which way that is.
#:
#: A repository is something you clone from somebody else. A policy file
#: that could loosen would let a stranger's repository talk your machine
#: into capturing more than you agreed to, so every one of these is refused
#: rather than merged.
def _tightens(name: str, project_value: Any, global_value: Any) -> bool:
    """Whether a project value is at least as strict as the global one."""
    if name == "capture_mode":
        return MODES.index(project_value) <= MODES.index(global_value)
    if name in ("allow_personal", "allow_workspace"):
        return not project_value or bool(global_value)
    if name in ("max_suggestions_per_session", "max_auto_commits_per_day", "max_results"):
        return int(project_value) <= int(global_value)
    if name == "max_context_chars":
        return int(project_value) <= int(global_value)
    if name == "allow_categories":
        return bool(set(project_value) <= set(global_value))
    if name == "secrets":
        return bool(project_value == "reject")
    # Everything else is a preference rather than a permission: which
    # categories need approval, how long task context lives, whether
    # personal memory is searched. A project may set those either way.
    return True


def load(project_root: str | Path | None, *, home: Path) -> Policy:
    """The effective policy, and where each value came from.

    `home` is passed rather than resolved so this module stays pure and so
    a test does not have to arrange an environment variable to describe a
    policy.
    """
    global_data = read_file(Path(home) / POLICY_FILENAME)
    project_path = Path(project_root) / ".flanner" / POLICY_FILENAME if project_root else None
    project_data = read_file(project_path) if project_path else {}

    problems = validate(global_data, where="global policy")
    problems += validate(project_data, where="project policy")
    if problems:
        raise ValidationError("; ".join(problems))

    policy = Policy()
    provenance: dict[str, str] = {}
    refused: list[str] = []
    changes: dict[str, Any] = {}

    for name, path, kind in _FIELDS:
        current = getattr(policy, name)

        raw_global = _dig(global_data, path)
        if raw_global is not None:
            current = _coerce(name, raw_global, kind)
            changes[name] = current
            provenance[name] = "global"

        raw_project = _dig(project_data, path)
        if raw_project is None:
            continue
        candidate = _coerce(name, raw_project, kind)
        if _tightens(name, candidate, current):
            changes[name] = candidate
            provenance[name] = "project"
        else:
            refused.append(
                f"{'.'.join(path)}={candidate!r} in the project policy would loosen "
                f"the global {current!r}, so it was ignored"
            )

    return replace(policy, **changes, provenance=provenance, refused=tuple(refused))


def explain(policy: Policy) -> list[tuple[str, Any, str]]:
    """Every setting, its value, and which file decided it.

    Read by `flanner mem policy show`. A person who edits a file and sees
    no change needs to be told which file is winning.
    """
    return [
        (name, getattr(policy, name), policy.provenance.get(name, "default"))
        for name, _path, _kind in _FIELDS
    ]


def example() -> str:
    """A policy file with every setting at its default, commented.

    Written by `flanner mem policy init`. Somebody narrowing a policy
    should start from what the defaults are rather than from a blank file
    and a documentation page.
    """
    return """# What this project will let be remembered.
#
# Every value here is the default; delete what you do not need to change.
# A project may only tighten what the global policy allows, so a setting
# that would loosen is ignored and reported by `flanner mem policy show`.
version: 1

# off | explicit | suggest | auto_safe
#   off       nothing is captured, and `remember` is refused
#   explicit  only when somebody says to remember
#   suggest   the agent proposes; you approve   (default)
#   auto_safe confirmed, low-sensitivity categories commit on their own
capture_mode: suggest

scope:
  default: project
  allow_personal: false
  allow_workspace: false

capture:
  allow_categories:
    - fact
    - decision
    - preference
    - constraint
    - lesson
    - relationship
    - task_context
  # Proposed rather than committed, even under auto_safe.
  require_approval:
    - preference
    - relationship
  deny_sources: []
  max_suggestions_per_session: 5
  max_auto_commits_per_day: 10

sensitivity:
  # Not configurable. Storing a credential is never a policy choice.
  secrets: reject
  personal_data: require_approval

retention:
  task_context_days: 30
  decisions_expire: false

retrieval:
  mode: contextual
  include_personal: true
  max_results: 8
  max_context_chars: 8000
"""
