"""Build the Python runtime the desktop app ships.

python-build-standalone, with flanner installed into it and the marker file
that tells flanner it is the desktop app's (`flanner.release.is_desktop`).

    python desktop/bundle/build_runtime.py --flanner flanner==0.15.0
    python desktop/bundle/build_runtime.py --flanner dist/flanner-0.15.0-py3-none-any.whl

A release installs the exact version from PyPI, so the app and pip users
run the same code. CI on a pull request installs the wheel it just built.

flanner's dependencies come from runtime.lock, next to this file, and pip
refuses any that does not match its recorded hash. flanner itself is then
installed without dependencies, and `pip check` fails the build if the lock
is missing anything flanner needs. Regenerate the lock after changing
pyproject.toml's dependencies (one command):

    uv pip compile pyproject.toml --universal --generate-hashes
        --python-version 3.12 --no-header -o desktop/bundle/runtime.lock

Stdlib only: it runs on a bare runner before anything else is installed.
It builds for the machine it runs on, because pip installs that machine's
wheels; each OS builds its own runtime in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "20260901"
PYTHON = "3.12.14"
URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{name}"

#: From that release's SHA256SUMS. A download that does not match is refused.
#: Bump RELEASE, PYTHON and these together.
SHA256 = {
    "x86_64-pc-windows-msvc": "e90c1b6419da3bd812dd73bb3de40287a21abf153438147639ec5e20375ea93f",
    "aarch64-apple-darwin": "3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76",
    "x86_64-unknown-linux-gnu": "936c246dfdbbfa7cb22dd01814a21f582a892689fae96b06071a5e433baffa22",
}

#: flanner.release.DESKTOP_MARKER. Repeated because flanner is not importable
#: here yet; the smoke tests fail if the two ever disagree.
MARKER = "flanner-desktop"

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).resolve().parent / "runtime.lock"

#: Written into the runtime's site-packages, so it runs whenever the bundled
#: Python starts: the app's sidecar, the launchers, and the copies of itself
#: flanner starts with sys.executable.
SITECUSTOMIZE = '''"""Keep the flanner desktop app's Python to itself.

Every Python 3.12 on a machine reads the user's site-packages and
PYTHONPATH. A package found there could shadow one flanner depends on,
so this Python drops both from sys.path whenever it starts.
"""

import os
import site
import sys

# ponytail: .pth files in the user site have already run by the time this
# does. Isolate harder with a ._pth file (Windows) if that ever matters.
_foreign = {
    os.path.normcase(os.path.abspath(entry))
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
    if entry
}
if site.ENABLE_USER_SITE:
    _foreign.add(os.path.normcase(os.path.abspath(site.getusersitepackages())))
sys.path[:] = [
    entry for entry in sys.path if os.path.normcase(os.path.abspath(entry)) not in _foreign
]
'''

#: What the app never uses: debug symbols, Tk (its folder dialog is native),
#: IDLE, and ensurepip (pip itself stays; `flanner desktop-link` uses it).
#: Per platform, because Windows globs ignore case: a Linux pattern such as
#: "lib/thread*" would match Lib/threading.py there.
TRIM = {
    "win32": (
        "**/*.pdb",
        "tcl",
        "Lib/tkinter",
        "Lib/idlelib",
        "Lib/ensurepip",
        "DLLs/_tkinter.pyd",
        "DLLs/tcl86t.dll",
        "DLLs/tk86t.dll",
    ),
    "posix": (
        "lib/python3.12/tkinter",
        "lib/python3.12/idlelib",
        "lib/python3.12/ensurepip",
        "lib/python3.12/lib-dynload/_tkinter*",
        "lib/tcl8*",
        "lib/tk8*",
        "lib/itcl*",
        "lib/thread2*",
    ),
}


def host_target() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return "x86_64-pc-windows-msvc"
    if sys.platform == "darwin" and machine == "arm64":
        return "aarch64-apple-darwin"
    if sys.platform.startswith("linux") and machine == "x86_64":
        return "x86_64-unknown-linux-gnu"
    raise SystemExit(f"There is no desktop build for {sys.platform} on {machine} yet.")


def python_in(runtime: Path) -> Path:
    return runtime / "python.exe" if sys.platform == "win32" else runtime / "bin" / "python3"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(target: str, cache: Path) -> Path:
    """The python-build-standalone archive for `target`, downloaded once and verified."""
    name = f"cpython-{PYTHON}+{RELEASE}-{target}-install_only.tar.gz"
    archive = cache / name
    if archive.is_file() and _sha256(archive) == SHA256[target]:
        return archive
    cache.mkdir(parents=True, exist_ok=True)
    partial = archive.with_suffix(".partial")
    url = URL.format(release=RELEASE, name=name)
    with urllib.request.urlopen(url, timeout=300) as reply, partial.open("wb") as file:  # noqa: S310 - a fixed https URL
        shutil.copyfileobj(reply, file)
    found = _sha256(partial)
    if found != SHA256[target]:
        partial.unlink()
        raise SystemExit(f"{name} does not match its pinned checksum (got {found}).")
    partial.replace(archive)
    return archive


def build(flanner: str, out: Path, cache: Path, lock: Path = LOCK) -> Path:
    """Unpack a fresh runtime into `out`, install `flanner`, mark it. Returns its python."""
    archive = fetch(host_target(), cache)
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as unpacked:
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter="data")
        (Path(unpacked) / "python").rename(out)

    python = python_in(out)
    # Without this, pip counts packages in the user's own site-packages as
    # installed, and leaves them out of the runtime.
    isolated = {**os.environ, "PYTHONNOUSERSITE": "1"}
    isolated.pop("PYTHONPATH", None)
    pip = [str(python), "-m", "pip"]
    quiet = ["--no-cache-dir", "--no-warn-script-location", "--disable-pip-version-check"]
    for step in (
        ["install", *quiet, "--require-hashes", "--no-deps", "-r", str(lock)],
        ["install", *quiet, "--no-deps", flanner],
        ["check", "--disable-pip-version-check"],
    ):
        subprocess.run([*pip, *step], check=True, env=isolated)  # noqa: S603 - fixed argv
    purelib = subprocess.run(  # noqa: S603 - as above
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
        env=isolated,
    ).stdout.strip()
    (Path(purelib) / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    trim(out)
    (out / MARKER).write_text("", encoding="utf-8")
    return python


def trim(runtime: Path) -> None:
    for pattern in TRIM["win32" if sys.platform == "win32" else "posix"]:
        for path in runtime.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--flanner", required=True, help="flanner==X.Y.Z, or a wheel path")
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "runtime")
    parser.add_argument("--cache", type=Path, default=ROOT / "build" / "cache")
    parser.add_argument("--lock", type=Path, default=LOCK, help="hashed dependency lock")
    args = parser.parse_args()
    python = build(args.flanner, args.out.resolve(), args.cache.resolve(), args.lock.resolve())
    print(python)


if __name__ == "__main__":
    main()
