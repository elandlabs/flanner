# ADR 0004: Curb reports on a stated launch context, with three evidence levels

Status: accepted

## Context

Curb answers what an AI coding agent can reach: which credentials it could
read, and which channels it could use to send them somewhere. The obvious
input is the agent's settings files. But settings files say what an agent
would apply, not what a running session does. A session can pass
`--settings`, pick a Codex `--profile`, start in another folder, or skip
permissions with a flag. Both agents document this: Claude Code puts command
line settings above project and user settings, and Codex puts CLI flags and
`-c` overrides above every file (Curb PRD, E11 and E47).

A report that read the files and called the result "verified" would
overstate what it knew. The outside review of the PRD found exactly this.

## Decision

Every result is for a stated **launch context**: the agent and its version,
the working directory, the Codex profile and project trust, and the settings
sources the launch loads. The default is the agent started from the current
folder with no flags. A person can name another with `--dir` and
`--profile`, or pass the launch command itself after `--`. Each scheduled
job is assessed in its own context, taken from its definition. Every report
prints its context and the assumption behind it.

The flags Curb understands are the documented flags of the pinned baseline
versions (Claude Code 2.1.287, Codex 0.154.0), taken from their `--help`. An
unknown flag makes every channel unknown, the worse case, because it could
turn a channel on as easily as off. A settings file Curb cannot parse does
the same.

Findings carry one of three evidence levels:

- **Configured:** read from the settings for the stated context. R1 never
  claims more than this.
- **Enforced:** a tester run in the same context proved the running agent
  denies the operation. This arrives with the tester in R3.
- **Assumed:** inferred from documentation, a default, or an input Curb
  could not read. An unreadable input always takes its worse value.

Severity comes from a versioned rule table, `severity-r1` version 1
(`flanner/curb_severity.py`), over four inputs: a readable wide credential,
any readable credential, outside content, and uncontrolled egress. A verdict
is marked assumed only when an assumption changes it: Curb scores again
with every assumed input given its better value, and keeps "configured"
when both scores agree.

Approval prompts are reported but do not count as controls in version 1.
So a sandbox counts only when nothing can leave it behind a prompt: Claude
Code's strict sandbox mode (`allowUnsandboxedCommands: false`, no
`excludedCommands`), or Codex with `approval_policy = "never"`.

## Alternatives considered

- **Treat settings files as the running session.** Simpler, and wrong
  whenever a launch adds a flag, which scheduled jobs routinely do.
- **Inspect running sessions.** Reading other processes' command lines is
  platform-specific and still misses shell aliases. Recorded as a Later item.
- **Count approval prompts as controls.** They are a real barrier, but how
  well they hold depends on how people answer them, which Curb cannot see.

## Consequences

- Most developer machines will score High today. The report says which
  setting would change that, which is the point.
- A new agent version is analysed but marked assumed until the corpus
  passes against it and the baseline moves.
- Changing a severity rule means bumping `curb_severity.VERSION` and the
  expected results in the tests.
