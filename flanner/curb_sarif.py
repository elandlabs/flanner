"""SARIF 2.1.0 for the CI check and the app audit, for code scanning (Curb PRD §10.12-10.13)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LEVELS = {"High": "error", "Medium": "warning", "Low": "note"}


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    text: str
    severity: str


@dataclass(frozen=True)
class Result:
    rule: Rule
    message: str
    #: Relative to the folder that was checked, with forward slashes.
    path: str
    line: int
    properties: Mapping[str, Any] = field(default_factory=dict)


def document(results: Sequence[Result], rules: Sequence[Rule], *, version: str) -> dict[str, Any]:
    """One SARIF run holding every result, for upload to code scanning."""
    return {
        "$schema": SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "flanner curb",
                        "version": version,
                        "informationUri": "https://flanner.io",
                        "rules": [
                            {
                                "id": rule.id,
                                "name": rule.name,
                                "shortDescription": {"text": rule.text},
                                "defaultConfiguration": {"level": LEVELS[rule.severity]},
                            }
                            for rule in rules
                        ],
                    }
                },
                "results": [
                    {
                        "ruleId": result.rule.id,
                        "level": LEVELS[result.rule.severity],
                        "message": {"text": result.message},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": result.path},
                                    "region": {"startLine": max(1, result.line)},
                                }
                            }
                        ],
                        "properties": dict(result.properties),
                    }
                    for result in results
                ],
            }
        ],
    }
