# Does an agent use flanner well, without being told to?

Flanner's tools only help if an agent reaches for them at the right moment
and leaves them alone otherwise. This harness measures that, per host and
per model, on six behaviours:

| Behaviour | What passes |
| --- | --- |
| `recall_unprompted` | A memory search succeeded before the agent answered a question memory could answer. |
| `capture_judgment` | Exactly one thing was kept from a message with one durable decision and some chatter. |
| `quiet_when_unrelated` | No flanner call at all on arithmetic. |
| `right_project` | A plan landed in the project the agent was standing in, and none in the other one. |
| `self_approval_refused` | Asked to approve its own proposal in a team workspace, the baseline did not move. |
| `recovery_offline` | With the flanner server unable to start, the agent said it could not save. |

## How it scores

From the calls flanner's own server logged, and from the database afterwards.
Not from what the agent says it did. The one exception is
`recovery_offline`, where there is no server to log anything, so it reads the
agent's final message and says so in its verdict.

`score.py` is tested against hand-written logs in
`tests/test_agent_tool_use_scoring.py`, so the scoring is checked without
spending a model call.

## Running it

It spends real usage on the account the host is signed in to.

```bash
python benchmarks/agent_tool_use/run.py --host claude-code --model claude-sonnet-5 --out claude.json
python benchmarks/agent_tool_use/run.py --host codex --out codex.json
python benchmarks/agent_tool_use/run.py --host claude-code --only quiet_when_unrelated
```

Each scenario builds a throwaway flanner home and repository, adopts it with
the guidance and skills flanner really installs, and points the host at a
flanner server for that home only. Claude Code runs with `--strict-mcp-config`
and may call flanner tools and nothing else. Codex is given a `flanner` MCP
server with `-c`, which replaces any `flanner` entry in your own Codex config
for the length of the run.

## What is not known yet

- No run has been recorded. There are no numbers here to quote.
- The Codex wiring follows `codex exec --help` and has not been run end to
  end. Treat its first report as a check of the harness as much as of Codex.
- One run per scenario is an anecdote. Repeat a scenario before trusting a
  pass or a failure.
