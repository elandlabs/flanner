# ADR 0007: A change counts as tighten-only only if no channel ends up broader

Status: accepted

## Context

Curb's fixes (R3) and an org's signed policy (R5) change agent settings.
A grant given by mistake, or a policy pushed by a compromised control
plane, must never be able to widen what an agent can reach. So Curb needs
a test it can trust: is this change tighten-only (Curb PRD §10.8)?

Edit kinds mislead. Adding a deny entry usually tightens, but Claude Code's
MCP allowlist has documented traps (E14): a `serverName` entry matches
stdio servers only while the list has no `serverCommand` entries, so
removing the last command entry lets any program run under an allowed
name; removing an empty `allowedMcpServers` allows every server; and
without `allowManagedMcpServersOnly`, allowlists from every scope merge.

## Decision

`flanner/curb_tighten.py` judges one file's change for one launch context.
It is tighten-only only when all three hold:

1. **Every key it touches is known and moves the tighter way.** A table per
   agent gives each key's rule: deny lists only grow, allow lists only
   shrink, a sandbox only turns on, ranked modes only fall. A key with no
   rule, such as `env`, a helper command, a proxy or an endpoint, makes the
   change unproven, and unproven is not tighten-only.
2. **Effective reach is no broader.** Curb resolves the settings as they
   are and as they would be (`curb_settings.replaced`, which writes
   nothing), runs the same reach assessment `curb map` uses on probe files,
   and checks that no channel's state moves toward open and no probe
   becomes readable through a new channel.
3. **No MCP server would load that could not before.** Probe servers reuse
   every listed name, command and URL, plus strangers, and each is run
   through the admin's allow and deny lists before and after.

The probes catch the traps above, and the tests keep each one as a case.

## Alternatives considered

- **Classify edit kinds.** Simple, and wrong for exactly the traps above.
- **Compare settings files textually.** Says nothing about effect: the same
  line means different things in different scopes.
- **Trust the console's preview.** The console cannot see each device's own
  settings; the device decides.

## Consequences

- Conservative by construction: anything Curb cannot judge waits for a
  person's approval, including some changes that are in fact tighter.
- A new settings key needs a rule before Curb can apply it on its own.
- The test is only as good as the resolver's model of each agent version,
  which the R1 corpus keeps honest.
