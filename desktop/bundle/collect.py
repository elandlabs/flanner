"""Copy this platform's bundles out of Tauri's build folder, renamed.

    python desktop/bundle/collect.py --version 0.15.0 --out dist/desktop

Tauri names files after the version (Flanner_0.15.0_x64-setup.exe). The
release gives them names without it, so that
https://github.com/elandlabs/flanner/releases/latest/download/<name>
always serves the newest build, and flanner.io/download/<os> can redirect
there for good. Updater signatures (.sig) come along when the build made
them; a dry run makes none.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BUNDLES = Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "release" / "bundle"

#: Per platform: (folder under bundle/, Tauri's name, the release's name).
#: `{version}` is filled in; each entry must produce exactly one file.
FILES = {
    "win32": [
        ("nsis", "Flanner_{version}_x64-setup.exe", "Flanner-windows-x64-setup.exe"),
    ],
    "darwin": [
        ("dmg", "Flanner_{version}_aarch64.dmg", "Flanner-macos-arm64.dmg"),
        ("macos", "Flanner.app.tar.gz", "Flanner-macos-arm64.app.tar.gz"),
    ],
    "linux": [
        ("appimage", "Flanner_{version}_amd64.AppImage", "Flanner-linux-x64.AppImage"),
        ("deb", "Flanner_{version}_amd64.deb", "Flanner-linux-x64.deb"),
    ],
}


def collect(version: str, out: Path, bundles: Path = BUNDLES, platform: str = "") -> list[Path]:
    platform = platform or ("linux" if sys.platform.startswith("linux") else sys.platform)
    out.mkdir(parents=True, exist_ok=True)
    copied = []
    for folder, built, released in FILES[platform]:
        source = bundles / folder / built.format(version=version)
        if not source.is_file():
            # The macOS updater archive exists only when updater files were made.
            if released.endswith(".app.tar.gz"):
                continue
            raise SystemExit(f"{source} was not built.")
        for suffix in ("", ".sig"):
            if (source.parent / (source.name + suffix)).is_file():
                target = out / (released + suffix)
                shutil.copy2(source.parent / (source.name + suffix), target)
                copied.append(target)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for path in collect(args.version, args.out):
        print(path)


if __name__ == "__main__":
    main()
