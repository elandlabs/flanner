"""Refusing to remember a secret.

The one gate that runs before anything is written. A memory is a file on
disk, catalogued, searched, and one day handed to a teammate's device, so a
credential that gets in is a credential that has been copied, indexed and
possibly shared before anyone notices.

**Deterministic, not clever.** Everything here is a regular expression or an
arithmetic check. There is no model and no heuristic that depends on
context, because the cost of a false negative is a leaked key and the cost
of a false positive is one refusal a person can read and work around. A
check that cannot be reasoned about from its own source has no business
standing between a user and their credentials.

**No match is ever logged.** A guard that writes the secret it found into a
log file has moved the secret rather than stopped it. `Detection` carries
the pattern's name and a redacted excerpt; the matched text is discarded.

Pure by design: this module imports nothing from the package, so it can be
tested with a table of strings and reused by the capture pipeline, the
explicit remember path and, later, attachment ingestion, without any of
them arranging a database first.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple


class Detection(NamedTuple):
    """One reason a body was refused.

    ``excerpt`` is redacted. It exists so a person can find the offending
    line in their own text, not so the value can be recovered from it.
    """

    name: str
    excerpt: str


@dataclass(frozen=True)
class _Pattern:
    name: str
    regex: re.Pattern[str]


#: Length at which a single opaque token is checked for entropy.
#:
#: Thirty-two characters is where hex digests, base64 keys and session
#: tokens live, and where ordinary English words and file paths do not.
_ENTROPY_MIN_LEN = 32

#: Shannon entropy per character above which a long token is treated as
#: random rather than written. English prose sits near 2.0 bits and
#: base64-encoded randomness near 6.0; 4.0 separates them with room on both
#: sides. Measured per character so token length does not shift the answer.
_ENTROPY_THRESHOLD = 4.0

#: Long tokens that are high-entropy by nature and carry no authority.
#: Content hashes are in every plan header this product writes, so treating
#: them as secrets would refuse the product's own output.
_ENTROPY_EXEMPT = re.compile(
    r"""(?ix)
    ^(
        sha256: [0-9a-f]{64}
      | [0-9a-f]{40}                      # git object id
      | [0-9a-f]{64}                      # bare sha-256
      | [0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}   # uuid
    )$
    """
)

#: Ordered so the specific vendor patterns are reported before the generic
#: assignment catch, which would otherwise take the credit for all of them.
_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Pattern("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    _Pattern("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    _Pattern("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    _Pattern("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    _Pattern("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    _Pattern("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    _Pattern("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{24,}\b")),
    _Pattern("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    _Pattern("pypi_token", re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}\b")),
    _Pattern("private_key_block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    _Pattern("ssh_private_key", re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----")),
    _Pattern(
        "jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
    ),
    _Pattern(
        "connection_string_password",
        # A URL with credentials in it. The password half is what matters;
        # the scheme is left open because every database has its own.
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]{4,}@[^\s/]+"),
    ),
    _Pattern(
        "assigned_credential",
        # The generic catch, and the one most likely to fire on prose. It
        # wants an assignment operator and a value with no spaces, so
        # "the password policy is eight characters" does not match while
        # `password = hunter2hunter2` does.
        re.compile(
            r"""(?ix)
            \b (?: password | passwd | secret | token | api[_-]?key
                 | access[_-]?key | client[_-]?secret | private[_-]?key )
            \b \s* [:=] \s* ["']? (?P<value> [^\s"']{8,} ) ["']?
            """
        ),
    ),
)

#: Values that look like an assignment but are placeholders. A refusal on
#: `password = <your password here>` teaches people that the guard is noise,
#: which is how a guard stops being read.
_PLACEHOLDER = re.compile(
    r"""(?ix)
    ^(
        [<\[{(] .* [>\]})]                # <redacted>, [REDACTED], {{token}}
      | x{3,} | \*{3,} | \.{3,}
      | redacted | changeme | placeholder | example | yourpassword
      | your[_-]?(?:password|token|key|secret) (?:[_-]?here)?
      | todo | tbd | none | null | nil | unset | omitted
      | \$\{? [A-Za-z_][A-Za-z0-9_]* \}?  # $VAR / ${VAR}
      | %\( [A-Za-z_]+ \)s | \{[A-Za-z_]*\}
    )$
    """
)


#: Any run long enough to be worth an entropy check.
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_+/=.-]{%d,}" % _ENTROPY_MIN_LEN)  # noqa: UP031


def _redact(text: str) -> str:
    """A recognisable shape with nothing usable left in it."""
    stripped = text.strip()
    if len(stripped) <= 8:
        return "*" * len(stripped)
    return f"{stripped[:4]}{'*' * 8}{stripped[-2:]}"


def _entropy(token: str) -> float:
    """Shannon entropy per character, in bits."""
    if not token:
        return 0.0
    counts = Counter(token)
    total = len(token)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _looks_random(token: str) -> bool:
    """Whether a long token is more likely generated than written.

    Requires mixed character classes as well as entropy. A long lowercase
    word list or a run of hyphenated English clears the entropy bar on its
    own, and refusing those would be refusing prose.
    """
    if len(token) < _ENTROPY_MIN_LEN or _ENTROPY_EXEMPT.match(token):
        return False
    classes = sum(
        bool(re.search(pattern, token))
        for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )
    return classes >= 3 and _entropy(token) >= _ENTROPY_THRESHOLD


def scan(text: str) -> list[Detection]:
    """Every reason this text must not be stored.

    Returns all detections rather than the first, so a person fixing one
    does not discover the next on the following attempt.
    """
    found: list[Detection] = []
    seen: set[str] = set()

    for pattern in _PATTERNS:
        for match in pattern.regex.finditer(text):
            value = match.groupdict().get("value") or match.group(0)
            if _PLACEHOLDER.match(value.strip()):
                continue
            if pattern.name in seen:
                continue
            seen.add(pattern.name)
            found.append(Detection(pattern.name, _redact(value)))

    if "high_entropy_token" not in seen:
        for token in _LONG_TOKEN.findall(text):
            if _looks_random(token):
                found.append(Detection("high_entropy_token", _redact(token)))
                break

    return found


def is_safe(text: str) -> bool:
    """Whether this text may be stored. The whole module in one question."""
    return not scan(text)


def describe(detections: list[Detection]) -> str:
    """One line a person can act on, carrying no secret."""
    if not detections:
        return ""
    parts = ", ".join(f"{d.name} ({d.excerpt})" for d in detections)
    return f"refusing to store what looks like a credential: {parts}"
