"""severity-r1, version 1: R1's inputs in, High, Medium or Low out (Curb PRD §9.4).

The rules are a table, applied first match wins. An input Curb could not
read has already been given its worse value by the caller; what arrives
here is whether each input was read or assumed, so the verdict can say
which it rests on.

Every report names this function and its version. Changing a rule means
bumping `VERSION`, the expected fixture results and the release notes.
"""

from __future__ import annotations

from dataclasses import dataclass

NAME = "severity-r1"
VERSION = 1
LABEL = f"{NAME} v{VERSION}"

HIGH, MEDIUM, LOW = "High", "Medium", "Low"
CONFIGURED, ENFORCED, ASSUMED = "configured", "enforced", "assumed"

RULES: tuple[tuple[str, str, str], ...] = (
    ("H1", HIGH, "A wide credential is readable, and egress is uncontrolled"),
    (
        "H2",
        HIGH,
        "Any credential is readable, the agent takes in external content, "
        "and egress is uncontrolled",
    ),
    ("M1", MEDIUM, "A wide credential is readable, but every egress channel is controlled"),
    ("M2", MEDIUM, "Any credential is readable, and egress is uncontrolled"),
    ("L1", LOW, "None of the above"),
)
_TEXT = {rule: text for rule, _, text in RULES}
_LEVEL = {rule: level for rule, level, _ in RULES}


@dataclass(frozen=True)
class Inputs:
    wide_readable: bool
    any_readable: bool
    external_content: bool
    egress_uncontrolled: bool
    #: Each input that was assumed rather than read, said in words.
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    severity: str
    rule: str
    text: str
    evidence: str
    assumptions: tuple[str, ...]
    function: str = LABEL


def evaluate(inputs: Inputs) -> Verdict:
    if inputs.wide_readable and inputs.egress_uncontrolled:
        rule = "H1"
    elif inputs.any_readable and inputs.external_content and inputs.egress_uncontrolled:
        rule = "H2"
    elif inputs.wide_readable:
        rule = "M1"
    elif inputs.any_readable and inputs.egress_uncontrolled:
        rule = "M2"
    else:
        rule = "L1"
    evidence = ASSUMED if inputs.assumptions else CONFIGURED
    return Verdict(_LEVEL[rule], rule, _TEXT[rule], evidence, inputs.assumptions)
