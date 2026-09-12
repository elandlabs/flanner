import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from flanner.database import init_database
from flanner.git_integration import find_git_root


@pytest.fixture(autouse=True)
def _isolated_flanner_home(tmp_path, monkeypatch):
    """Point FLANNER_HOME, and the home directory itself, at a temp dir.

    FLANNER_HOME prevents tests from seeing the developer's real ~/.flanner
    — in particular a live daemon.json, which would make MCP write tools
    forward to a running `flanner web` instead of executing in-process.

    The home directory is isolated for a blunter reason: `flanner init`
    registers the MCP server where each agent looks for it, and two of those
    places are ~/.claude.md and ~/.claude.json. Without this, running the
    suite would edit the developer's own editor configuration. `Path.home()`
    reads these, and which one it reads differs by platform, so all four are
    set rather than guessing.
    """
    monkeypatch.setenv("FLANNER_HOME", str(tmp_path / "flanner-home"))

    # Deliberately not created. Tests assert on what is inside their temp
    # directory, and a directory conjured up by a fixture nothing asked for
    # shows up in those listings as an unexplained extra. Whatever writes
    # here makes it, the way it would on a real machine. The name avoids
    # "home", which several tests already use for FLANNER_HOME.
    fake_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOMEDRIVE", fake_home.drive or "")
    monkeypatch.setenv("HOMEPATH", str(fake_home)[len(fake_home.drive) :])


@pytest.fixture
def db(tmp_path):
    """Fresh database per test, in a temp directory."""
    db_path = tmp_path / "data.db"
    init_database(str(db_path))
    return db_path


@pytest.fixture
def git_repo(tmp_path):
    """Fresh git-initialized project directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


@pytest.fixture
def outside_any_repo(tmp_path):
    """A directory that really is not inside a git repository.

    `tmp_path` was assumed to be one, and is not. Where the system temp
    directory sits inside a checkout — somebody's actual machine, and the
    cause of four of five failures in an outside review — every test
    asserting "there is no repo here" was quietly asserting the opposite,
    and the paths they exist to cover went unrun with no failure to show
    for it.

    `find_git_root` walks up looking for `.git`, so the precondition cannot
    be faked from inside: it has to be climbed out of. Normally the loop
    does not run at all and this is an ordinary directory under `tmp_path`.
    """
    anchor = Path(tmp_path)
    while (enclosing := find_git_root(str(anchor))) is not None:
        parent = Path(enclosing).parent
        if parent == Path(enclosing):
            pytest.skip("every directory on this machine is inside a git repository")
        anchor = parent

    made = Path(tempfile.mkdtemp(prefix="flanner-no-repo-", dir=anchor))
    try:
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_agent_configs(tmp_path, monkeypatch):
    """Point the Claude Code and Codex config lookups at empty temp paths.

    Both are read from the developer's real home otherwise, so `status`
    would report whatever this machine happens to have registered, and a
    test asserting "not registered" would pass or fail by who ran it.
    """
    import flanner.claude_integration as ci

    monkeypatch.setattr(ci, "claude_code_user_config_path", lambda: tmp_path / "claude.json")
    monkeypatch.setattr(ci, "codex_config_path", lambda: tmp_path / "codex.toml")


@pytest.fixture(autouse=True)
def _isolated_keychain(monkeypatch):
    """An in-memory keychain for every test.

    The real backend here is the OS credential store: on this machine
    WinVaultKeyring, on a Mac the login keychain. Letting the suite write
    there would leave a developer's own store full of test keys, and would
    make tests share an identity through a channel FLANNER_HOME does not
    isolate.

    A fake rather than disabling the keychain outright, so the keychain path
    is the one actually exercised. Disabling it would leave the code that
    matters covered only by the tests that opt back in.
    """
    try:
        import keyring
        from keyring.backend import KeyringBackend
    except ImportError:
        # No keychain library, so nothing to protect: `identity` will take
        # its file fallback. Erroring every test over a missing isolation
        # fixture would be worse than the pollution it guards against.
        yield
        return

    class Memory(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self) -> None:
            self._held: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self._held.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self._held[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            self._held.pop((service, username), None)

    previous = keyring.get_keyring()
    keyring.set_keyring(Memory())
    yield
    keyring.set_keyring(previous)


@pytest.fixture
def integrations_on(monkeypatch):
    """Switch Linear and Jira on for one test.

    They ship off. A test that exercises them says so rather than the whole
    suite running in a configuration nobody ships.
    """
    monkeypatch.setenv("FLANNER_INTEGRATIONS", "1")


@pytest.fixture(autouse=True)
def _not_inside_an_agent_shell(monkeypatch):
    """Tests run inside agent hosts too, which set the markers `actions` looks for.

    Cleared, so a test sees a person's terminal unless it says otherwise.
    """
    from flanner import actions

    for marker in actions.AGENT_SHELL_MARKERS:
        monkeypatch.delenv(marker, raising=False)
