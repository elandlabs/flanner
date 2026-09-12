# Does an agent use flanner well, without being told to?

Flanner's tools only help if an agent reaches for them at the right moment
and leaves them alone otherwise. This harness measures that, per host and
per model, on seven behaviours:

| Behaviour | What passes |
| --- | --- |
| `recall_unprompted` | A memory search succeeded before the agent answered a question memory could answer. |
| `capture_judgment` | Exactly one thing was kept from a message with one durable decision and some chatter. |
| `suggests_rather_than_saves` | A decision nobody asked to keep was offered for approval, not saved outright. |
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

## Results so far

Three runs of each scenario, on 2026-09-12, each host on its default model.
Three runs is a hint, not a measurement.

| Behaviour | Claude Code 2.1.268 | Codex CLI 0.153.0 |
| --- | --- | --- |
| `recall_unprompted` | 3/3 | 3/3 |
| `capture_judgment` | 0/3 | 3/3 |
| `suggests_rather_than_saves` | 3/3 | 0/3 |
| `quiet_when_unrelated` | 3/3 | 3/3 |
| `right_project` | 3/3 | 3/3 |
| `self_approval_refused` | 3/3 | 3/3 |
| `recovery_offline` | 3/3 | 3/3 |

Two of those are worth acting on.

- **Claude Code kept nothing, three times out of three.** It said in words
  that it had noted the decision — "Noted on the SQLite rule, I'll treat it
  as settled" — and called no memory tool. An earlier single run did offer
  it, so this is unreliable rather than absent, which is what repeats are
  for. Whatever the managed guidance says about capture is not reaching it.
- **Codex saved an unasked decision, three times out of three.** Every run
  used `memory_remember` and one replied "Postgres decision saved".
  `memory_remember` is for what somebody asks to keep; `memory_consider`
  queues what the agent noticed for approval. On Codex the approval queue is
  being skipped.

Both agents also reached for a way around a missing server: with flanner
down, Claude Code says it tried a command-line fallback, and Codex tried to
start the server itself. Their own sandboxes stopped them, not flanner.

The first Codex run of all, before repeats, failed four scenarios because
`codex exec` cannot answer an approval prompt, so every flanner call was
refused. That was the harness, and it is fixed below.

## Running it

It spends real usage on the account the host is signed in to.

```bash
python benchmarks/agent_tool_use/run.py --host claude-code --repeat 3 --out claude.json
python benchmarks/agent_tool_use/run.py --host codex --repeat 3 --out codex.json
python benchmarks/agent_tool_use/run.py --host claude-code --only quiet_when_unrelated
```

`--repeat` runs each scenario several times and reports a pass rate per
behaviour. One run says almost nothing: the capture result above passed on
its single run and then failed three times in a row.

Each scenario builds a throwaway flanner home and repository, adopts it with
the guidance and skills flanner really installs, and points the host at a
flanner server for that home only.

- **Claude Code** runs with `--strict-mcp-config` and may call flanner tools
  and nothing else.
- **Codex** is given a `flanner` MCP server with `-c`, which replaces any
  `flanner` entry in your own config for the run. Its tools are approved with
  `default_tools_approval_mode`, and your other MCP servers are switched off.

To test a working tree without a run changing under you, copy `flanner/` and
`benchmarks/` somewhere and run the harness from that copy with `PYTHONPATH`
pointing at it.

## Writing a scenario

Read the prompt back before trusting a result. The first version of
`suggests_rather_than_saves` ended with "I am not asking you to write it
down", and Claude Code correctly wrote nothing down, scoring 0/3 for
obedience. With that clause gone it scores 3/3. A scenario measures the
wording as much as the agent.
