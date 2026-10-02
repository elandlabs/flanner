# Flanner Curb CI check

Checks every agent step in a repository's GitHub Actions workflows the way
`flanner curb map` checks an agent launch on a laptop: whether untrusted
text from issues, comments or pull requests reaches it, who can start it,
the secrets and tools it holds, and whether Harden-Runner blocks its
egress. It writes SARIF for code scanning and names workflow files and
lines, never a secret's name.

```yaml
name: Curb
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
  security-events: write
jobs:
  curb:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: elandlabs/flanner/actions/curb-ci@v0.16.0
        id: curb
        with:
          fail-on: high
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: ${{ steps.curb.outputs.sarif }}
```

| Input | Default | Means |
|---|---|---|
| `path` | `.` | The repository folder to check |
| `sarif` | `curb-ci.sarif` | Where to write the SARIF file |
| `fail-on` | `high` | Fail the step at this severity or above: `high`, `medium`, `low` or `never` |
| `fix` | `false` | Make the one-line fixes that are safe without a person |

## Severity, `ci-r1` version 1

| Rule | Severity | When |
|---|---|---|
| CI-H1 | High | Untrusted input reaches the agent, it holds secrets or a token it can use, or runs unsafe, and egress is open |
| CI-M1 | Medium | Untrusted input reaches the agent, and it can run shell commands or write |
| CI-M2 | Medium | The agent holds secrets or runs unsafe, though no untrusted input reaches it |
| CI-L1 | Low | Anything else |

## Fixes

`fix: true` removes `allowed_non_write_users`, turns Codex's
`safety-strategy: unsafe` into `drop-sudo` and `sandbox: danger-full-access`
into `workspace-write`, and drops `--dangerously-skip-permissions` and
`--yolo`. It edits the files only; open a pull request with them so a
person reviews the change. Everything else is advice in each finding.
