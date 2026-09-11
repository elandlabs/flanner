"""Every operation flanner offers, and which surface offers it.

One list of the actions a person or an agent can take: the CLI command, the
MCP tool and the web route for each, what kind of act it is, and, where an
agent cannot call it, why not. tests/test_operations.py checks it against
the running CLI, web app and MCP server, so it cannot name a command, route
or tool that does not exist, or miss one that does.

The marketing site's capability tables were written by hand and drifted.
They called every Skills operation CLI-only while the web UI already
adopts, shares, rolls back and installs skills, and they kept a tool count
from before integrations were switched off. `python -m flanner.operations`
prints this list as JSON, so the site reads it rather than retyping it.

Imports nothing from the package, so any layer may read it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

#: What kind of act an operation is, from asking least to asking most of
#: whoever takes it.
ACCESS = ("read", "write", "approve", "share", "destructive", "admin")

#: The order the site groups operations in.
DOMAINS = ("projects", "plans", "review", "memory", "skills", "mesh", "local", "integrations")

#: Said where an operation is deliberately left for a later release rather
#: than kept from agents on principle. Worded once, so the two cases read
#: differently everywhere they appear.
NOT_YET = "Not offered to agents yet."


@dataclass(frozen=True)
class Operation:
    domain: str
    action: str
    access: str
    cli: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    web: tuple[str, ...] = ()
    #: Required whenever `mcp` is empty. The tests refuse a silent gap.
    why_not_mcp: str = ""
    #: The feature flag this sits behind, if any.
    gated: str = ""
    #: What a caller needs to know that the columns do not say.
    note: str = ""


def _op(
    domain: str,
    action: str,
    access: str,
    *,
    cli: tuple[str, ...] = (),
    mcp: tuple[str, ...] = (),
    web: tuple[str, ...] = (),
    why: str = "",
    gated: str = "",
    note: str = "",
) -> Operation:
    return Operation(domain, action, access, cli, mcp, web, why, gated, note)


OPERATIONS: tuple[Operation, ...] = (
    # --- projects ------------------------------------------------------------
    _op(
        "projects",
        "Find out where you are",
        "read",
        mcp=("project_context",),
        note="The project, whether review is enforced, what memory keeps, and what is switched on.",
    ),
    _op(
        "projects",
        "Adopt a repository",
        "write",
        cli=("init",),
        mcp=("initialize_project_tool", "create_project_tool"),
        web=("GET /projects/new", "POST /projects/new"),
    ),
    _op(
        "projects",
        "See every project",
        "read",
        cli=("list",),
        mcp=("list_projects",),
        web=("GET /", "GET /projects", "GET /projects/{project_id}"),
    ),
    _op(
        "projects",
        "Confirm where plans go and how they are headed",
        "read",
        mcp=("get_plan_config",),
    ),
    _op(
        "projects",
        "Change a project's settings",
        "write",
        cli=("config",),
        mcp=("configure_project_tool",),
    ),
    _op(
        "projects",
        "Delete a project",
        "destructive",
        cli=("delete",),
        mcp=("delete_project_tool",),
        web=("POST /projects/{project_id}/delete", "DELETE /api/projects/{project_id}"),
    ),
    _op(
        "projects",
        "Keep plan files out of git",
        "write",
        cli=("setup-gitignore",),
        why="Edits .gitignore. Adopting a repository already does it, where you see it happen.",
    ),
    # --- plans ---------------------------------------------------------------
    _op(
        "plans",
        "Create a plan",
        "write",
        mcp=("create_plan_file_tool",),
        web=("GET /projects/{project_id}/plans/new", "POST /projects/{project_id}/plans/new"),
    ),
    _op(
        "plans",
        "Revise a plan",
        "write",
        mcp=("update_plan_file_tool",),
        web=("GET /plans/{plan_file_id}/edit", "POST /plans/{plan_file_id}/edit"),
    ),
    _op(
        "plans",
        "Read a plan",
        "read",
        mcp=("get_plan_file_tool",),
        web=("GET /plans/{plan_file_id}", "GET /plans/{plan_file_id}/download"),
    ),
    _op(
        "plans",
        "Browse plans",
        "read",
        mcp=("list_plan_files_tool",),
        web=("GET /plans",),
    ),
    _op(
        "plans",
        "See a plan's history",
        "read",
        cli=("history",),
        mcp=("get_plan_history_tool",),
        web=("GET /plans/{plan_file_id}/history",),
    ),
    _op(
        "plans",
        "Compare two versions",
        "read",
        cli=("diff",),
        why=f"{NOT_YET} An agent can read both versions with get_plan_file_tool.",
    ),
    _op(
        "plans",
        "Check whether a plan still matches the code",
        "read",
        cli=("freshness", "why"),
        mcp=("get_plan_freshness_tool",),
        web=("GET /freshness",),
    ),
    _op(
        "plans",
        "Import plan files already on disk",
        "write",
        cli=("sync",),
        why="Imports a whole directory at once, which is worth watching happen.",
    ),
    _op(
        "plans",
        "Retire a plan",
        "share",
        cli=("retire",),
        why="Hides the plan for everyone who honours the claim, so a person decides.",
    ),
    _op(
        "plans",
        "Search projects and plans",
        "read",
        web=("GET /api/search",),
        why="The web command palette's search. An agent reads plans with the plan tools.",
    ),
    # --- review --------------------------------------------------------------
    _op(
        "review",
        "Propose a version for review",
        "write",
        cli=("review propose",),
        mcp=("propose_plan_revision_tool",),
    ),
    _op(
        "review",
        "Approve, reject, request changes or withdraw",
        "approve",
        cli=("review decide",),
        mcp=("record_plan_review_decision_tool",),
        note=(
            "The decision is recorded as whoever this device is signed in as. Where "
            "review is enforced, an agent may not approve: a person does, at the CLI."
        ),
    ),
    _op(
        "review",
        "See proposals and the accepted baseline",
        "read",
        cli=("review status",),
        mcp=("get_plan_workflow_status_tool",),
        web=("GET /review",),
    ),
    _op(
        "review",
        "Check a plan is safe to implement",
        "read",
        mcp=("get_plan_assurance_tool",),
    ),
    _op(
        "review",
        "Comment on a quotation",
        "write",
        cli=("review comment",),
        why=f"{NOT_YET} A note against quoted text is a person's review action today.",
    ),
    _op(
        "review",
        "Export a plan for outside review, and take the notes back",
        "share",
        cli=("review pack", "review import"),
        why="Sends a plan outside the team and brings notes back in, so a person decides.",
    ),
    # --- memory --------------------------------------------------------------
    _op(
        "memory",
        "Save a memory",
        "write",
        cli=("mem remember",),
        mcp=("memory_remember",),
    ),
    _op(
        "memory",
        "Search memories",
        "read",
        cli=("mem recall",),
        mcp=("memory_recall",),
        web=("GET /memory",),
    ),
    _op(
        "memory",
        "Read one memory in full",
        "read",
        cli=("mem show",),
        mcp=("memory_get",),
        web=("GET /memory/{memory_id}",),
    ),
    _op(
        "memory",
        "Browse memories",
        "read",
        cli=("mem list",),
        mcp=("memory_list",),
    ),
    _op(
        "memory",
        "Correct a memory",
        "write",
        cli=("mem supersede",),
        mcp=("memory_supersede",),
    ),
    _op(
        "memory",
        "Stop recalling a memory",
        "write",
        cli=("mem forget",),
        mcp=("memory_forget",),
    ),
    _op(
        "memory",
        "Suggest a memory for approval",
        "write",
        mcp=("memory_consider",),
        note="What an agent noticed itself. It waits for a person unless policy keeps it.",
    ),
    _op(
        "memory",
        "See suggestions waiting",
        "read",
        cli=("mem pending",),
        mcp=("memory_pending",),
        web=("GET /memory/pending",),
    ),
    _op(
        "memory",
        "Approve or reject a suggestion",
        "approve",
        cli=("mem approve", "mem reject"),
        mcp=("memory_decide",),
        web=("POST /memory/pending/decide",),
        note=(
            "An agent relays what the user decided. Categories the policy says a person "
            "must approve are refused through the agent."
        ),
    ),
    _op(
        "memory",
        "Read the capture policy",
        "read",
        cli=("mem policy show", "mem policy validate"),
        mcp=("memory_policy",),
    ),
    _op(
        "memory",
        "Write a policy file",
        "admin",
        cli=("mem policy init",),
        why="The file decides what your agent may keep. The agent must not write its own limits.",
    ),
    _op(
        "memory",
        "Change the capture mode",
        "admin",
        cli=("mem mode",),
        why="Decides how much your agent may capture. Letting it widen its own permission defeats the setting.",
    ),
    _op(
        "memory",
        "Attach or remove a file",
        "write",
        cli=("mem attach", "mem detach"),
        mcp=("memory_attach", "memory_detach"),
    ),
    _op(
        "memory",
        "Open an attachment",
        "read",
        cli=("mem open",),
        web=("GET /memory/{memory_id}/attachments/{attachment_id}",),
        why="Opens a file in your own applications, which only makes sense where you are.",
    ),
    _op(
        "memory",
        "Share a memory with the team",
        "share",
        cli=("mem share",),
        mcp=("memory_share",),
        web=("POST /memory/{memory_id}/share",),
    ),
    _op(
        "memory",
        "Ask peers to stop recalling a shared memory",
        "share",
        cli=("mem withdraw",),
        mcp=("memory_withdraw",),
        web=("POST /memory/{memory_id}/withdraw",),
    ),
    _op(
        "memory",
        "Bring back a forgotten memory",
        "write",
        cli=("mem restore",),
        why="Reverses a deliberate decision, which is yours to make.",
    ),
    _op(
        "memory",
        "Rebuild the index from the memory files",
        "destructive",
        cli=("mem rebuild",),
        why="A repair over your whole store. It belongs where you can watch it run.",
    ),
    _op(
        "memory",
        "Delete unreferenced attachments",
        "destructive",
        cli=("mem gc",),
        why="Removes files from disk. Nothing that deletes runs unattended.",
    ),
    # --- skills --------------------------------------------------------------
    _op(
        "skills",
        "See what loads and what is wrong",
        "read",
        cli=("skills scan", "skills list", "skills inspect", "skills doctor"),
        mcp=("skills_report",),
        web=("GET /skills", "GET /skills/{name}"),
        note="The agent's read records nothing. `flanner skills scan` records by default.",
    ),
    _op(
        "skills",
        "See which skills were used",
        "read",
        cli=("skills report", "skills observe status"),
        mcp=("skills_usage",),
    ),
    _op(
        "skills",
        "Turn watching on or off",
        "admin",
        cli=("skills observe enable", "skills observe disable"),
        web=("POST /skills/observe",),
        why="Consent to being watched. An agent switching on its own monitoring would make the setting meaningless.",
    ),
    _op(
        "skills",
        "Delete recorded skill use",
        "destructive",
        cli=("skills data purge",),
        web=("POST /skills/purge",),
        why="Your record of your own work. Deleting it is never delegated.",
    ),
    _op(
        "skills",
        "Hand over work to learn from, and see what was handed over",
        "write",
        cli=("skills evidence submit", "skills evidence list"),
        why=f"{NOT_YET} Evidence exists because a person submitted it, which keeps learning from becoming surveillance.",
    ),
    _op(
        "skills",
        "Draft, revise and read skill proposals",
        "write",
        cli=("skills propose", "skills revise", "skills proposals", "skills review"),
        web=("GET /skills/proposals", "POST /skills/proposals/revise"),
        why=f"{NOT_YET} Drafting will come to agents as reviewable proposals, never as installs.",
    ),
    _op(
        "skills",
        "Approve or reject a skill draft",
        "approve",
        cli=("skills approve", "skills reject"),
        web=("POST /skills/proposals/decide",),
        why="An approval covers the exact text a person read. No model output may authorise its own installation.",
    ),
    _op(
        "skills",
        "Keep a copy, install it, or put it back",
        "write",
        cli=("skills adopt", "skills install", "skills rollback"),
        web=("POST /skills/{name}/adopt", "POST /skills/rollback"),
        why="Writes into the directory your agent reads. A person decides what an agent loads.",
    ),
    _op(
        "skills",
        "See kept versions and installs",
        "read",
        cli=("skills versions", "skills installs"),
        why=NOT_YET,
    ),
    _op(
        "skills",
        "Send a skill to the team, or install one that arrived",
        "share",
        cli=("skills share", "skills import"),
        web=("POST /skills/{name}/share", "POST /skills/import"),
        why="Receiving is not installing. Both ends are a person's decision.",
    ),
    _op(
        "skills",
        "See what arrived and what you follow",
        "read",
        cli=("skills transfers", "skills channel list"),
        mcp=("mesh_status",),
    ),
    _op(
        "skills",
        "Follow a skill's updates",
        "write",
        cli=("skills channel subscribe", "skills channel unsubscribe"),
        web=("POST /skills/channel",),
        why="Following notifies you and never installs, so whoever publishes cannot steer what your agent reads.",
    ),
    _op(
        "skills",
        "Record evaluation cases and results",
        "write",
        cli=("skills eval add-case", "skills eval add-profile", "skills eval record"),
        why=f"{NOT_YET} Flanner records comparisons; it does not run them.",
    ),
    _op(
        "skills",
        "See the evaluation matrix",
        "read",
        cli=("skills eval matrix",),
        why=NOT_YET,
    ),
    # --- mesh ----------------------------------------------------------------
    _op(
        "mesh",
        "See who you are signed in as, your workspaces and your peers",
        "read",
        cli=("whoami",),
        mcp=("mesh_status",),
        web=("GET /mesh",),
        note="The agent's view is offline: it reads the cached session and never contacts a peer.",
    ),
    _op(
        "mesh",
        "Enrol this machine",
        "admin",
        cli=("accept", "login"),
        why="Generates a key and binds the machine to your account. An agent must never enrol a device.",
    ),
    _op(
        "mesh",
        "Bind a repository to a workspace",
        "admin",
        cli=("join",),
        why="Decides what syncs to whom. A scope decision belongs to a person.",
    ),
    _op(
        "mesh",
        "Invite people and manage members and devices",
        "admin",
        cli=("invite", "members", "devices add", "devices list", "devices revoke"),
        why="Changes who has access, and a seat costs money.",
    ),
    _op(
        "mesh",
        "Serve to and pull from peers",
        "share",
        cli=("peer start", "peer serve", "peer stop", "peer pull", "peer push"),
        why="Moves your work between machines. Syncing is always something you asked for.",
    ),
    _op(
        "mesh",
        "See how this device reaches its peers",
        "read",
        cli=("peer status",),
        why="It contacts peers over the network. mesh_status gives an agent the offline view.",
    ),
    _op(
        "mesh",
        "Join or leave a private network",
        "admin",
        cli=("mesh connect", "mesh leave", "mesh status"),
        why="Changes how the machine is reachable.",
    ),
    _op(
        "mesh",
        "Sign out",
        "admin",
        cli=("logout",),
        why="Ends the session. The device keeps its identity.",
    ),
    # --- local ---------------------------------------------------------------
    _op(
        "local",
        "Register flanner with your agents",
        "admin",
        cli=("setup", "register", "unregister"),
        why="Edits where your editors look for tools. An agent must not rewire its own connection.",
    ),
    _op(
        "local",
        "See whether agents can reach flanner",
        "read",
        cli=("status", "claude-info"),
        why="Answers whether an agent is connected, which an agent calling a tool already knows.",
    ),
    _op(
        "local",
        "Diagnose and repair the store",
        "destructive",
        cli=("doctor",),
        why="Repairs run where you can see what they changed.",
    ),
    _op(
        "local",
        "Run the web UI and the background server",
        "admin",
        cli=("web", "start", "stop"),
        web=("GET /settings",),
        why="Starts and stops processes on your machine.",
    ),
    _op(
        "local",
        "Guard plan files and record skill use from an agent hook",
        "write",
        cli=("hook guard-write", "hook skill-use"),
        why="Called by the agent's own hook system when it writes a file or uses a skill, not by a person or a tool call.",
    ),
    # --- integrations, behind FLANNER_INTEGRATIONS ---------------------------
    _op(
        "integrations",
        "Set up Linear",
        "admin",
        cli=("linear config", "linear auth"),
        mcp=("configure_linear_tool", "get_linear_config_tool"),
        gated="integrations",
    ),
    _op(
        "integrations",
        "Link a plan to a Linear issue",
        "write",
        cli=("linear link", "linear unlink", "linear refresh"),
        mcp=("link_plan_to_linear_tool", "unlink_linear_issue_tool"),
        gated="integrations",
    ),
    _op(
        "integrations",
        "See Linear links",
        "read",
        cli=("linear links", "linear show"),
        mcp=("get_linear_links_tool", "list_linear_links_tool"),
        web=("GET /integrations",),
        gated="integrations",
    ),
    _op(
        "integrations",
        "Set up Jira",
        "admin",
        cli=("jira config",),
        mcp=("configure_jira_tool", "get_jira_config_tool"),
        gated="integrations",
    ),
    _op(
        "integrations",
        "Link a plan to a Jira issue",
        "write",
        cli=("jira link", "jira unlink"),
        mcp=("link_plan_to_jira_tool", "unlink_jira_issue_tool"),
        gated="integrations",
    ),
    _op(
        "integrations",
        "See Jira links",
        "read",
        cli=("jira links", "jira show"),
        mcp=("get_jira_links_tool", "list_jira_links_tool"),
        gated="integrations",
    ),
)


def as_json() -> dict[str, object]:
    """The registry in the shape the site reads. Deterministic."""
    ungated = sorted({name for op in OPERATIONS if not op.gated for name in op.mcp})
    return {
        "access": list(ACCESS),
        "domains": list(DOMAINS),
        "mcp_tool_count": len(ungated),
        "operations": [asdict(op) for op in OPERATIONS],
    }


if __name__ == "__main__":
    print(json.dumps(as_json(), indent=2, ensure_ascii=False))
