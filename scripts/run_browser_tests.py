"""Run the browser suite locally, in one command.

    python scripts/run_browser_tests.py            # the whole suite
    python scripts/run_browser_tests.py -k palette # anything after is pytest's

A suite that is awkward to run locally is a suite people stop running. The
seeding and the server are the fixtures' job (`tests/browser/conftest.py`);
the only thing that cannot be done from inside pytest is fetching the
browser, so that is all this adds.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    install = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT
    )
    if install.returncode != 0:
        print("could not fetch chromium — the suite needs it, so stopping here")
        return install.returncode
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-m", "browser", *sys.argv[1:]], cwd=ROOT
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
