"""A known catalog, built the way the product builds one.

Everything here goes through the domain layer — `database.create_project`,
`plan_ops`, `review`, `memory_ops` — rather than through SQL. A seeder that
writes rows directly drifts: it keeps passing after the write path it is
meant to stand in for has changed shape, and the tests on top of it then
pass against a world that no longer exists.

The catalog is the same every run in everything a person or a test can see:
the same projects, plans, versions, review state, memories and text. Ids and
timestamps are **not** fixed, and cannot be through this layer — row ids come
from `uuid4()` column defaults and times from `utils.utcnow()`, neither of
which takes an argument. So `seed()` returns a manifest of what it made, and
`--home` writes it beside the database. Address the catalog by that manifest,
never by a hard-coded uuid.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "demo-manifest.json"

#: Who may do what in the seeded catalog. See the `roles=` calls below.
_ROLES = {"demo": "maintainer", "reviewer": "maintainer"}

#: Committed into the seeded repository so a plan can cite a real file, and
#: so freshness has commits to count rather than an empty history.
_REPO_FILES = {
    "src/payments/charge.py": "def charge(amount):\n    return {'ok': True, 'amount': amount}\n",
    "src/payments/webhooks.py": "def deliver(event):\n    return True\n",
    "src/payments/keys.py": "ROTATION_DAYS = 30\n",
    "README.md": "# payments-service\n\nThe part that takes money.\n",
}

#: Committed and then deleted. `freshness._bad_refs` counts a cited path as
#: invalid only when git history shows it once existed — "gone" rather than
#: "not started yet" — so a plan cannot be made stale by citing a path that
#: was never there.
_MOVED_FILE = "src/payments/retry_queue.py"

_JWT_VERSIONS = (
    ("# JWT key rotation\n\nRotate the signing key on a schedule.\n", ""),
    (
        "# JWT key rotation\n\nRotate the signing key on a schedule.\n\n"
        "Rotation lives in `src/payments/keys.py`.\n",
        "point at where rotation lives",
    ),
    (
        "# JWT key rotation\n\nRotate the signing key every 30 days.\n\n"
        "Rotation lives in `src/payments/keys.py`.\n",
        "settle on 30 days",
    ),
    (
        "# JWT key rotation\n\nRotate the signing key every 30 days.\n\n"
        "Rotation lives in `src/payments/keys.py`.\n\n"
        "## Overlap\n\nOld and new keys both verify for one rotation.\n",
        "keep an overlap window",
    ),
    (
        "# JWT key rotation\n\nRotate the signing key every 30 days.\n\n"
        "Rotation lives in `src/payments/keys.py`.\n\n"
        "## Overlap\n\nOld and new keys both verify for one rotation.\n\n"
        "## Rollback\n\nRe-publish the previous public key.\n",
        "say how to roll back",
    ),
)

# A path that is not in the repository. `freshness.extract_refs` picks it out
# of the code span and `_status` returns `stale` on a reference that no longer
# exists, before churn or age is consulted — which makes this the one way to
# seed a stale plan that does not depend on how many commits a clock allows.
_WEBHOOK_PLAN = (
    "# Webhook delivery\n\nRetries back off exponentially.\n\n"
    "The retry loop is in `src/payments/retry_queue.py`, which was moved.\n"
)

_IDEMPOTENCY_PLAN = (
    "# Idempotency keys\n\nEvery write takes a client-supplied key.\n\n"
    "Superseded by the gateway's own deduplication.\n"
)

_RATE_LIMIT_PLAN = (
    "# Rate limiting\n\nA token bucket per API key.\n\n"
    "## Limits\n\n60 requests a minute, bursting to 120.\n"
)

_MEMORIES = (
    {
        "title": "Money is integer minor units",
        "content": (
            "Amounts are integer minor units everywhere. A float rounded a "
            "cent away from the ledger once and the reconciliation took a week."
        ),
        "category": "constraint",
    },
    {
        "title": "Webhooks are at-least-once",
        "content": (
            "Webhook delivery is at-least-once, so every consumer has to be "
            "idempotent. The gateway will not promise exactly-once."
        ),
        "category": "decision",
    },
    {
        "title": "Retry budget is per key",
        "content": (
            "The retry budget is counted per API key rather than per endpoint, "
            "so one noisy consumer cannot spend another's."
        ),
        "category": "lesson",
    },
)


def _rmtree(path: Path) -> None:
    """Delete a tree that contains a git repository.

    Windows marks objects in `.git` read-only, and `rmtree` refuses those
    rather than clearing the bit, so a plain call fails on every re-seed.
    """

    def clear(func: Callable[[str], Any], target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear)
    else:
        shutil.rmtree(path, onerror=lambda f, t, e: clear(f, t, e[1]))


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(root),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "flanner demo",
            "GIT_AUTHOR_EMAIL": "demo@flanner.invalid",
            "GIT_COMMITTER_NAME": "flanner demo",
            "GIT_COMMITTER_EMAIL": "demo@flanner.invalid",
        },
    )


def _git_repo_with_history(root: Path) -> None:
    """A repository with real history, so freshness has something to judge."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    for path, body in _REPO_FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        _git(root, "add", path)
        _git(root, "commit", "-q", "-m", f"add {path}")
    moved = root / _MOVED_FILE
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_text("QUEUE = []\n", encoding="utf-8")
    _git(root, "add", _MOVED_FILE)
    _git(root, "commit", "-q", "-m", f"add {_MOVED_FILE}")
    _git(root, "rm", "-q", _MOVED_FILE)
    _git(root, "commit", "-q", "-m", f"move {_MOVED_FILE} out of this service")


