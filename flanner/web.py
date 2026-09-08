"""
Web interface for Flanner

Provides a browser-based UI for viewing and managing plan files.
"""

import asyncio
import functools
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import markdown
import nh3
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from . import __version__, ipc, services
from .database import (
    PlanFileModel,
    SkillEvalCaseModel,
    SkillModel,
    count_memories,
    create_project,
    delete_project,
    get_linear_config,
    get_linear_links,
    get_plan_file,
    get_project,
    get_session,
    get_version,
    init_database,
    last_received_by_device,
    list_all_linear_links,
    list_artifacts,
    list_versions,
    plan_file_counts_by_project,
    recent_plan_files,
)
from .database import count_plan_files as db_count_plan_files
from .database import count_plan_files_recent as db_count_plan_files_recent
from .database import count_projects as db_count_projects
from .database import list_plan_files as db_list_plan_files
from .database import list_projects as db_list_projects
from .exceptions import DatabaseError
from .freshness import compute_freshness
from .freshness import head_commit as freshness_head
from .freshness import peek as freshness_peek
from .frontmatter import read_managed
from .git_integration import find_git_root, update_gitignore, validate_git_repo
from .linear_utils import generate_linear_issue_url
from .plan_ops import create_plan, record_new_version
from .storage import ensure_plan_directory_exists, load_plan_file
from .utils import format_relative_time, hash_content

# Initialize FastAPI app
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Flanner", description="Manage plan files with automatic versioning", version=__version__
)

# Get paths
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Setup Jinja2 templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Ensure database is initialized
def ensure_db() -> None:
    """Ensure database is initialized"""
    try:
        get_session()
    except DatabaseError:
        # path resolution is env-aware in init_database
        init_database()


# Tags/attributes kept when sanitizing rendered markdown. Everything markdown
# produces (including codehilite's span/class and heading ids) is allowed; the
# sanitizer strips <script>, event handlers, javascript: URLs, and <style>.
_SANITIZE_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_SANITIZE_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title"},
    "code": {"class"},
    "span": {"class"},
    "pre": {"class"},
    "div": {"class"},
    "h1": {"id"},
    "h2": {"id"},
    "h3": {"id"},
    "h4": {"id"},
    "h5": {"id"},
    "h6": {"id"},
    "td": {"align"},
    "th": {"align"},
}


# Template filters
def markdown_filter(text: str | None) -> str:
    """Render markdown to sanitized HTML.

    The output is inserted with ``|safe``, so it is run through nh3 to strip any
    raw HTML that could execute (scripts, event handlers, javascript: URLs) while
    keeping the formatting and code-highlighting markup markdown emits.
    """
    if not text:
        return ""

    md = markdown.Markdown(
        extensions=["fenced_code", "codehilite", "tables", "toc", "nl2br"],
        extension_configs={"codehilite": {"css_class": "highlight", "linenums": False}},
    )
    return nh3.clean(md.convert(text), tags=_SANITIZE_TAGS, attributes=_SANITIZE_ATTRS)


# Add custom filters to Jinja2
templates.env.filters["markdown"] = markdown_filter
templates.env.filters["relative_time"] = format_relative_time
templates.env.filters["basename"] = lambda p: Path(p).name


# Stamp static assets so the browser refetches when they change. The newest
# mtime under static/ means an edit-then-restart busts the cache even within a
# release (the version string alone would not, since it only moves on release).
def _asset_version() -> str:
    try:
        newest = max(f.stat().st_mtime for f in STATIC_DIR.rglob("*") if f.is_file())
        return f"{__version__}-{int(newest)}"
    except ValueError:
        return __version__


templates.env.globals["asset_version"] = _asset_version()
# Sidebar counts default to absent, so a page that cannot count (an error
# page, say) renders the nav without numbers instead of failing.
templates.env.globals["nav_projects"] = None
templates.env.globals["nav_plans"] = None
templates.env.globals["nav_attention"] = 0
templates.env.globals["nav_signed_in"] = False
templates.env.globals["nav_peers"] = 0
templates.env.globals["nav_review"] = 0
templates.env.globals["app_version"] = __version__
# Stamped once at import. A footer year that re-read the clock on every
# render would be the only thing on the page that could change without the
# page changing, and nobody is running this process across New Year.
templates.env.globals["app_year"] = datetime.now(timezone.utc).year


# --- requests that came from another website --------------------------------
#
# This UI binds 127.0.0.1 and has no login, so "only you can reach it" was
# carrying the whole security argument. That is not true inside a browser.
# Any page you have open can submit a form to http://localhost:8080, and the
# browser sends it with your cookies. Same-origin policy blocks *reading* the
# reply; it does not block making the request. Before this, a page on any
# site could delete every project here, and a domain pointed at 127.0.0.1
# (DNS rebinding) could read them first to find the ids.
#
# Two checks on the request itself. No token threaded through templates, no
# session to keep, nothing for a form to forget.

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HOSTS_ENV = "FLANNER_WEB_HOSTS"

# Set by `flanner web` when it was told to bind somewhere other than
# loopback, and readable by anyone running uvicorn directly. A flag on the
# module rather than a write into os.environ: a command that mutates the
# environment leaks into everything else in the process, which is exactly
# how a test that only wanted the warning turned the check off for the
# whole suite.
ALLOW_ANY_HOST = False


def _hostname(value: str) -> str:
    """The host part of a Host header or an Origin netloc, without the port."""
    if value.startswith("["):  # bracketed IPv6, e.g. [::1]:8080
        return value.partition("]")[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def _host_is_allowed(host_header: str) -> bool:
    """Whether a Host header names this machine, or one the operator allowed.

    Loopback names always pass. Beyond that, `FLANNER_WEB_HOSTS` names the
    hosts an operator serves under (or `*` for any), for a deployment that
    runs uvicorn directly rather than through `flanner web`.
    """
    allowed = os.environ.get(_HOSTS_ENV, "")
    if ALLOW_ANY_HOST or allowed.strip() == "*":
        return True
    extra = {h.strip() for h in allowed.split(",") if h.strip()}
    return _hostname(host_header) in (_LOOPBACK | extra)


@app.middleware("http")
async def block_foreign_requests(request: Request, call_next: Any) -> Response:
    """Refuse requests a browser made on another site's behalf.

    An `Origin` that disagrees with `Host` means a page somewhere else asked
    for this. Browsers have sent `Origin` on cross-origin form posts for
    years, so this is the check that matters. A request with no `Origin` at
    all is allowed: that is curl, or the MCP server calling `/ipc/call`, and
    neither is a browser carrying somebody's cookies.
    """
    host = request.headers.get("host", "")
    if not _host_is_allowed(host):
        return PlainTextResponse(
            "Refused: this address is not one flanner serves. "
            "It is a local tool, reachable as localhost.",
            status_code=403,
        )

    origin = request.headers.get("origin")
    if origin and request.method in _UNSAFE_METHODS:
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc != host:
            return PlainTextResponse(
                f"Refused: this request came from {origin}, not from flanner.",
                status_code=403,
            )

    result: Response = await call_next(request)
    return result


_STATUS_LABELS = {400: "Bad Request", 404: "Not Found", 500: "Server Error"}


@app.exception_handler(HTTPException)
async def html_error_pages(request: Request, exc: HTTPException) -> Response:
    """Browsers get a styled error page; /api/* callers keep JSON."""
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "status_code": exc.status_code,
            "status_label": _STATUS_LABELS.get(exc.status_code, "Error"),
            "detail": exc.detail,
        },
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def html_validation_pages(request: Request, exc: RequestValidationError) -> Response:
    """Bad query/form input (e.g. ?page=abc) gets a styled 400, not raw 422 JSON."""
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "status_code": 400,
            "status_label": "Bad Request",
            "detail": "That request had an invalid value. Check the address and try again.",
        },
        status_code=400,
    )


@app.exception_handler(Exception)
async def html_crash_page(request: Request, exc: Exception) -> Response:
    """Unexpected failures: log the traceback, never show one to the user."""
    logger.exception("Unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "request": request,
            "status_code": 500,
            "status_label": "Server Error",
            "detail": "Something went wrong on our side. The details are in the server log.",
        },
        status_code=500,
    )


# Rendering guard: markdown.convert on multi-MB documents takes seconds and,
# called from an async route, would freeze the event loop for every client.
MAX_RENDER_CHARS = 1_000_000
PAGE_SIZE = 50

# Rendered-HTML cache keyed by content hash; versions are immutable so a
# hash hit can never be stale. The in-process OrderedDict LRU is the decided
# size rather than a placeholder for a bigger one: a shared or persistent
# cache only pays across processes, and a local single-user UI has none.
_RENDER_CACHE_MAX = 64
_render_cache: OrderedDict[str, str] = OrderedDict()


def render_plan_html(content: str, content_hash: str | None) -> str:
    """Markdown -> HTML with an LRU cache on the version's content hash."""
    key = content_hash or hash_content(content)
    if key in _render_cache:
        _render_cache.move_to_end(key)
        return _render_cache[key]
    html = markdown_filter(content)
    _render_cache[key] = html
    if len(_render_cache) > _RENDER_CACHE_MAX:
        _render_cache.popitem(last=False)
    return html


# =============================================================================
# HTML PAGES
# =============================================================================


# --- the mesh -----------------------------------------------------------------
#
# Everything the CLI prints for `flanner whoami` and `flanner peer status`,
# read from the same places: the cached session file and the entitlement it
# holds. All local. Nothing here makes a network call, which is why the page
# renders instantly and works offline.


def _retirement_view(session: Any, plan_file: Any) -> dict[str, Any] | None:
    """The banner a retired plan carries, or None when it is not retired."""
    from .assurance import retirement

    standing = retirement(session, str(plan_file.id))
    if not standing.retired:
        return None
    return {"by": standing.by, "reason": standing.reason, "at": standing.at}


def _storage_view(session: Any) -> dict[str, Any]:
    """What this device is holding, and the fact that it never prunes.

    Shown because "keep everything" is a decision, and a decision nobody can
    see the cost of is one they never really made. There is no cleanup
    button: history is the point of an append-only store, and a control
    that quietly broke lineage would be worse than a growing number.
    """
    rows = list_artifacts(session)
    payload_bytes = sum(len(r.payload or "") for r in rows)
    plan_bytes = _local_plan_bytes(session)
    return {
        "artifacts": len(rows),
        "payload": _bytes_label(payload_bytes),
        "plans": _bytes_label(plan_bytes),
        "total": _bytes_label(payload_bytes + plan_bytes),
    }


def _hidden(session: Any) -> set[str]:
    """Plans claimed as retired, for every listing and every count.

    Hidden, not gone. The artifacts are all still here and the plan comes
    back the moment somebody restores it; this is a page honouring a claim,
    which is the strongest thing an append-only store can offer.

    Counts take the same set as the lists they describe, or the sidebar
    ends up asserting a number the page beneath it does not show.
    """
    from .assurance import retired_plan_ids

    return retired_plan_ids(session)


def _visible_plans(session: Any, project_id: Any) -> list[Any]:
    """A project's plans, minus any claimed as retired."""
    return db_list_plan_files(session, project_id, exclude=_hidden(session))


def _local_plan_bytes(session: Any) -> int:
    """How much plan text this device is holding, on disk.

    Exists to give the "nothing is uploaded" claim a denominator. A bare
    "0 B held by flanner" is true but unmeasured, and a number nobody
    computed reads as decoration; beside a real figure for what is here, it
    says something.

    Current versions only, not every revision: the question is how much of
    your work this is about, not how much history the store has kept.
    """
    total = 0
    for plan_file in session.query(PlanFileModel).all():
        version = get_version(session, plan_file.id, plan_file.current_version)
        if version is None:
            continue
        try:
            total += Path(version.file_path).stat().st_size
        except OSError:
            # A plan whose file has moved still counts as zero rather than
            # blanking the page. This is a stat tile, not an integrity check.
            continue
    return total


def _bytes_label(count: int) -> str:
    """Bytes as something a person reads, at one decimal place."""
    if count < 1024:
        return f"{count} B"
    for unit in ("KB", "MB", "GB"):
        count_f = count / 1024
        if count_f < 1024 or unit == "GB":
            return f"{count_f:.1f} {unit}"
        count = int(count_f)
    return f"{count} B"


def _mesh_state(session: Any = None) -> dict[str, Any]:
    """This device's identity, account, access and known peers.

    ``session`` is optional so the page still renders before this device has
    a catalog; without it the peer list simply carries no arrival times.
    """
    from . import identity as device_identity
    from . import session as cache

    state: dict[str, Any] = {
        "device_id": device_identity.device_id(),
        "signed_in": False,
    }
    current = cache.load()
    if current is None:
        return state

    # The verdict carries the parsed claims when the token could be read at
    # all, so a malformed entitlement still renders a page saying so rather
    # than raising.
    verdict = current.status()
    claims = verdict.claims

    state.update(
        {
            "signed_in": True,
            "user_id": current.user_id,
            "organization_id": current.organization_id,
            "endpoint": current.endpoint,
            "relay_url": current.relay_url,
            "status": verdict.status,
            "reason": verdict.reason,
            # A peer is a device this one already holds a public key for.
            # Without the key there is nothing to verify, so the keyring is
            # the honest definition of "who this machine can sync with".
            "peers": sorted(current.device_keys or {}),
            # When work signed by each device last reached this one. Not
            # "when they were last online": an artifact can arrive relayed
            # through a third machine long after its author went away, and
            # dressing that up as a liveness light would be a claim the
            # data cannot support.
            "last_received": last_received_by_device(session) if session is not None else {},
            "local_bytes": _bytes_label(_local_plan_bytes(session)) if session is not None else "",
            "workspaces": sorted(current.keyring or {}),
        }
    )
    if claims is not None:
        state["plan"] = claims.plan
        state["features"] = list(claims.features)
        state["expires_at"] = claims.expires_at
        state["grants"] = [
            {"workspace_id": c.workspace_id, "role": c.role} for c in claims.workspace_capabilities
        ]
    return state


def _comments(session: Any, plan_file: Any, body: str) -> list[dict[str, Any]]:
    """Teammates' notes, each with whether it still finds its text.

    Resolved against the version being shown, not the one it was written
    on, because that is the question a reader has: does this note still
    apply to what is in front of me?
    """
    from .anchors import AMBIGUOUS, EXACT, MOVED, STRANDED, Anchor, resolve
    from .assurance import load_comments

    said = {
        EXACT: ("anchored", "fresh"),
        MOVED: ("the text around it changed", "aging"),
        AMBIGUOUS: ("quoted text appears several times", "aging"),
        STRANDED: ("lost its place", "stale"),
    }
    out: list[dict[str, Any]] = []
    for event in load_comments(session, str(plan_file.id)):
        payload = event.payload
        raw = payload.get("anchor") or {}
        state = resolve(Anchor.from_dict(raw), body)
        label, tone = said[state.status]
        out.append(
            {
                "by": str(event.actor or "unknown"),
                "quote": str(raw.get("quote") or ""),
                "body": str(payload.get("body") or ""),
                "version": payload.get("target_version"),
                "state": state.status,
                "label": label,
                "tone": tone,
                "anchored": state.anchored,
                "matched": state.matched,
            }
        )
    return out


def _external_notes(session: Any, plan_file: Any) -> list[dict[str, Any]]:
    """Notes a reviewer outside the mesh sent back, flattened for display.

    Read through `load_external_reviews` rather than the review projection,
    because these must never move a plan's accepted baseline. Every one is
    marked unverified: the reviewer had no device key, so the only signature
    involved says which device received the notes, not who wrote them.
    """
    from .assurance import load_external_reviews

    out: list[dict[str, Any]] = []
    for event in load_external_reviews(session, str(plan_file.id)):
        payload = event.payload
        who = str(payload.get("reviewer") or "an unnamed reviewer")
        for note in payload.get("notes") or []:
            out.append(
                {
                    "reviewer": who,
                    "quote": str(note.get("quote") or ""),
                    "body": str(note.get("body") or ""),
                    "at": str(note.get("at") or ""),
                    "version": payload.get("target_version"),
                    "source": str(payload.get("source") or "packet"),
                }
            )
    return out


def _review_rows(session: Any) -> list[dict[str, Any]]:
    """Every plan that has a review event, worst first.

    Plans nobody has proposed a change to are left out: a list of everything
    would bury the handful that need a decision.
    """
    from . import authz
    from . import review as review_module
    from .assurance import load_comments

    rows: list[dict[str, Any]] = []
    for project in db_list_projects(session):
        # Resolved once per project and passed down, so the badge and the
        # state it labels come from the same answer. Letting `status`
        # resolve it again would put two calls behind one row, and a page
        # that disagreed with itself about who may decide is worse than a
        # page that does not say.
        authorization = authz.resolve(project)
        for plan_file in _visible_plans(session, project.id):
            try:
                state = review_module.status(
                    session,
                    plan_file=plan_file,
                    project=project,
                    roles=authorization.roles,
                )
            except Exception:  # noqa: BLE001 - one bad plan must not blank the page
                logger.warning("could not project review state for %s", plan_file.id)
                continue
            outside = _external_notes(session, plan_file)
            comments = load_comments(session, str(plan_file.id))
            if (
                not state.proposals
                and not state.pending
                and not state.rejected
                and not outside
                and not comments
            ):
                continue
            rows.append(
                {
                    "plan_file": plan_file,
                    "project": project,
                    "pending": list(state.pending),
                    "rejected": [{"id": i, "why": why} for i, why in state.rejected],
                    "conflicted": state.conflicted,
                    "accepted": state.accepted_artifact_id,
                    "outside": outside,
                    # Counted, not resolved. Whether each one still finds its
                    # text is a per-version question, and answering it here
                    # would mean rendering every plan in the database to
                    # draw one list.
                    "comments": len(comments),
                    # Solo projects run the workflow against a local role
                    # map anyone can edit, so a decision here is a rehearsal
                    # rather than an authorization. The page has to say
                    # which one it is showing; a reader cannot tell from an
                    # accepted baseline alone.
                    "enforced": authorization.enforced,
                    "advisory_reason": authorization.reason,
                }
            )
    rows.sort(key=lambda r: (not r["conflicted"], -len(r["pending"]), -r["comments"]))
    return rows


def _nav(session: Any) -> dict[str, Any]:
    """Counts the sidebar shows on every page.

    Cheap aggregates in SQL. The attention count is what the Freshness
    badge reports, and it is deliberately the same number the page itself
    lists — a badge that disagrees with the page it links to is worse than
    no badge.
    """
    from . import session as cache
    from .assurance import count_review_subjects

    held = cache.load()
    return {
        "nav_projects": db_count_projects(session),
        "nav_plans": db_count_plan_files(session, exclude=_hidden(session)),
        # Deliberately not computed here. It is the only number in this
        # context that costs git, and every page carries it, so it set the
        # floor under every response — including pages that show no
        # freshness. The sidebar renders a placeholder and app.js fills it
        # in from /nav/attention once the page is up.
        "nav_attention": None,
        # Zero when this machine has no account, which is the normal state
        # and the reason the whole Team group hides itself in that case.
        "nav_signed_in": held is not None,
        "nav_peers": len(held.device_keys or {}) if held else 0,
        "nav_review": count_review_subjects(session),
        # Cheap, unlike freshness: memory counts are two indexed
        # queries and touch no repository, so this one can be here.
        "nav_memory": count_memories(session),
        "nav_memory_pending": count_memories(session, status="proposed"),
        # From the catalog, not a fresh scan: this is on every page, and a
        # scan hashes every file in every package. Zero until the first scan.
        "nav_skills": session.query(SkillModel).filter_by(effective=True).count(),
    }


def _plan_freshness(
    session: Any, plan_file: Any, *, head: str | None = None
) -> dict[str, Any] | None:
    """Freshness for a plan's latest version, or None if it cannot be judged."""
    version = get_version(session, plan_file.id, None)
    if version is None:
        return None
    project = get_project(session, plan_file.project_id)
    root = project.project_root if project else None
    if not root:
        return None

    # Ask the cache before opening the file. The version row already holds a
    # hash of its content, which is exactly what the cache is keyed on, so a
    # hit costs neither a read nor a git process.
    if head is None:
        head = freshness_head(root)
    body_id = version.content_hash or ""
    record = freshness_peek(root, body_id, version.created_at, head=head) if body_id else None
    if record is None:
        try:
            body = read_managed(Path(version.file_path).read_text(encoding="utf-8"))[1]
        except (OSError, ValueError):
            return None
        record = compute_freshness(
            root, body, version.created_at, head=head, body_id=body_id or None
        )
    record["plan_file"] = plan_file
    record["project"] = project
    record["version"] = version
    return record


def _freshness_for(project: Any, body: str, version: Any) -> dict[str, Any] | None:
    """Evidence for one already-loaded version. None when git cannot judge it."""
    root = project.project_root if project else None
    if not root:
        return None
    try:
        return compute_freshness(root, body, version.created_at)
    except Exception:  # noqa: BLE001 - a plan must render even if git is odd
        return None


def _needs_attention(session: Any, request: Any = None) -> list[dict[str, Any]]:
    """Every plan that is not fresh, worst first.

    Ordered by evidence rather than by date, because a plan edited this
    morning can already be wrong and one from March can still be true.

    ``request`` memoizes the answer for the life of one request. The
    freshness page asked for this twice — once for the sidebar badge and
    once for the table under it — and each pass walks every plan.
    """
    if request is not None:
        cached = getattr(request.state, "attention", None)
        if cached is not None:
            return list(cached)

    rank = {"stale": 0, "suspect": 1, "aging": 2}
    out: list[dict[str, Any]] = []
    for project in db_list_projects(session):
        # One rev-parse for the repository rather than one per plan.
        head = freshness_head(project.project_root) if project.project_root else None
        for plan_file in _visible_plans(session, project.id):
            record = _plan_freshness(session, plan_file, head=head)
            if record and record["status"] in rank:
                out.append(record)
    out.sort(key=lambda r: (rank[r["status"]], -len(r.get("reasons") or [])))
    # A number the client-side sort control can order by. Worst is highest, so
    # "most drifted" is a descending sort like every other column.
    for row in out:
        row["drift_rank"] = len(rank) - rank[row["status"]]
    if request is not None:
        request.state.attention = list(out)
    return out


def _freshness_mix(session: Any, projects: Any) -> dict[Any, dict[str, int]]:
    """How each project's plans are distributed across the four statuses."""
    out: dict[Any, dict[str, int]] = {}
    for project in projects:
        tally = {"fresh": 0, "aging": 0, "suspect": 0, "stale": 0}
        # One rev-parse per repository, not one per plan.
        head = freshness_head(project.project_root) if project.project_root else None
        for plan_file in _visible_plans(session, project.id):
            record = _plan_freshness(session, plan_file, head=head)
            if record:
                tally[record["status"]] = tally.get(record["status"], 0) + 1
        if any(tally.values()):
            out[project.id] = tally
    return out


def _attention_count(session: Any) -> int:
    try:
        return len(_needs_attention(session))
    except Exception:  # noqa: BLE001 - a badge must never break a page
        return 0


@app.get("/projects/freshness-mix")
async def projects_freshness_mix(request: Request) -> dict[str, dict[str, int]]:
    """The freshness column on /projects, fetched after the page is up.

    Same reasoning as /nav/attention: it is a walk over every plan in every
    project, and it was the reason the projects page was the slowest in the
    UI while showing a column most visits never read.
    """
    ensure_db()
    session = get_session()
    projects = db_list_projects(session)
    mix = await run_in_threadpool(_freshness_mix, session, projects)
    return {str(pid): counts for pid, counts in mix.items()}


@app.get("/nav/attention")
async def nav_attention(request: Request) -> dict[str, int]:
    """How many plans need attention, fetched after the page is up.

    Its own address because it is the one number in the sidebar that costs
    git. Computing it inline put a repository walk in front of the first
    byte of every page, including pages with no freshness on them.
    """
    ensure_db()
    session = get_session()
    return {"count": await run_in_threadpool(_attention_count, session)}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard - show all projects"""
    ensure_db()
    session = get_session()

    # Aggregates in SQL; loading every plan file to count them is O(rows)
    # in Python and an N+1 query per project.
    total_projects = db_count_projects(session)
    total_plans = db_count_plan_files(session, exclude=_hidden(session))
    updated_this_week = db_count_plan_files_recent(session, days=7, exclude=_hidden(session))
    plan_counts = plan_file_counts_by_project(session, exclude=_hidden(session))

    projects = db_list_projects(session, limit=12)

    recent_activity: list[dict[str, Any]] = [
        {"project": pf.project, "plan_file": pf, "updated_at": pf.updated_at}
        for pf in recent_plan_files(session, limit=10, exclude=_hidden(session))
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **_nav(session),
            "plan_counts": plan_counts,
            "request": request,
            "projects": projects,
            "total_projects": total_projects,
            "total_plans": total_plans,
            "updated_this_week": updated_this_week,
            "recent_activity": recent_activity,
        },
    )


@app.get("/projects", response_class=HTMLResponse)
async def projects_list(
    request: Request, page: int = 1, message: str | None = None, sort: str = "updated"
) -> HTMLResponse:
    """List projects, a page at a time"""
    ensure_db()
    session = get_session()
    success = {"deleted": "Project deleted."}.get(message or "")
    # Anything unrecognised falls back rather than erroring: this arrives from
    # a query string, and a bookmarked ?sort=nonsense should still render.
    sort = sort if sort in ("updated", "name") else "updated"

    total = db_count_projects(session)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    projects = db_list_projects(session, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, sort=sort)
    plan_counts = plan_file_counts_by_project(session, exclude=_hidden(session))

    # Not computed here. See _nav's attention badge: this is the same walk,
    # every plan in every project, several git processes each. It made the
    # projects page the slowest in the UI while showing a column most
    # visits do not read. The page arrives first and the column fills in.
    #
    # The freshness mix per project, which is the column the design leads
    # with. Computed off the request thread: it reads files and shells out
    # to git, and a slow repo should not block the event loop.

    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            **_nav(session),
            "request": request,
            "projects": projects,
            "plan_counts": plan_counts,
            "total_projects": total,
            "total_plans": db_count_plan_files(session, exclude=_hidden(session)),
            "updated_this_week": db_count_plan_files_recent(
                session, days=7, exclude=_hidden(session)
            ),
            "recent_activity": [
                {"plan_file": pf, "project": pf.project, "updated_at": pf.updated_at}
                for pf in recent_plan_files(session, limit=5, exclude=_hidden(session))
            ],
            "page": page,
            "pages": pages,
            "total_pages": pages,
            "total": total,
            "sort": sort,
            "success": success,
        },
    )


@app.get("/projects/new", response_class=HTMLResponse)
async def new_project_form(request: Request) -> HTMLResponse:
    """Show create project form"""
    return templates.TemplateResponse(request, "project_new.html", {"request": request})


def _new_project_error(request: Request, session: Any, error: str, **fields: Any) -> HTMLResponse:
    """Re-render the new-project form with what went wrong.

    `fields` carries back what was typed. A form that clears itself on a
    validation error makes the person retype work the server already has.
    """
    return templates.TemplateResponse(
        request,
        "project_new.html",
        {**_nav(session), "request": request, "error": error, **fields},
    )


@app.post("/projects/new")
async def create_project_post(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    project_root: str | None = Form(None),
    plan_directory: str = Form(".plans"),
) -> Response:
    """Create a new project"""
    ensure_db()
    session = get_session()

    if not project_root or project_root.strip() == "":
        project_root = find_git_root(os.getcwd())
        if not project_root:
            return _new_project_error(
                request,
                session,
                "Could not find git repository. Please specify project root manually.",
            )

    if not validate_git_repo(project_root):
        return _new_project_error(
            request, session, f"{project_root} is not a valid git repository"
        )

    try:
        project = create_project(
            session,
            name=name,
            description=description,
            project_root=project_root,
            plan_directory=plan_directory,
            auto_gitignore=True,
        )
    except ValueError as e:
        return _new_project_error(
            request,
            session,
            str(e),
            name=name,
            description=description,
            project_root=project_root,
            plan_directory=plan_directory,
        )

    ensure_plan_directory_exists(project_root, plan_directory)
    update_gitignore(project_root, plan_directory.rstrip("/") + "/", comment="MCP Plan Manager")
    return RedirectResponse(url=f"/projects/{project.id}", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: str, page: int = 1) -> HTMLResponse:
    """Show project detail with a page of plan files"""
    ensure_db()
    session = get_session()

    try:
        project_uuid = UUID(project_id)
        project = get_project(session, project_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    total = db_count_plan_files(session, project_uuid, exclude=_hidden(session))
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    plan_files = db_list_plan_files(
        session, project_uuid, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )

    # Count Linear links per plan so the list can mark linked plans.
    linear_counts: dict[str, int] = {}
    for row in list_all_linear_links(session, project_uuid):
        key = str(row["plan_file_id"])
        linear_counts[key] = linear_counts.get(key, 0) + 1

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            **_nav(session),
            "request": request,
            "project": project,
            "plan_files": plan_files,
            "page": page,
            "pages": pages,
            "total": total,
            "linear_counts": linear_counts,
        },
    )


@app.post("/projects/{project_id}/delete")
async def delete_project_post(project_id: str) -> RedirectResponse:
    """Delete a project and all associated plan files"""
    ensure_db()
    session = get_session()

    try:
        project_uuid = UUID(project_id)
        project = get_project(session, project_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Delete project (cascade deletes plan files and versions)
    if delete_project(session, project_uuid):
        return RedirectResponse(url="/projects?message=deleted", status_code=303)
    else:
        raise HTTPException(status_code=500, detail="Failed to delete project")


@app.get("/projects/{project_id}/plans/new", response_class=HTMLResponse)
async def new_plan_form(request: Request, project_id: str) -> HTMLResponse:
    """Show create plan file form"""
    ensure_db()
    session = get_session()

    try:
        project_uuid = UUID(project_id)
        project = get_project(session, project_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return templates.TemplateResponse(
        request, "plan_new.html", {**_nav(session), "request": request, "project": project}
    )


@app.post("/projects/{project_id}/plans/new")
async def create_plan_post(
    request: Request,
    project_id: str,
    name: str = Form(...),
    description: str = Form(""),
    content: str = Form(...),
) -> Response:
    """Create a new plan file"""
    ensure_db()
    session = get_session()

    try:
        project_uuid = UUID(project_id)
        project = get_project(session, project_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if not project.project_root:
        # Previously crashed with TypeError (HTTP 500); surface the config problem instead
        raise HTTPException(status_code=400, detail="Project has no project_root configured")

    # Create plan record and initial version through the shared write path
    try:
        plan_file, _ = create_plan(
            session,
            project=project,
            name=name,
            content=content,
            description=description,
            created_by="user",
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "plan_new.html",
            {
                **_nav(session),
                "request": request,
                "project": project,
                "error": str(e),
                "name": name,
                "description": description,
                "content": content,
            },
        )

    return RedirectResponse(url=f"/plans/{plan_file.id}", status_code=303)


@app.get("/plans/{plan_file_id}/download")
async def plan_download(plan_file_id: str, version: int | None = None) -> FileResponse:
    """Send one version of a plan as a file.

    The button used to point straight at the absolute path recorded in the
    database, which is not a URL. The browser asked this server for a path
    beginning with a drive letter, and got a 404 every time.

    The path is resolved from the version row rather than taken from the
    request, so this cannot be talked into serving something else.
    """
    ensure_db()
    session = get_session()
    try:
        plan_file_uuid = UUID(plan_file_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan file ID") from None

    plan_file = get_plan_file(session, plan_file_uuid)
    if not plan_file:
        raise HTTPException(status_code=404, detail="Plan file not found")
    version_obj = get_version(session, plan_file_uuid, version)
    if not version_obj:
        raise HTTPException(status_code=404, detail="Version not found")

    path = Path(version_obj.file_path)
    if not path.is_file():
        # The row can outlive the file: someone moved or deleted it on disk.
        raise HTTPException(status_code=404, detail="That version is no longer on disk")

    return FileResponse(
        path,
        media_type="text/markdown",
        filename=f"{plan_file.name}_v{version_obj.version}.md",
    )


def _linear_links_for(session: Any, plan_file_uuid: UUID, project_id: Any) -> list[dict[str, Any]]:
    """The issues this plan is linked to, ready for the template.

    Title and state are whatever the last link or refresh cached, because
    reading them live would put a network call on every page render.
    """
    config = get_linear_config(session, project_id)
    return [
        {
            "issue_id": link.linear_issue_id,
            "title": link.issue_title,
            "state": link.issue_state,
            "notes": link.notes,
            "url": generate_linear_issue_url(config.workspace, link.linear_issue_id)
            if config
            else None,
        }
        for link in get_linear_links(session, plan_file_uuid)
    ]


@app.get("/plans/{plan_file_id}", response_class=HTMLResponse)
async def plan_view(
    request: Request,
    plan_file_id: str,
    version: int | None = None,
    message: str | None = None,
) -> HTMLResponse:
    """View a plan file (specific version or latest)"""
    ensure_db()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
        plan_file = get_plan_file(session, plan_file_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan file ID") from None

    if not plan_file:
        raise HTTPException(status_code=404, detail="Plan file not found")

    project = get_project(session, plan_file.project_id)
    version_obj = get_version(session, plan_file_uuid, version)
    if not version_obj:
        raise HTTPException(
            status_code=404, detail=f"Version {version if version else 'latest'} not found"
        )
    all_versions = list_versions(session, plan_file_uuid)
    linear_links = _linear_links_for(session, plan_file_uuid, plan_file.project_id)

    try:
        frontmatter_data, body = load_plan_file(version_obj.file_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"File not found at {version_obj.file_path}"
        ) from None

    # Render off the event loop; a large document must not stall other clients.
    render_capped = len(body) > MAX_RENDER_CHARS
    content_html = (
        ""
        if render_capped
        else await run_in_threadpool(render_plan_html, body, version_obj.content_hash)
    )

    return templates.TemplateResponse(
        request,
        "plan_view.html",
        {
            **_nav(session),
            "request": request,
            "project": project,
            "plan_file": plan_file,
            "version": version_obj,
            "all_versions": all_versions,
            "frontmatter": frontmatter_data,
            "linear_links": linear_links,
            "content": body,
            "content_html": content_html,
            "render_capped": render_capped,
            "content_chars": len(body),
            # Freshness for the version being shown, so the page can say why
            # it is judged the way it is rather than only that it is.
            "freshness": _freshness_for(project, body, version_obj),
            # The page still renders for a retired plan; a link somebody
            # saved should explain itself rather than 404. The banner is
            # what makes the difference visible.
            "retirement": _retirement_view(session, plan_file),
            "comments": _comments(session, plan_file, body),
            "outside_notes": _external_notes(session, plan_file),
            "info": {
                "no_changes": "No changes detected - the content matches the current version, "
                "so a new version was not created."
            }.get(message or ""),
        },
    )


@app.get("/plans/{plan_file_id}/edit", response_class=HTMLResponse)
async def plan_edit(request: Request, plan_file_id: str) -> HTMLResponse:
    """Edit a plan file (creates new version)"""
    ensure_db()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
        plan_file = get_plan_file(session, plan_file_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan file ID") from None

    if not plan_file:
        raise HTTPException(status_code=404, detail="Plan file not found")

    # Get project
    project = get_project(session, plan_file.project_id)

    # Get latest version
    version_obj = get_version(session, plan_file_uuid)
    if not version_obj:
        raise HTTPException(status_code=404, detail="No versions found")

    # Load file content
    try:
        frontmatter_data, body = load_plan_file(version_obj.file_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found") from None

    return templates.TemplateResponse(
        request,
        "plan_edit.html",
        {
            **_nav(session),
            "request": request,
            "project": project,
            "plan_file": plan_file,
            "version": version_obj,
            "content": body,
        },
    )


@app.post("/plans/{plan_file_id}/edit")
async def plan_update(
    request: Request, plan_file_id: str, content: str = Form(...), notes: str = Form("")
) -> RedirectResponse:
    """Update a plan file (creates new version)"""
    ensure_db()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
        plan_file = get_plan_file(session, plan_file_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan file ID") from None

    if not plan_file:
        raise HTTPException(status_code=404, detail="Plan file not found")

    # Get project
    project = get_project(session, plan_file.project_id)
    if not project:
        # Previously crashed with AttributeError (HTTP 500); explicit 404 instead
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.project_root:
        # Previously crashed with TypeError (HTTP 500); surface the config problem instead
        raise HTTPException(status_code=400, detail="Project has no project_root configured")

    # Get latest version
    latest_version = get_version(session, plan_file_uuid)
    if not latest_version:
        # Previously crashed with AttributeError (HTTP 500); explicit 404 instead
        raise HTTPException(status_code=404, detail="No versions found")

    # Check if content changed
    new_hash = hash_content(content)
    if latest_version.content_hash == new_hash:
        # No changes, redirect back to view
        return RedirectResponse(url=f"/plans/{plan_file_id}?message=no_changes", status_code=303)

    # Create new version (locked: refreshes, picks the next free number, commits)
    record_new_version(
        session,
        project=project,
        plan_file=plan_file,
        content=content,
        created_by="user",
        notes=notes,
    )

    return RedirectResponse(url=f"/plans/{plan_file_id}", status_code=303)


@app.get("/plans/{plan_file_id}/history", response_class=HTMLResponse)
async def plan_history(request: Request, plan_file_id: str) -> HTMLResponse:
    """View version history of a plan file"""
    ensure_db()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
        plan_file = get_plan_file(session, plan_file_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan file ID") from None

    if not plan_file:
        raise HTTPException(status_code=404, detail="Plan file not found")

    # Get project
    project = get_project(session, plan_file.project_id)

    # Get all versions
    versions = list_versions(session, plan_file_uuid)

    return templates.TemplateResponse(
        request,
        "plan_history.html",
        {
            **_nav(session),
            "request": request,
            "project": project,
            "plan_file": plan_file,
            "versions": versions,
        },
    )


# =============================================================================
# API ENDPOINTS (JSON responses for AJAX)
# =============================================================================


def _plans_to_judge(session: Any) -> list[tuple[Any, str | None]]:
    """Every visible plan, paired with its repository's current commit."""
    out: list[tuple[Any, str | None]] = []
    for project in db_list_projects(session):
        head = freshness_head(project.project_root) if project.project_root else None
        for plan_file in _visible_plans(session, project.id):
            out.append((plan_file, head))
    return out


