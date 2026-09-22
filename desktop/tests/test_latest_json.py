"""latest.json: what every installed app reads to decide whether to update."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bundle"))
import latest_json  # noqa: E402


def _signed(folder: Path) -> None:
    for name in latest_json.UPDATERS.values():
        (folder / f"{name}.sig").write_text(f"sig-of-{name}\n", encoding="utf-8")


def test_every_platform_points_at_the_tagged_release(tmp_path: Path) -> None:
    _signed(tmp_path)
    when = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

    written = latest_json.latest("0.15.0", tmp_path, when)

    assert written["version"] == "0.15.0"
    assert written["pub_date"] == "2026-09-23T12:00:00Z"
    platforms = written["platforms"]
    assert isinstance(platforms, dict)
    assert set(platforms) == {"windows-x86_64", "darwin-aarch64", "linux-x86_64"}
    windows = platforms["windows-x86_64"]
    assert windows["url"] == (
        "https://github.com/elandlabs/flanner/releases/download/v0.15.0/"
        "Flanner-windows-x64-setup.exe"
    )
    assert windows["signature"] == "sig-of-Flanner-windows-x64-setup.exe"
    assert all("/latest/" not in entry["url"] for entry in platforms.values())


def test_a_missing_platform_stops_the_release(tmp_path: Path) -> None:
    """Half a release would offer an update some platforms cannot install."""
    _signed(tmp_path)
    (tmp_path / "Flanner-linux-x64.AppImage.sig").unlink()

    with pytest.raises(SystemExit, match="linux"):
        latest_json.latest("0.15.0", tmp_path)


# --- the names the release gives the files ----------------------------------------

import collect  # noqa: E402


def test_bundles_get_names_without_the_version(tmp_path: Path) -> None:
    bundles = tmp_path / "bundle"
    (bundles / "appimage").mkdir(parents=True)
    (bundles / "deb").mkdir()
    (bundles / "appimage" / "Flanner_0.15.0_amd64.AppImage").write_bytes(b"app")
    (bundles / "appimage" / "Flanner_0.15.0_amd64.AppImage.sig").write_text("sig")
    (bundles / "deb" / "Flanner_0.15.0_amd64.deb").write_bytes(b"deb")

    copied = collect.collect("0.15.0", tmp_path / "out", bundles, platform="linux")

    assert sorted(path.name for path in copied) == [
        "Flanner-linux-x64.AppImage",
        "Flanner-linux-x64.AppImage.sig",
        "Flanner-linux-x64.deb",
    ]


def test_every_updater_file_latest_json_names_is_one_collect_makes() -> None:
    """The two scripts must agree, or an app would be told to fetch a file that is not there."""
    released = {name for files in collect.FILES.values() for _, _, name in files}
    assert set(latest_json.UPDATERS.values()) <= released


def test_a_missing_installer_stops_the_release(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="was not built"):
        collect.collect("0.15.0", tmp_path / "out", tmp_path / "bundle", platform="win32")