def _signed_in_session(organization: str = "org_demo") -> None:
    """Cache a genuinely signed entitlement, so the Team group renders joined.

    Self-signed on purpose: the control plane's key is whatever the keyring
    says it is, and a demo home is not proving anything to anybody. It is a
    real token through the real encoder, so nothing downstream needs a
    special case for it.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from . import identity
    from . import session as cache
    from .artifacts import canonical_bytes
    from .entitlements import Claims, WorkspaceCapability, encode_token
    from .workflow import MAINTAINER

    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)

    def stamp(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    device_id = identity.device_id()
    claims = Claims(
        organization_id=organization,
        user_id="demo",
        device_id=device_id,
        key_id="sk_demo",
        issued_at=stamp(now - timedelta(minutes=1)),
        expires_at=stamp(now + timedelta(days=30)),
        workspace_capabilities=(WorkspaceCapability(workspace_id="ws_demo", role=MAINTAINER),),
    )
    token = encode_token(claims, identity.sign(canonical_bytes(claims.to_dict()), key))
    cache.save(
        cache.Session(
            endpoint="https://api.flanner.invalid",
            device_id=device_id,
            organization_id=organization,
            user_id="demo",
            entitlement=token,
            keyring={"sk_demo": identity.public_key_b64(key.public_key())},
            org_role="admin",
        )
    )


def seed(home: Path | str, *, signed_in: bool = False) -> dict[str, Any]:
    """Build the catalog under `home` and return what was made.

    `home` becomes `FLANNER_HOME` for this process: half the package resolves
    it from the environment at call time, so setting it is the only way to
    point them all at the same place.
    """
    home = Path(home).resolve()
    # A re-seed replaces the catalog rather than adding to it: "the same
    # every run" has to survive running twice. Only a home this function
    # wrote is removed — anything else is somebody's real data, and the
    # refusal is cheaper than the apology.
    if (home / MANIFEST_FILENAME).exists():
        _rmtree(home)
    elif home.exists() and any(home.iterdir()):
        raise ValueError(f"{home} is not empty and was not seeded by this command")
    home.mkdir(parents=True, exist_ok=True)
    os.environ["FLANNER_HOME"] = str(home)
    os.environ.pop("FLANNER_DB_PATH", None)

    from . import memory_ops, review
    from .database import create_project, get_session, init_database
    from .plan_ops import create_plan, record_new_version
    from .workflow import APPROVE

    init_database(str(home / "data.db"))
    session = get_session()

    payments_root = home / "repos" / "payments-service"
    _git_repo_with_history(payments_root)
    # Deliberately not a repository. Freshness has to say "git unavailable"
    # somewhere, and a catalog where every project is a repo never shows it.
    notifications_root = home / "repos" / "notifications-service"
    (notifications_root / ".plans").mkdir(parents=True, exist_ok=True)

    payments = create_project(
        session,
        name="payments-service",
        description="Charges, refunds and the webhooks that announce them.",
        project_root=str(payments_root),
        auto_gitignore=False,
    )
    notifications = create_project(
        session,
        name="notifications-service",
        description="Email and push, fanned out from the event bus.",
        project_root=str(notifications_root),
        auto_gitignore=False,
    )
    session.commit()

    # Five versions: created at v1, then four saves.
    jwt, _ = create_plan(
        session,
        project=payments,
        name="jwt-key-rotation",
        description="How the signing key is rotated without dropping traffic.",
        content=_JWT_VERSIONS[0][0],
        created_by="demo",
    )
    session.commit()
    for content, notes in _JWT_VERSIONS[1:]:
        record_new_version(
            session,
            project=payments,
            plan_file=jwt,
            content=content,
            created_by="demo",
            notes=notes,
        )

    webhooks, _ = create_plan(
        session,
        project=payments,
        name="webhook-delivery",
        description="At-least-once delivery, with a retry budget.",
        content=_WEBHOOK_PLAN,
        created_by="demo",
    )
    session.commit()

    idempotency, _ = create_plan(
        session,
        project=payments,
        name="idempotency-keys",
        description="Client-supplied keys on every write.",
        content=_IDEMPOTENCY_PLAN,
        created_by="demo",
    )
    session.commit()
    review.retire(
        session,
        project=payments,
        plan_file=idempotency,
        reason="the gateway deduplicates now",
        # Explicit roles because these projects have not joined a workspace,
        # and without one `authz` resolves nobody to anything. A local
        # catalog is its own authority; that is what solo means.
        roles=_ROLES,
        actor="demo",
    )

    rate_limiting, _ = create_plan(
        session,
        project=notifications,
        name="rate-limiting",
        description="A token bucket per API key.",
        content=_RATE_LIMIT_PLAN,
        created_by="demo",
    )
    session.commit()

    # A review in progress: a proposal, a comment on it, and one approval.
    proposal = review.propose(
        session,
        project=notifications,
        plan_file=rate_limiting,
        message="Limits agreed with the platform team. Please look.",
        actor="demo",
        roles=_ROLES,
    )
    review.comment(
        session,
        project=notifications,
        plan_file=rate_limiting,
        quote="bursting to 120",
        body="Is the burst per key or per account?",
        actor="reviewer",
        roles=_ROLES,
    )
    review.decide(
        session,
        project=notifications,
        plan_file=rate_limiting,
        proposal_id=proposal.event.event_id,
        action=APPROVE,
        actor="reviewer",
        roles=_ROLES,
    )

    kept, shared, pending = (
        memory_ops.remember(
            session,
            content=spec["content"],
            title=spec["title"],
            category=spec["category"],
            project=payments,
            created_by="demo",
            status="proposed" if index == 2 else "active",
        )[0]
        for index, spec in enumerate(_MEMORIES)
    )
    session.commit()
    memory_ops.promote(session, memory_id=shared.id, workspace_id="ws_demo", created_by="demo")
    session.commit()

    if signed_in:
        _signed_in_session()

    manifest: dict[str, Any] = {
        "home": str(home),
        "signed_in": signed_in,
        "projects": {
            "payments-service": {"id": str(payments.id), "root": str(payments_root)},
            "notifications-service": {
                "id": str(notifications.id),
                "root": str(notifications_root),
            },
        },
        "plans": {
            "jwt-key-rotation": str(jwt.id),
            "webhook-delivery": str(webhooks.id),
            "idempotency-keys": str(idempotency.id),
            "rate-limiting": str(rate_limiting.id),
        },
        "memories": {
            "kept": str(kept.id),
            "shared": str(shared.id),
            "pending": str(pending.id),
        },
    }
    (home / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_manifest(home: Path | str) -> dict[str, Any]:
    """What `seed` wrote beside the database."""
    text = (Path(home) / MANIFEST_FILENAME).read_text(encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded
