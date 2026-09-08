"""Import-boundary enforcement.

Layering contract (see ARCHITECTURE section in README):
- foundation modules import nothing else from the package
- database/storage sit on the foundation only
- server, web, and cli are composition roots; they must not import each other
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "flanner"

FOUNDATION = {
    "exceptions",
    # Announcing that something is going away. Imports only `warnings`, so
    # anything may reach for it without dragging a dependency along.
    "deprecation",
    "utils",
    # What flanner did, recorded locally and never shipped. Imports only the
    # standard library, so any layer may reach for it — including `peer`,
    # which is the surface that answers other machines unattended.
    "observe",
    # Why the control plane said no, as a word a program can match on. Part
    # of the public wire format, so it imports nothing and anything may.
    "refusals",
    "frontmatter",
    "git_integration",
    # Deciding whether a body carries a credential. Pure regex and
    # arithmetic, so it can be tested with a table of strings and reused by
    # every capture path without one of them arranging a database first.
    "memory_guard",
    "jira_utils",
    "linear_utils",
}
ALLOWED = {
    **{m: set() for m in FOUNDATION},
    "database": {"exceptions"},
    "storage": {"exceptions", "frontmatter", "utils"},
    "freshness": {"utils"},
    "ipc": set(),
    # The one import: refusing to mint a second identity for a machine that
    # already has one needs an error a caller can tell apart from "no key".
    "identity": {"exceptions"},
    # Palette and table shapes for the command line. Presentation only,
    # so it imports nothing from the package and nothing may import it
    # except the surfaces that print.
    "tui": set(),
    # A plan rendered as one standalone file. Pure: it is handed the text
    # and returns a string, so it reads no database and touches no network.
    "packet": set(),
    # Where a comment is attached and whether it still holds. Pure text
    # matching over a rendered plan; it renders through packet rather than
    # keeping a third copy of the markdown configuration.
    "anchors": {"packet"},
    # The provider seam: mesh is pure protocol, and only an adapter may
    # know a vendor. No core module may import an adapter (PRD §10.1).
    "mesh": set(),
    "mesh_fake": {"exceptions", "mesh"},
    # The device half of the mesh seam. An adapter, so it may know a
    # vendor; nothing else may import it.
    "mesh_netbird": {"exceptions", "mesh"},
    # The portability suite. Written against the protocol only, so it
    # cannot accidentally encode how one vendor happens to behave.
    "mesh_conformance": {"exceptions", "mesh"},
    "artifacts": {"identity"},
    "entitlements": {"identity", "artifacts"},
    "device_auth": {"identity", "artifacts"},
    # The entitlement cache. No network here on purpose: read commands
    # resolve authorization through it, and an import boundary is a better
    # guarantee than a promise that nobody will call out.
    "session": {"identity", "entitlements"},
    # The only module below the composition roots that may reach the network.
    "account": {"identity", "device_auth", "entitlements", "refusals", "session"},
    "authz": {"workflow", "session", "entitlements", "database", "plan_ops"},
    # Peer transport. Talks to other devices, never to the control plane,
    # so it may not import account any more than a read command may.
    # `assurance` is here because the serving path has to know which plans
    # have been claimed as retired, and duplicating that projection would
    # give two places to disagree about whether a plan is visible. It is a
    # local read over rows already in this database; the reachability test
    # below still proves peer cannot get to `account` through it.
    "peer": {
        "observe",
        "entitlements",
        "identity",
        "sync",
        "device_auth",
        "push",
        "assurance",
        "artifacts",
        # Spending a nonce is part of authorising a request, not a detour
        # through storage: without it the freshness window is the only thing
        # standing between a captured request and a replay of it.
        "replay",
    },
    # Nonce bookkeeping. Reaches the table it writes and the module that
    # defines the window it is sized against, and nothing else.
    "replay": {"database", "device_auth"},
    # The transport carries what peer decides; it never decides anything
    # itself, so it reaches for peer and the device key and nothing else.
    "peer_iroh": {"identity", "peer", "session"},
    "workflow": {"artifacts"},
    "assurance": FOUNDATION
    | {"artifacts", "identity", "workflow", "database", "freshness", "authz"},
    "review": FOUNDATION
    | {
        "workflow",
        "assurance",
        "database",
        "plan_ops",
        "authz",
        # A comment quotes the plan it is attached to, so recording one means
        # reading that version and checking the quotation is really in it.
        "anchors",
        "storage",
    },
    # `plan_ops` because a received plan version has to become a file
    # somebody can open. Same layer, not a reach upward — plan_ops sits on
    # database and storage exactly as sync does, and imports nothing from
    # here, so the cycle test stays quiet. Until this edge existed,
    # "accepted" meant a row in a table and nothing on disk.
    "sync": FOUNDATION | {"artifacts", "database", "plan_ops"},
    "reconcile": FOUNDATION | {"database", "artifacts", "identity"},
    "services": FOUNDATION
    | {"database", "storage", "plan_ops", "linear_api", "agent_hooks", "ipc", "review"},
    "claude_integration": set(),
    "linear_api": {"exceptions", "linear_utils"},
    "server": FOUNDATION
    | {"database", "storage", "freshness", "services", "artifacts", "assurance", "review"},
    # The web UI reads freshness and MCP registration state so the Freshness
    # and Settings pages cannot disagree with what the CLI prints. Both are
    # pure local reads - freshness depends only on utils, claude_integration
    # on nothing - so neither widens the read path toward the network.
    "push": {"artifacts", "sync", "workflow", "database"},
    "web": FOUNDATION
    | {
        "database",
        "storage",
        "plan_ops",
        "ipc",
        "services",
        "freshness",
        "claude_integration",
        # The Mesh and Review pages read the same state the CLI prints.
        # Both are local reads: `session` is a cached file, `review` is a
        # projection over rows already in this database. Neither can reach
        # `account`, which the reachability test below is what guarantees.
        "session",
        "review",
        # Comments are shown against the version on screen, so the page has
        # to ask whether each one still finds its text.
        "anchors",
        # Outside review is read separately from the projection that decides
        # a plan's baseline, and shown separately too.
        "assurance",
        # Reads the key file this machine generated, so the Mesh page can
        # name the device even before it has ever joined a team.
        "identity",
        # The Review page has to say whether a decision would be enforced or
        # is only a rehearsal. That is one question with one answer, and it
        # is answered here, so the page asks rather than guessing from the
        # role map it was handed.
        "authz",
    },
    "agent_hooks": FOUNDATION | {"database"},
    # `identity` so a received version can be told from one written here.
    # Whether an incoming version may move the current-version pointer turns
    # entirely on that, and getting it wrong means either a peer changing
    # what you have open or a received history stuck on its oldest version.
    "plan_ops": FOUNDATION | {"database", "storage", "artifacts", "identity"},
    "cli": FOUNDATION
    | {
        "tui",
        # Writes a plan out as a standalone file, and reads back the notes
        # an outside reviewer returned. Both are local reads of local state.
        "packet",
        "assurance",
        # `review status` resolves each comment against the newest version.
        "anchors",
        "database",
        "storage",
        "server",
        "web",
        "claude_integration",
        "agent_hooks",
        "linear_api",
        "freshness",
        "ipc",
        "reconcile",
        "services",
        "review",
        "session",
        "account",
        "peer",
        # The composition root chooses a mesh implementation, so it
        # names the adapter and the protocol it is typed against.
        "mesh",
        "mesh_netbird",
        # Choosing between transports means naming both of them.
        "peer_iroh",
        # Joining re-roots existing plans, which is a write-path concern.
        "plan_ops",
        "authz",
        "entitlements",
        "identity",
        # `doctor` reports how far this machine's clock is from the server's,
        # and the threshold it compares against is the peer freshness window.
        # Naming the module that owns that rule is better than copying the
        # number into a diagnostic that would then drift from it.
        "device_auth",
    },
    "__main__": {"cli"},
    "__init__": set(),
}


def internal_imports(path: Path) -> set[str]:
    """All flanner-internal modules imported anywhere in the file (incl. inside functions)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0 and node.module is None:  # from . import x, y
                # Relative with no module: a name here is either a
                # submodule or a symbol from __init__ (e.g. __version__).
                # Only the former is a boundary crossing. This whole form was
                # previously invisible, so boundaries could be crossed
                # without the test noticing.
                for alias in node.names:
                    if (PACKAGE / f"{alias.name}.py").exists():
                        found.add(alias.name)
            elif node.level > 0 and node.module:  # from .x import y
                found.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("flanner."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("flanner."):
                    found.add(alias.name.split(".")[1])
    return found


def test_every_module_has_a_boundary_rule():
    modules = {p.stem for p in PACKAGE.glob("*.py")}
    assert modules <= set(ALLOWED), f"add boundary rules for: {modules - set(ALLOWED)}"


def test_import_boundaries_hold():
    violations = []
    for path in PACKAGE.glob("*.py"):
        illegal = internal_imports(path) - ALLOWED[path.stem]
        if illegal:
            violations.append(f"{path.name} imports {sorted(illegal)}")
    assert not violations, "; ".join(violations)


def test_no_read_path_can_reach_the_network():
    """A read command must never make an HTTP call.

    `account` is the one module below the composition roots allowed to
    reach out. Anything that resolves authorization for a read - authz,
    assurance, review - must stay clear of it, transitively. Stated as a
    reachability check rather than a comment, because the tempting shortcut
    when wiring entitlements in is exactly one import away.
    """
    reachable = {}
    for module in ALLOWED:
        path = PACKAGE / f"{module}.py"
        reachable[module] = internal_imports(path) if path.exists() else set()

    def closure(start: str) -> set[str]:
        seen, pending = set(), [start]
        while pending:
            current = pending.pop()
            for dependency in reachable.get(current, set()):
                if dependency not in seen:
                    seen.add(dependency)
                    pending.append(dependency)
        return seen

    for module in ("authz", "assurance", "review", "session", "workflow", "peer"):
        assert "account" not in closure(module), (
            f"{module} can reach the network through account; "
            "a read command would make an HTTP call"
        )


def test_the_version_attribute_matches_the_installed_metadata():
    """`flanner.__version__` was hardcoded and drifted two releases behind
    `pyproject.toml`. It reaches the web UI footer and the settings page, so
    it was wrong on screen, not merely wrong in principle.

    Reading it from installed metadata leaves one source of truth. This
    test fails if anybody hardcodes it again.
    """
    import importlib.metadata

    import flanner

    assert flanner.__version__ == importlib.metadata.version("flanner")


def test_the_version_is_derived_rather_than_typed():
    """The specific mistake: a literal somebody has to remember on release.

    Stated positively. Forbidding the literal outright would also forbid the
    fallback sentinel, which is the one assignment that should stay — and a
    guard that fires on correct code gets deleted rather than heeded.
    """
    source = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    assert "_installed_version(" in source, (
        "__version__ is no longer read from package metadata; it will drift again"
    )
