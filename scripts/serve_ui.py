"""Run the local web UI against a seeded catalog, for looking at it.

It used to point `FLANNER_HOME` at a `.ui-preview/` directory that nothing
created, so what the UI showed depended on whose working tree you were in.
It now builds the catalog with `flanner demo seed`, which is the same one
the browser suite asserts against — a screenshot and a test failure can
then be about the same thing.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Must be set before flanner.web is imported: the app resolves its home at
# import time. `seed` sets it too; this keeps the two from disagreeing when
# something in between reads it.
PREVIEW_HOME = Path(os.environ.get("FLANNER_HOME") or ROOT / ".ui-preview")
os.environ["FLANNER_HOME"] = str(PREVIEW_HOME)

from flanner.demo import seed  # noqa: E402

if __name__ == "__main__":
    manifest = seed(PREVIEW_HOME, signed_in="--signed-out" not in sys.argv)
    print(f"seeded {manifest['home']}")

    import uvicorn

    from flanner.web import app

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8094")), log_level="info")
