"""Run the browser suite locally, in one command.

    python scripts/run_browser_tests.py            # the whole suite
    python scripts/run_browser_tests.py -k palette # anything after is pytest's
    python scripts/run_browser_tests.py --docker   # in Playwright's image, as CI

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
    args = sys.argv[1:]
    if "--docker" in args:
        args.remove("--docker")
        # compose.browser.yml builds the image and appends args to pytest.
        compose = ["docker", "compose", "-f", str(ROOT / "compose.browser.yml")]
        return subprocess.run([*compose, "run", "--rm", "browser", *args], cwd=ROOT).returncode  # noqa: S603

    install = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "playwright", "install", "chromium"], cwd=ROOT
    )
    if install.returncode != 0:
        print("could not fetch chromium — the suite needs it, so stopping here")
        return install.returncode
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-m", "browser", *args], cwd=ROOT
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
