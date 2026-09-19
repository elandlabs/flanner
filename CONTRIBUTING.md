# Contributing to flanner

## Branches

- `development` is the working branch. Day-to-day work and PRs target it.
- `main` is the release branch and stays always-releasable.
- CI runs on pushes and PRs to both.
- To release: open a PR from `development` to `main`, merge it, then tag and
  cut a GitHub Release from `main` (see Releasing). Publishing to PyPI only
  happens from a Release on `main`, so nothing ships until it reaches `main`.

## Setup

With [uv](https://docs.astral.sh/uv/) (recommended, cross-platform, one step):

```bash
git clone https://github.com/elandlabs/flanner.git
cd flanner
uv sync --extra dev        # creates .venv and installs everything from uv.lock
uv run pre-commit install
```

Or with pip and a manual virtualenv:

```bash
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install
```

## Before you open a PR

All of these must pass; CI enforces them. With uv you can run them without
activating anything (drop the `uv run` prefix if your venv is active):

```bash
uv run ruff check flanner/ tests/ benchmarks/
uv run ruff format --check flanner/ tests/ benchmarks/
uv run mypy --strict flanner/
uv run pytest -m "not browser" --cov=flanner --cov-fail-under=85
```

The unit suite leaves the browser tests out: they start a server and drive
Chromium, so they are a separate command and a separate CI job, and they run
without coverage so the 85% floor measures the same thing it always has.

### Browser tests

`tests/browser/` opens the local web UI in a real browser and walks through
it: adopting a project, writing and revising a plan, the freshness stream,
live updates, navigation, the command palette, and an accessibility sweep
(WCAG AA contrast in light and dark, no sideways scrolling at three widths).
Each run seeds its own catalog and starts its own server; nothing touches
your `~/.flanner`.

```bash
python scripts/run_browser_tests.py             # fetches Chromium once, then runs
python scripts/run_browser_tests.py -k palette  # anything after goes to pytest
uv run pytest -m browser                        # the same, if Chromium is installed
```

With nothing installed but Docker, run them in the same Playwright image CI
uses, so the browser, fonts and system libraries match exactly:

```bash
python scripts/run_browser_tests.py --docker
docker compose -f compose.browser.yml run --rm browser   # the same, by hand
```

On a failure, the trace, a screenshot and the console log land in
`test-results/`. Open a trace with `playwright show-trace <path>/trace.zip`.

To look at the UI with the same data the tests use:

```bash
flanner demo seed --home ./.demo-home
FLANNER_HOME=./.demo-home flanner web
```

## Ground rules

- Commit titles are semantic: `type(scope): description` (`feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `ci`, `perf`)
- New code ships with tests in the same PR; bug fixes include a regression test
- The import-boundary test (`tests/test_architecture.py`) encodes the layering
  contract; if your change needs a new edge, argue for it in the PR
- New dependencies must justify their weight over the stdlib or an existing dep
- Keep user-facing changes in `CHANGELOG.md` under `[Unreleased]`
- Architecture decisions get an ADR in `docs/adr/`
- Anything user-facing that goes away warns first: `deprecation.warn(...)`
  names the replacement and the version it stops working in, and it keeps
  working for at least one minor release. `doctor --output json` changed
  shape in 0.9.2 without this, which is why the rule is written down

## Running the app

In an activated venv the `flanner` command is on your PATH:

```bash
flanner init          # database + MCP registration + project
flanner web           # browser UI at http://localhost:8080
python -m flanner.server  # MCP server over stdio
```

Without activating, use the module form (inside the repo, `uv run flanner` is
shadowed by the `flanner/` source directory, so prefer `-m`):

```bash
uv run python -m flanner.cli init
uv run python -m flanner.cli web
uv run python -m flanner.server
```

## Benchmarks

Reproduce with `python benchmarks/bench.py` (throwaway temp database). Reference
numbers on Windows 11, Python 3.12, SQLite on NVMe: `create_project` ~36 ms,
`create_plan_file` (2.4 KB body) ~40 ms, `list_plan_files` (100 plans) ~5 ms.

## Releasing

1. Move the `[Unreleased]` CHANGELOG entries under a new version heading.
2. Bump `version` in `pyproject.toml` and `server.json` (both the top-level
   `version` and `packages[0].version`). `flanner/__init__.py` needs no edit:
   it reads the installed package metadata, so it follows `pyproject.toml`
   on its own. `tests/test_release.py` fails if the two files disagree.
3. Commit, then tag and push: `git tag vX.Y.Z && git push origin main --tags`.
4. Publish a GitHub Release for the tag. The `publish` workflow then builds the
   package and uploads it to PyPI via Trusted Publishing (OIDC, no tokens).
5. Only submit `server.json` to the MCP registry *after* that version is live on
   PyPI; its `packages[0].version` must resolve to a published release.

The publish job runs in the `pypi` GitHub Environment. Add a **required
reviewer** to that environment (repo *Settings -> Environments -> pypi ->
Required reviewers*) so every publish pauses for a human to approve before it
uploads to PyPI. The one-time PyPI-side setup (registering the trusted
publisher) is in the workflow header at `.github/workflows/publish.yml`.


## The dev environment is locked; the runtime is not

`requirements-dev.lock` pins the toolchain — pytest, ruff, mypy and their
trees — to a version *and* a hash, and CI installs from it with
`--require-hashes`. Regenerate it whenever `pyproject.toml` changes:

```bash
uv pip compile pyproject.toml --extra dev --generate-hashes --universal --python-version 3.12 -o requirements-dev.lock
```

`--universal` matters: a lock made without it is specific to the machine that
made it, and one made on Windows pins `pywin32`, which cannot install on a
Linux runner.

**The runtime dependencies stay ranges, deliberately.** This is a published
library. Pinning what a consumer installs forces our resolution onto every
project that depends on us, which is the opposite of a library's job. The
test matrix resolves those ranges fresh on every run, which is how we find
out that a dependency has broken us before a user does.

So the lock buys the toolchain: a green run is reproducible, a failure is
attributable to a change rather than to ruff shifting underneath us, and a
compromised dev package cannot slip in unnoticed.

## Benchmarks

```bash
python benchmarks/bench.py                 # the table
python benchmarks/bench.py --check         # what CI runs
python benchmarks/bench.py --save-baseline # after a deliberate change
```

A performance claim needs numbers from this, before and after, on the same
machine. The README's tables are what it printed. Cold start measures the
installed `flanner` console script rather than `python -m flanner.cli` —
they are not the same, and the one a person types is the one worth quoting.