# --- what changed, and telling the page about it ----------------------------
#
# The catalog has several writers and they are separate processes: this UI,
# `flanner peer serve` taking a push from a teammate, the MCP server acting
# for an agent, a `flanner sync` in a terminal. None of them are in this
# process's call stack, so nothing here can be notified in-process.
#
# So the question asked is "did anything change?" rather than "did somebody
# remember to tell me?". Two indexed queries, once a second, shared by every
# open tab. It cannot miss a writer, including one added later.

#: How often the catalog is checked. Fast enough to feel immediate, slow
#: enough that an idle browser tab costs almost nothing.
LIVE_POLL_SECONDS = 1.0

#: How long one connection lasts before the browser is asked to make another.
#:
#: A stream with no end relies entirely on noticing the client has gone, and
#: a client that vanishes without closing cleanly would otherwise leave this
#: polling the database forever. Ending on purpose costs a reconnect the
#: browser performs by itself, which is the property SSE was chosen for.
LIVE_STREAM_MAX_SECONDS = 300.0


def _catalog_snapshot(session: Any) -> dict[str, str]:
    """A signature per plan: enough to tell what changed, and nothing else.

    `updated_at` moves when a plan is revised here, `current_version` when a
    baseline is accepted, and the version count when one merely *arrives*
    from a peer — which deliberately does not move the pointer, and so would
    be invisible to the other two.

    One entry is not a plan: "mesh" covers the cached session, so gaining a
    peer registers as a change even though no plan moved.
    """
    from . import session as session_cache
    from .database import PlanFileModel, VersionModel

    # Who this device can sync with is not in the database: the peer keyring
    # lives in the cached session, written by `flanner login` and by a key
    # exchange. The mesh page reads it, so a change to it has to count as a
    # change to something.
    try:
        stat = session_cache.session_path().stat()
        mesh = f"{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        mesh = "none"

    counts = dict(
        session.query(VersionModel.plan_file_id, func.count(VersionModel.id))
        .group_by(VersionModel.plan_file_id)
        .all()
    )
    rows = session.query(
        PlanFileModel.id, PlanFileModel.updated_at, PlanFileModel.current_version
    ).all()
    signatures = {
        str(pid): f"{updated}|{current}|{counts.get(pid, 0)}" for pid, updated, current in rows
    }
    signatures["mesh"] = mesh
    return signatures


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Server-sent events: which plans changed, as they change.

    Server-sent rather than the newline-delimited json the freshness scan
    uses, because this stream is open-ended. It lives as long as the tab, and
    reconnecting after a sleep or a dropped connection is the browser's job
    rather than something to hand-roll — which is exactly the property that
    made SSE the wrong fit for a scan that ends.
    """
    ensure_db()
    session = get_session()

    async def stream() -> Any:
        seen = await run_in_threadpool(_catalog_snapshot, session)
        # Named so a reconnecting browser is told the stream is alive before
        # anything has changed, rather than sitting on a silent socket.
        yield "event: ready\ndata: {}\n\n"

        deadline = time.monotonic() + LIVE_STREAM_MAX_SECONDS
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return
            await asyncio.sleep(LIVE_POLL_SECONDS)
            try:
                now = await run_in_threadpool(_catalog_snapshot, session)
            except Exception:  # noqa: BLE001 - a dropped poll is not a dead stream
                logger.exception("could not read the catalog for live updates")
                continue

            added = sorted(set(now) - set(seen))
            removed = sorted(set(seen) - set(now))
            changed = sorted(k for k in now.keys() & seen.keys() if now[k] != seen[k])
            if added or removed or changed:
                seen = now
                payload = json.dumps({"added": added, "removed": removed, "changed": changed})
                yield f"event: catalog\ndata: {payload}\n\n"
            else:
                # A comment frame. Keeps the connection warm and lets the
                # server notice a browser that went away without saying so.
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/plans/{plan_id}/revision")
async def plan_revision(plan_id: str) -> dict[str, Any]:
    """The version a plan is on now, for a page deciding whether it is stale."""
    ensure_db()
    session = get_session()
    try:
        plan_file = get_plan_file(session, UUID(plan_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="No such plan") from None
    if plan_file is None:
        raise HTTPException(status_code=404, detail="No such plan")
    return {"version": plan_file.current_version, "name": plan_file.name}


@app.get("/freshness/stream")
async def freshness_stream(request: Request) -> StreamingResponse:
    """The drift table, a row at a time, as each plan is judged.

    Judging one plan means several git processes, and judging all of them
    took about seven seconds against a real store with a cold cache. That
    was seven seconds of blank page. The work is the same; what changes is
    that the first answer arrives in a few hundred milliseconds and the rest
    land as they come, with a count of what is left.

    Rows are rendered from the same partial the page uses, and sent as
    html. Building them in the script would be a second copy of the markup.

    Newline-delimited json: one object per line, so a reader can act on
    each without waiting for the end.
    """
    ensure_db()
    session = get_session()
    rank = {"stale": 0, "suspect": 1, "aging": 2}
    row_template = templates.get_template("_freshness_row.html")

    async def lines() -> Any:
        items = await run_in_threadpool(_plans_to_judge, session)
        yield json.dumps({"total": len(items)}) + "\n"

        tally = {"fresh": 0, "aging": 0, "suspect": 0, "stale": 0}
        for plan_file, head in items:
            # Off the event loop: this shells out to git, and a slow
            # repository must not stall every other request in the process.
            record = await run_in_threadpool(
                functools.partial(_plan_freshness, session, plan_file, head=head)
            )
            if record is None:
                yield json.dumps({"judged": 1}) + "\n"
                continue
            status = record["status"]
            tally[status] = tally.get(status, 0) + 1
            if status in rank:
                record["drift_rank"] = len(rank) - rank[status]
                yield (
                    json.dumps(
                        {
                            "judged": 1,
                            "drift": record["drift_rank"],
                            "html": row_template.render(row=record),
                        }
                    )
                    + "\n"
                )
            else:
                yield json.dumps({"judged": 1}) + "\n"

        yield json.dumps({"done": True, "tally": tally}) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        # Nothing may sit on this and hand it over in one piece; the whole
        # point is that the first row arrives before the last is computed.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/freshness", response_class=HTMLResponse)
async def freshness_page(request: Request, full: int = 0) -> HTMLResponse:
    """Which plans have stopped being true, and the evidence for saying so.

    Renders a shell and lets the stream fill it, unless `?full=1` — which
    computes everything first and is what the noscript link points at.
    """
    ensure_db()
    session = get_session()
    attention: list[dict[str, Any]] = []
    tally = {"fresh": 0, "aging": 0, "suspect": 0, "stale": 0}

    if full:
        attention = await run_in_threadpool(_needs_attention, session, request)
        for plan_file, head in await run_in_threadpool(_plans_to_judge, session):
            record = await run_in_threadpool(
                functools.partial(_plan_freshness, session, plan_file, head=head)
            )
            if record:
                tally[record["status"]] = tally.get(record["status"], 0) + 1

    return templates.TemplateResponse(
        request,
        "freshness.html",
        {
            "streaming": not full,
            # `_nav` rather than three hand-picked counts: this page was
            # supplying its own subset, so the peer count and the signed-in
            # flag fell back to their defaults and the sidebar quietly lost
            # entries whenever somebody opened it.
            **_nav(session),
            "request": request,
            "attention": attention,
            "tally": tally,
        },
    )


@app.get("/mesh", response_class=HTMLResponse)
async def mesh_page(request: Request) -> HTMLResponse:
    """This device's place in the mesh, and who it can sync with.

    The same facts `flanner whoami` and `flanner peer status` print, read
    from the same cached session. No network call, so it renders offline and
    tells the truth about a machine that has been disconnected for a week.
    """
    ensure_db()
    session = get_session()
    return templates.TemplateResponse(
        request,
        "mesh.html",
        {**_nav(session), "request": request, "mesh": _mesh_state(session)},
    )


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request) -> HTMLResponse:
    """Plans with a proposal waiting on somebody."""
    ensure_db()
    session = get_session()
    rows = await run_in_threadpool(_review_rows, session)
    return templates.TemplateResponse(
        request,
        "review.html",
        {**_nav(session), "request": request, "rows": rows},
    )


@app.get("/skills", response_class=HTMLResponse)
async def skills_page(
    request: Request,
    scope: str = "",
    shadowed: str = "",
    agent: str = "claude-code",
    days: int = 30,
    said: str = "",
) -> HTMLResponse:
    """Every skill this machine's agents would load, and what is wrong with them.

    Scans on request rather than reading the catalog. A skill package is a
    directory somebody else owns and edits without telling us, so a page
    served from the last scan would be confidently out of date — which is
    the one failure this page exists to catch.
    """
    ensure_db()
    session = get_session()
    from . import skills_ops

    root = find_git_root(str(Path.cwd()))
    packages = await run_in_threadpool(skills_ops.scan, Path(root) if root else None, agent)
    await run_in_threadpool(skills_ops.record, session, packages)
    report = skills_ops.report(Path(root) if root else None, agent, packages=packages)

    rows = [
        pkg
        for pkg in report["packages"]
        if (shadowed or pkg["effective"]) and (not scope or pkg["scope"] == scope)
    ]

    from . import skills_manage, skills_observe

    here = Path(root) if root else None
    return templates.TemplateResponse(
        request,
        "skills.html",
        {
            **_nav(session),
            "request": request,
            "report": report,
            "rows": rows,
            "scope": scope,
            "shadowed": bool(shadowed),
            "said": said,
            "observe": await run_in_threadpool(skills_observe.status, session),
            "usage": await run_in_threadpool(skills_observe.usage, session, here, days),
            "days": days,
            "stored": await run_in_threadpool(skills_manage.stored),
            "installs": await run_in_threadpool(skills_manage.installations, session, here),
        },
    )


@app.post("/skills/observe")
async def skills_observe_form(
    request: Request,
    action: str = Form(...),
    agent: str = Form("claude-code"),
    retention_days: int = Form(30),
) -> RedirectResponse:
    """Turn observation on or off for the project this server is serving.

    Installs and removes the hook as well as flipping the flag. Leaving a
    hook behind after switching off would keep starting a flanner process
    on every skill invocation to be told it is not wanted.
    """
    ensure_db()
    session = get_session()
    from . import skills_observe
    from .agent_hooks import ensure_observe_hook, remove_observe_hook

    root = find_git_root(str(Path.cwd()))
    if root is None:
        return RedirectResponse("/skills?said=not+a+repository", status_code=303)

    try:
        if action == "enable":
            await run_in_threadpool(
                skills_observe.enable, session, Path(root), agent, retention_days
            )
            await run_in_threadpool(ensure_observe_hook, root)
            said = "watching+this+project"
        else:
            await run_in_threadpool(skills_observe.disable, session, Path(root), agent)
            await run_in_threadpool(remove_observe_hook, root)
            said = "stopped+watching"
    except ValueError as error:
        return RedirectResponse(f"/skills?said={quote(str(error))}", status_code=303)
    return RedirectResponse(f"/skills?said={said}", status_code=303)


@app.post("/skills/purge")
async def skills_purge_form(request: Request, scope: str = Form("project")) -> RedirectResponse:
    """Delete recorded skill uses. Only ever when asked."""
    ensure_db()
    session = get_session()
    from . import skills_observe

    root = find_git_root(str(Path.cwd()))
    where = Path(root) if (root and scope == "project") else None
    gone = await run_in_threadpool(skills_observe.purge, session, where)
    return RedirectResponse(
        f"/skills?said=deleted+{gone['deleted']}+recorded+use(s)", status_code=303
    )


@app.post("/skills/rollback")
async def skills_rollback_form(
    request: Request, installation_id: str = Form(...)
) -> RedirectResponse:
    """Put back what an install replaced."""
    ensure_db()
    session = get_session()
    from . import skills_manage

    try:
        await run_in_threadpool(skills_manage.rollback, session, installation_id, None)
    except (ValueError, OSError) as error:
        return RedirectResponse(f"/skills?said={quote(str(error))}", status_code=303)
    return RedirectResponse("/skills?said=rolled+back", status_code=303)


@app.get("/skills/proposals", response_class=HTMLResponse)
async def skills_proposals_page(
    request: Request, open_id: str = "", suite: str = "", said: str = ""
) -> HTMLResponse:
    """Skills waiting on a person, and the comparisons behind them.

    Declared before `/skills/{...}` would be, and reached from the Skills
    page rather than the sidebar: it is the same area, and a second rail
    entry for an inbox that is empty on most machines earns nothing.
    """
    ensure_db()
    session = get_session()
    from . import skills_eval, skills_learn
    from .database import get_project_by_root

    root = find_git_root(str(Path.cwd()))
    project = get_project_by_root(session, root) if root else None
    if project is None:
        return templates.TemplateResponse(
            request,
            "skills_proposals.html",
            {
                **_nav(session),
                "request": request,
                "rows": [],
                "open": None,
                "clusters": [],
                "matrix": None,
                "suites": [],
                "suite": "",
                "said": said,
                "no_project": True,
            },
        )

    rows = await run_in_threadpool(skills_learn.proposals, session, project, None)
    opened = None
    if open_id:
        try:
            opened = await run_in_threadpool(skills_learn.review, session, open_id, "")
        except ValueError:
            opened = None

    kept = await run_in_threadpool(skills_learn.evidence, session, project)
    suites = sorted({c.suite for c in session.query(SkillEvalCaseModel).all()})
    grid = await run_in_threadpool(skills_eval.matrix, session, suite) if suite in suites else None
    return templates.TemplateResponse(
        request,
        "skills_proposals.html",
        {
            **_nav(session),
            "request": request,
            "rows": rows,
            "open": opened,
            "clusters": await run_in_threadpool(skills_learn.cluster, kept),
            "matrix": grid,
            "suites": suites,
            "suite": suite,
            "said": said,
            "no_project": False,
        },
    )


@app.post("/skills/proposals/decide")
async def skills_decide_form(
    request: Request,
    proposal_id: str = Form(...),
    decision: str = Form(...),
    note: str = Form(""),
) -> RedirectResponse:
    """Approve or reject one exact draft, from the page that showed it.

    The approval binds to the draft's hash, so this cannot approve
    anything but the text that was on screen.
    """
    ensure_db()
    session = get_session()
    from . import skills_learn

    try:
        await run_in_threadpool(
            skills_learn.decide, session, proposal_id, decision, actor="web", note=note
        )
    except ValueError as error:
        return RedirectResponse(f"/skills/proposals?said={quote(str(error))}", status_code=303)
    return RedirectResponse(
        f"/skills/proposals?open_id={proposal_id}&said={decision}", status_code=303
    )


@app.post("/skills/proposals/revise")
async def skills_revise_form(
    request: Request, proposal_id: str = Form(...), body: str = Form(...)
) -> RedirectResponse:
    """Edit a draft, which puts it back in review.

    Editing after approval is meant to invalidate the approval. That is
    the whole reason an approval names a hash rather than a row.
    """
    ensure_db()
    session = get_session()
    from . import skills_learn

    try:
        await run_in_threadpool(skills_learn.revise, session, proposal_id, body)
    except ValueError as error:
        return RedirectResponse(f"/skills/proposals?said={quote(str(error))}", status_code=303)
    return RedirectResponse(
        f"/skills/proposals?open_id={proposal_id}&said=revised", status_code=303
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """What this install is configured to do. Read-mostly by design.

    Anything that would change a project belongs to that project's page;
    this is the machine-wide view.
    """
    ensure_db()
    session = get_session()
    # The same source `flanner claude-info` reads, so the CLI and the UI
    # can never disagree about what is installed.
    from .claude_integration import get_claude_config_info
    from .database import get_db_path

    try:
        info = get_claude_config_info()
    except Exception:  # noqa: BLE001 - a settings page must still render
        info = {}
    claude = {
        "registered": bool(info.get("server_registered")),
        "state": "registered" if info.get("server_registered") else "not registered",
        "config_path": info.get("config_path"),
        "servers": info.get("total_servers"),
    }
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "db_path": get_db_path(),
            "port": request.url.port or 8080,
            "version": __version__,
            "claude": claude,
            "storage": _storage_view(session),
            **_nav(session),
        },
    )


@app.get("/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request) -> HTMLResponse:
    """Issue trackers a plan can be linked to."""
    ensure_db()
    session = get_session()

    # Links are held per project, so gather them across all of them.
    links: list[dict[str, Any]] = []
    config = None
    for project in db_list_projects(session):
        links.extend(list_all_linear_links(session, project.id))
        config = config or get_linear_config(session, project.id)

    return templates.TemplateResponse(
        request,
        "integrations.html",
        {
            "linear_links": links,
            "linear_config": config,
            **_nav(session),
        },
    )


# --- memory ------------------------------------------------------------------


@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request, q: str = "", status: str = "active") -> HTMLResponse:
    """Everything remembered on this machine, searchable.

    One page for both browsing and searching, because the difference is a
    filled-in box and splitting them would mean two places that decide what
    a memory row looks like.
    """
    ensure_db()
    session = get_session()
    from . import memory_ops
    from .database import list_memories as db_list_memories

    if q:
        # `search_all`, not `recall`. Recall's scope is a boundary for an
        # agent; this page already lists every project's memories beside
        # the box, and a search that returned fewer than the list shows
        # would read as broken.
        rows = await run_in_threadpool(
            functools.partial(memory_ops.search_all, session, query=q, limit=50)
        )
        reason = True
    else:
        rows = [
            {
                "id": str(m.id),
                "title": m.title,
                "summary": m.body[:240],
                "category": m.category,
                "scope": m.scope,
                "confidence": m.confidence,
                "created_by": m.created_by,
                "created_at": m.created_at.isoformat() + "Z" if m.created_at else None,
                "match_reason": "",
            }
            for m in await run_in_threadpool(
                functools.partial(
                    db_list_memories, session, status=None if status == "all" else status
                )
            )
        ]
        reason = False

    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "rows": rows,
            "query": q,
            "status": status,
            "show_reason": reason,
            "summary": await run_in_threadpool(memory_ops.summary, session),
            **_nav(session),
        },
    )


@app.get("/memory/pending", response_class=HTMLResponse)
async def memory_pending_page(request: Request) -> HTMLResponse:
    """Suggestions waiting on a decision.

    Declared before `/memory/{memory_id}` so "pending" is read as the page
    it is rather than as an id that does not parse.
    """
    ensure_db()
    session = get_session()
    from . import memory_ops

    project = await run_in_threadpool(memory_ops.resolve_project, session)
    waiting = await run_in_threadpool(
        functools.partial(memory_ops.pending, session, project_id=project.id if project else None)
    )
    policy = await run_in_threadpool(memory_ops.policy_for, project)

    return templates.TemplateResponse(
        request,
        "memory_pending.html",
        {
            "rows": waiting,
            "capture_mode": policy.capture_mode,
            **_nav(session),
        },
    )


@app.post("/memory/pending/decide")
async def memory_decide_form(
    request: Request,
    memory_id: str = Form(...),
    decision: str = Form(...),
    supersede: str = Form(""),
) -> RedirectResponse:
    """Approve or reject one suggestion from the page."""
    ensure_db()
    from .services import dispatch

    await run_in_threadpool(
        functools.partial(
            dispatch,
            "memory_decide",
            {
                "memory_id": memory_id,
                "decision": decision,
                "supersede_conflict": bool(supersede),
                "created_by": "web",
            },
        )
    )
    return RedirectResponse("/memory/pending", status_code=303)


@app.post("/memory/{memory_id}/share")
async def memory_share_form(memory_id: str) -> RedirectResponse:
    """Share one memory with the workspace this project joined.

    A button rather than a checkbox on the write form, because sharing is a
    decision taken after the fact and often by somebody rereading what they
    wrote. Personal memory has no such button: the refusal lives in the
    domain, and offering a control that always fails would be worse.
    """
    ensure_db()
    from .services import dispatch

    result = await run_in_threadpool(
        functools.partial(dispatch, "memory_share", {"memory_id": memory_id, "created_by": "web"})
    )
    return RedirectResponse(
        f"/memory/{memory_id}?said={quote(str(result.get('message', '')))}", status_code=303
    )


@app.post("/memory/{memory_id}/withdraw")
async def memory_withdraw_form(memory_id: str, reason: str = Form("")) -> RedirectResponse:
    """Ask peers to stop recalling a shared memory."""
    ensure_db()
    from .services import dispatch

    result = await run_in_threadpool(
        functools.partial(
            dispatch,
            "memory_withdraw",
            {"memory_id": memory_id, "reason": reason, "created_by": "web"},
        )
    )
    return RedirectResponse(
        f"/memory/{memory_id}?said={quote(str(result.get('message', '')))}", status_code=303
    )


@app.get("/memory/{memory_id}", response_class=HTMLResponse)
async def memory_detail(request: Request, memory_id: str, said: str = "") -> HTMLResponse:
    """One memory, its provenance and everything that happened to it."""
    ensure_db()
    session = get_session()
    from . import memory_ops

    try:
        detail = await run_in_threadpool(memory_ops.describe, session, UUID(memory_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="No such memory") from None
    except Exception:
        raise HTTPException(status_code=404, detail="No such memory") from None

    return templates.TemplateResponse(
        request,
        "memory_detail.html",
        {
            "memory": detail,
            "said": said,
            "attachments": await run_in_threadpool(
                memory_ops.attachments_of, session, UUID(memory_id)
            ),
            **_nav(session),
        },
    )


@app.get("/memory/{memory_id}/attachments/{attachment_id}")
async def memory_attachment(memory_id: str, attachment_id: str) -> FileResponse:
    """Serve one attached file.

    The type is the one detected when the file was stored, never one
    guessed from its name, so a `.png` that is really something else is
    served as what it is. `nosniff` stops a browser second-guessing that,
    and the file downloads rather than rendering: this is a local tool, and
    a file somebody added is not something to run inside the page.
    """
    ensure_db()
    session = get_session()
    from . import memory_ops

    try:
        path, mime, name = await run_in_threadpool(
            memory_ops.open_attachment, session, UUID(attachment_id)
        )
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="No such attachment") from None
    except Exception:
        raise HTTPException(status_code=404, detail="No such attachment") from None

    return FileResponse(
        path,
        media_type=mime,
        filename=name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/plans", response_class=HTMLResponse)
async def plans_page(request: Request) -> HTMLResponse:
    """Every plan across every project, newest first."""
    ensure_db()
    session = get_session()
    rows = []
    for plan_file in recent_plan_files(session, limit=200, exclude=_hidden(session)):
        rows.append({"plan_file": plan_file, "project": plan_file.project})
    return templates.TemplateResponse(request, "plans.html", {"rows": rows, **_nav(session)})


@app.get("/api/projects")
async def api_list_projects() -> list[dict[str, Any]]:
    """API: List all projects"""
    ensure_db()
    session = get_session()

    projects = db_list_projects(session)

    return [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "project_root": p.project_root,
            "plan_directory": p.plan_directory,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "plan_files_count": len(p.plan_files),
        }
        for p in projects
    ]


@app.get("/api/search")
async def api_search_index() -> list[dict[str, str]]:
    """Flat index of projects and plans for the command palette."""
    ensure_db()
    session = get_session()
    items: list[dict[str, str]] = []
    # The palette is a listing too. A retired plan reachable by typing its
    # name would make the hiding look like a bug rather than a decision.
    hidden = _hidden(session)
    for p in db_list_projects(session):
        items.append(
            {"type": "project", "name": p.name, "context": "", "url": f"/projects/{p.id}"}
        )
        for pf in (x for x in p.plan_files if str(x.id) not in hidden):
            items.append(
                {"type": "plan", "name": pf.name, "context": p.name, "url": f"/plans/{pf.id}"}
            )
    return items


@app.get("/api/projects/{project_id}/plans")
async def api_list_plan_files(project_id: str) -> list[dict[str, Any]]:
    """API: List plan files for a project"""
    ensure_db()
    session = get_session()

    try:
        project_uuid = UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None

    if not get_project(session, project_uuid):
        raise HTTPException(status_code=404, detail="Project not found")

    plan_files = _visible_plans(session, project_uuid)

    return [
        {
            "id": str(pf.id),
            "name": pf.name,
            "description": pf.description,
            "current_version": pf.current_version,
            "updated_at": pf.updated_at.isoformat() if pf.updated_at else None,
        }
        for pf in plan_files
    ]


@app.get("/api/plans/{plan_file_id}")
async def api_get_plan(plan_file_id: str, version: int | None = None) -> dict[str, Any]:
    """API: Get plan file content"""
    ensure_db()
    session = get_session()

    try:
        plan_file_uuid = UUID(plan_file_id)
        plan_file = get_plan_file(session, plan_file_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid plan file ID") from None

    if not plan_file:
        raise HTTPException(status_code=404, detail="Plan file not found")

    # Get version
    version_obj = get_version(session, plan_file_uuid, version)
    if not version_obj:
        raise HTTPException(status_code=404, detail="Version not found")

    # Load content
    try:
        frontmatter_data, body = load_plan_file(version_obj.file_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found") from None

    return {
        "plan_file": {
            "id": str(plan_file.id),
            "name": plan_file.name,
            "description": plan_file.description,
            "current_version": plan_file.current_version,
        },
        "version": {
            "version": version_obj.version,
            "created_by": version_obj.created_by,
            "created_at": version_obj.created_at.isoformat() if version_obj.created_at else None,
            "notes": version_obj.notes,
        },
        "content": body,
        "frontmatter": frontmatter_data,
    }


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str) -> dict[str, Any]:
    """API: Delete a project"""
    ensure_db()
    session = get_session()

    try:
        project_uuid = UUID(project_id)
        project = get_project(session, project_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project ID") from None

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_name = project.name
    plan_files_count = len(project.plan_files)

    # Delete project
    if delete_project(session, project_uuid):
        return {
            "success": True,
            "message": f"Project '{project_name}' deleted successfully",
            "plan_files_deleted": plan_files_count,
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to delete project")


# ---------------------------------------------------------------------------
# Daemon IPC (PRD Phase 1). When this app is the long-running local process it
# is the single writer: other processes forward their write operations here
# instead of mutating shared state themselves. Operations are looked up in the
# shared service registry, so the daemon and an in-process caller run exactly
# the same code. The token is supplied by the CLI through FLANNER_IPC_TOKEN;
# without it IPC is off.
# ---------------------------------------------------------------------------


def _require_ipc_token(request: Request) -> None:
    expected = os.environ.get(ipc.TOKEN_ENV)
    if not expected:
        raise HTTPException(status_code=503, detail="IPC not enabled")
    supplied = request.headers.get("X-Flanner-Token", "")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid IPC token")


@app.post("/ipc/call")
async def ipc_call(request: Request) -> JSONResponse:
    """Run one shared write operation on behalf of another process.

    Any non-200 response means the operation did not run, which is what lets
    the caller safely fall back to executing locally. A failure *inside* an
    operation is therefore reported as a 200 carrying an error payload, never
    as a 500 that a caller might retry and thereby apply twice.
    """
    _require_ipc_token(request)
    ensure_db()
    body = await request.json()
    op = str(body.get("op", ""))
    args = body.get("args") or {}
    fn = services.REGISTRY.get(op)
    if fn is None or not isinstance(args, dict):
        raise HTTPException(status_code=422, detail=f"Unknown IPC operation: {op}")
    try:
        return JSONResponse({"result": fn(**args)})
    except TypeError as e:  # bad arguments for this operation
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception as e:
        logger.exception("IPC operation %s failed", op)
        return JSONResponse({"result": {"error": True, "message": str(e)}})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)  # local-only tool, no auth layer
