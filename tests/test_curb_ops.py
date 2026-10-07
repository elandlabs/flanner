"""What Curb's two surfaces share (`curb_ops`): the team's view of this device."""

from pathlib import Path

from flanner import curb_credentials, curb_ops, curb_store


def test_the_team_view_is_taken_outside_any_project_and_never_walks_home(tmp_path, monkeypatch):
    """The team pass runs at every session start. From the home folder it
    walked every folder in it, looking for a project's .env files."""
    claude = tmp_path / "claude-config"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr("flanner.curb_inventory.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_report.shutil.which", lambda name: None)
    monkeypatch.setattr("flanner.curb_inventory.run", lambda argv: None)
    home = Path.home()
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[default]\n", encoding="utf-8")
    (home / "notes").mkdir()
    (home / "notes" / ".env").write_text("API_KEY=x\n", encoding="utf-8")
    walked = []
    real = curb_credentials.dotenv_files
    monkeypatch.setattr(
        curb_credentials, "dotenv_files", lambda cwd: walked.append(cwd) or real(cwd)
    )

    (report,) = curb_ops.device_reports()

    assert report.context.cwd == curb_store.outside().resolve()
    assert walked == [curb_store.outside().resolve()]
    kinds = {reach.credential.kind for reach in report.reach}
    assert "aws" in kinds and "dotenv" not in kinds
