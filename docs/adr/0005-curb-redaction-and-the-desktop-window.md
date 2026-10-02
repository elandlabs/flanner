# ADR 0005: Curb's terminal output is redacted for every caller; detail goes to a window

Status: accepted

## Context

Curb's full report is a map of where credentials live on a machine and
which agent channels can reach them. Handed to a prompt-injected agent, it
is a ready-made target list.

The first design showed full detail when the command ran in an interactive
terminal with no agent markers set. The outside review pointed out that an
agent with shell access can unset its markers and allocate a
pseudo-terminal, so neither check says who is asking. They are user
experience signals, not authentication.

## Decision

- **Terminal and JSON output are redacted for every caller.** They carry
  counts by severity, category and channel, and never a credential's name or
  location. No flag, environment variable or terminal setting changes that.
- **Full detail appears only in a desktop window.** `flanner curb show`
  starts a separate, detached process (`flanner curb show --in-window`)
  that works the report out again and draws it with Python's own
  `tkinter`. Nothing comes back to the command that asked, so an agent that
  runs `show` gets a notice and the person gets the window.
- **Nothing with full detail is written to disk.**
- **Curb offers no MCP tools.** The operations registry marks each Curb
  command CLI-only, with the reason.

Where no window can open (no desktop session, or a Python without Tk),
`show` says why and exits 1. `flanner curb map` still works.

## Alternatives considered

- **Terminal and agent-marker checks.** An agent can fake both.
- **A web page in `flanner web`.** A script can drive a local web session
  as easily as a terminal. The R3 web page stays redacted and opens the
  window.
- **Show detail after an OS approval.** Approvals arrive with writes in R3.
  For reading, a window is simpler and does not depend on how the person
  answers a prompt.
- **The flanner desktop app.** It is not released yet. Hosting the window
  there is a Later item.

## Consequences

- An agent that writes its own code can still scan the disk, or read the
  screen, as the user. Only the agent's sandbox and deny rules stop that,
  and installing them is what Curb's fixes (R3) are for. The PRD states this
  residual risk.
- Machines without a desktop get redacted reports only.
- Every test of a terminal or JSON surface plants credentials with
  distinctive names and asserts they do not appear (critical invariant 4).
