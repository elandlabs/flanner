"""What must never be remembered, and what must not be refused.

Two tables. The first is credentials the guard has to catch, because a
memory is written to disk, indexed, and one day handed to a teammate's
device: a key that gets in has already been copied by the time anyone reads
it. The second is ordinary sentences a developer would actually want to
keep, because a guard that cries wolf is a guard people learn to work
around, and the way they work around it is by not using the feature.

Both tables are the specification. A new pattern belongs in both.
"""

from __future__ import annotations

import pytest

from flanner import memory_guard

# --- must be refused --------------------------------------------------------
#
# Every value here is fabricated to the right shape. None is live, and none
# is copied from anywhere real.

SECRETS = [
    ("aws access key", "the deploy user is AKIAIOSFODNN7EXAMPLE, rotate it"),
    ("aws session key", "ASIAY34FZKBOKMUTVV7A came from sts"),
    ("github classic token", "ghp_16C7e42F292c6912E7710c838347Ae178B4a11223344"),
    ("github oauth token", "gho_16C7e42F292c6912E7710c838347Ae178B4a11223344"),
    ("github fine-grained", "github_pat_11ABCDEFG0abcdefghijkl_ABCDEFGHIJKLMNOP"),
    ("slack bot token", "xox" "b-1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("slack user token", "xox" "p-1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("stripe live key", "sk_" "live_51H8xQ2KZvKuTb3mNaBcDeFgH"),
    ("stripe test key", "sk_" "test_51H8xQ2KZvKuTb3mNaBcDeFgH"),
    ("stripe restricted", "rk_" "live_51H8xQ2KZvKuTb3mNaBcDeFgH"),
    ("google api key", "AIzaSyD-1234567890abcdefghijklmnopqrstu"),
    ("openai key", "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCD"),
    ("anthropic key", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"),
    ("npm token", "npm_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("pypi token", "pypi-AgEIcHlwaS5vcmcCJDU2Nzg5MDEy"),
    ("rsa private key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n"),
    ("bare private key", "-----BEGIN PRIVATE KEY-----\nMIIEvQ==\n"),
    ("openssh private key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNz\n"),
    (
        "jwt",
        "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkoifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ),
    ("postgres url with password", "postgresql://flanner:h7Kd9wQz@db.internal:5432/app"),
    ("mongo url with password", "mongodb://admin:sup3rSecretPw@cluster0.example.net"),
    ("redis url with password", "redis://default:aVeryLongPassword1@cache:6379"),
    ("password assignment", "password = h7Kd9wQzP4xL"),
    ("password colon", 'password: "h7Kd9wQzP4xL"'),
    ("passwd assignment", "passwd=h7Kd9wQzP4xL"),
    ("secret assignment", "client_secret = 8f2Kd93mQzP4xLbN"),
    ("token assignment", "token: 8f2Kd93mQzP4xLbN"),
    ("api key underscore", "api_key = 8f2Kd93mQzP4xLbN"),
    ("api key hyphen", "api-key: 8f2Kd93mQzP4xLbN"),
    ("access key assignment", "access_key=8f2Kd93mQzP4xLbN"),
    ("private key assignment", "private_key = 8f2Kd93mQzP4xLbN"),
    ("high entropy blob", "the value was xQ7#mK9$pL2vN8wR4tY6uI0oP3aS5dF1gH+jK/lZ="),
    ("secret in a sentence", "I set password=Tr0ub4dor&3xKcd on the staging box"),
]

# --- must be allowed --------------------------------------------------------
#
# These are the memories the product exists to keep. A false positive here
# is a feature nobody uses.

INNOCENT = [
    ("a decision", "Use PostgreSQL advisory locks instead of Redis for this workflow."),
    ("a constraint", "The application must remain usable offline for seven days."),
    ("a preference", "Prefer alarming launch issues over formatting nitpicks in reviews."),
    ("a lesson", "The prior Iroh test failed behind UDP-blocking corporate networks."),
    ("a fact", "The production API is rate-limited to 20 requests per second."),
    ("a relationship", "The authorization ADR is implemented by authz.py and entitlement.py."),
    ("task context", "Resume the migration after validating schema version 4 on Windows."),
    ("password policy in prose", "Our password policy requires at least twelve characters."),
    ("token talk", "The token bucket refills at ten per minute, with a burst of ten."),
    ("secret in prose", "The secret to this codebase is that files are canonical, not the db."),
    ("api key in prose", "Ask the admin for an api key; it is not stored in the repo."),
    ("a redacted placeholder", "password = <your password here>"),
    ("an env var reference", "password = ${DB_PASSWORD}"),
    ("a shell var", "export PGPASSWORD=$DB_PASSWORD"),
    ("a format placeholder", "password = {password}"),
    ("literally redacted", "client_secret = REDACTED"),
    ("changeme", "password: changeme"),
    ("a content hash", "artifact_id: sha256:" + "a1b2c3d4" * 8),
    ("a git commit", "Fixed in 9f2a7c1e4b8d3f6a0c5e2b9d7f4a1c8e3b6d0f5a"),
    ("a uuid", "plan_file_id: 2f501f42-7f56-43c0-930c-471318407bca"),
    ("a long file path", "/home/jayson/projects/flanner/flanner/web/templates/plan_view.html"),
    ("a long import", "from flanner.database import create_project, get_session, init_database"),
    ("a url with no password", "https://api.flanner.io/v1/entitlements?workspace=ws_f80a403e"),
    ("a url with a port", "postgresql://flanner@db.internal:5432/app"),
    ("a long sentence", "The control plane stores accounts and devices and never plan content."),
    ("a base64 word run", "abcdefghijklmnopqrstuvwxyzabcdefghij"),
    ("a package version", "cryptography>=42,<51 is what the published metadata says"),
    ("a docker digest line", "image: flanner-cloud:latest built at 2026-09-07T15:27:58"),
    ("an error message", "ImportError: cannot import name 'refusals' from 'flanner'"),
    ("a command", "docker compose --profile ops up -d --build --force-recreate ops"),
    ("a header name", "The X-Flanner-Token header carries the daemon's shared secret."),
    ("mixed case prose", "SQLite Has No WAL And No Foreign Keys Enabled In This Package."),
]


@pytest.mark.parametrize(("label", "text"), SECRETS, ids=[s[0] for s in SECRETS])
def test_a_credential_is_refused(label, text):
    detections = memory_guard.scan(text)

    assert detections, f"{label} was allowed through"
    assert not memory_guard.is_safe(text)


@pytest.mark.parametrize(("label", "text"), INNOCENT, ids=[s[0] for s in INNOCENT])
def test_ordinary_context_is_allowed(label, text):
    detections = memory_guard.scan(text)

    assert memory_guard.is_safe(text), f"{label} was refused: {detections}"


# --- the guard must not become the leak -------------------------------------


def test_the_secret_never_appears_in_what_is_reported():
    """A guard that logs what it caught has moved the secret, not stopped it."""
    secret = "sk_" "live_51H8xQ2KZvKuTb3mNaBcDeFgH"

    detections = memory_guard.scan(f"the key is {secret}")
    message = memory_guard.describe(detections)

    assert secret not in message
    for detection in detections:
        assert secret not in detection.excerpt
        assert detection.excerpt != secret


def test_the_excerpt_is_enough_to_find_the_line():
    """Redaction that leaves nothing recognisable makes the refusal useless."""
    detections = memory_guard.scan("password = h7Kd9wQzP4xL")

    assert detections
    assert detections[0].excerpt.startswith("h7Kd")
    assert "*" in detections[0].excerpt


def test_every_reason_is_reported_not_just_the_first():
    """Fixing one and rediscovering the next is a bad way to learn there were two."""
    both = "AKIAIOSFODNN7EXAMPLE and also ghp_16C7e42F292c6912E7710c838347Ae178B4a11223344"

    names = {d.name for d in memory_guard.scan(both)}

    assert "aws_access_key" in names
    assert "github_token" in names


def test_a_vendor_pattern_is_named_rather_than_the_generic_catch():
    """ "assigned_credential" tells somebody less than "stripe_key" does."""
    names = [d.name for d in memory_guard.scan("stripe_key = sk_" "live_51H8xQ2KZvKuTb3mNaBcDeFgH")]

    assert "stripe_key" in names


def test_empty_and_whitespace_are_safe():
    for text in ("", "   ", "\n\n", "\t"):
        assert memory_guard.is_safe(text)


def test_describe_says_nothing_when_there_is_nothing_to_say():
    assert memory_guard.describe([]) == ""


def test_the_two_tables_are_big_enough_to_mean_something():
    """The suite's own floor. A guard proven by three strings is not proven."""
    assert len(SECRETS) >= 30
    assert len(INNOCENT) >= 30
