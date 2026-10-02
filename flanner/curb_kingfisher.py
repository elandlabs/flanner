"""The leak sweep's detector: Kingfisher's Python SDK, an optional extra (Curb PRD §10.3).

`pip install 'flanner[sweep]'` brings `kingfisher-secret-scanner`
(Apache-2.0). Its scanning is offline. Only `validate`, run when a person
asks for it, contacts each secret's own issuer.

A match carries the secret's value only so the caller can digest it, and
Kingfisher's own finding only so validation can use it in the same
process. Neither is ever printed or stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INSTALL = "pip install 'flanner[sweep]'"


@dataclass(frozen=True)
class Match:
    rule_id: str
    rule_name: str
    line: int
    secret: str = field(repr=False)
    #: Kingfisher's finding, and every finding from the same file, which
    #: validation needs together (composite rules pair a key with its id).
    native: Any = field(default=None, repr=False, compare=False)
    group: tuple[Any, ...] = field(default=(), repr=False, compare=False)


def unavailable() -> str | None:
    """Why the sweep cannot run here, or None if it can."""
    try:
        import kingfisher_sdk  # noqa: F401 - only asking whether it imports
    except ImportError:
        return f"the leak sweep needs Kingfisher, which is not installed: {INSTALL}"
    return None


class Detector:
    """Kingfisher's built-in rules, offline, one file at a time."""

    def __init__(self) -> None:
        import kingfisher_sdk

        self._scanner = kingfisher_sdk.Scanner()

    def __call__(self, path: Path) -> list[Match]:
        found = tuple(self._scanner.scan_file(path))
        matches = []
        for finding in found:
            if not finding.visible:  # a helper half of a composite rule
                continue
            data = finding.to_dict()  # redacted by default
            location = data.get("location") or {}
            matches.append(
                Match(
                    rule_id=str(finding.rule_id),
                    rule_name=str(data.get("rule_name") or finding.rule_id),
                    line=int(location.get("line") or 0),
                    secret=str(finding.secret),
                    native=finding,
                    group=found,
                )
            )
        return matches


def validate(matches: list[Match]) -> list[str]:
    """Each match's outcome from its own issuer, in order.

    This sends every matched secret to the issuer its rule names, and
    nowhere else. Callers ask the person first, every run.
    """
    import kingfisher_sdk

    validator = kingfisher_sdk.Validator()
    outcomes: dict[int, str] = {}
    groups = {id(m.group): m.group for m in matches if m.group}
    for group in groups.values():
        for native, result in zip(group, validator.validate(list(group)), strict=False):
            outcomes[id(native)] = str(result.outcome)
    return [outcomes.get(id(m.native), "not_attempted") for m in matches]
