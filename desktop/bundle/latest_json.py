"""Write latest.json, the file installed apps read to learn about updates.

    python desktop/bundle/latest_json.py --version 0.15.0 --dir dist/desktop

`--dir` holds the release's updater files, each with its `.sig` beside it,
under the version-free names the release workflow gives them. Download
links name the tagged release, never `latest/`: an app that reads this
file gets exactly the build these signatures are for, even after a newer
release is published.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = "https://github.com/elandlabs/flanner"

#: Tauri's platform key, and the file its updater downloads on that platform.
UPDATERS = {
    "windows-x86_64": "Flanner-windows-x64-setup.exe",
    "darwin-aarch64": "Flanner-macos-arm64.app.tar.gz",
    "linux-x86_64": "Flanner-linux-x64.AppImage",
}


def latest(version: str, folder: Path, published: datetime | None = None) -> dict[str, object]:
    platforms = {}
    for platform, name in UPDATERS.items():
        signature = folder / f"{name}.sig"
        if not signature.is_file():
            raise SystemExit(f"{signature} is missing; every platform ships or none does.")
        platforms[platform] = {
            "signature": signature.read_text(encoding="utf-8").strip(),
            "url": f"{REPOSITORY}/releases/download/v{version}/{name}",
        }
    when = (published or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": version,
        "notes": f"{REPOSITORY}/blob/v{version}/docs/releases/v{version}.md",
        "pub_date": when,
        "platforms": platforms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args()
    json.dump(latest(args.version, args.dir), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
