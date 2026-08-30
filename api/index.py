"""Vercel entrypoint.

Vercel looks for a FastAPI instance named `app` at a supported entrypoint and
serves the whole application as one function on Fluid compute.

The sys.path lines exist because this repository uses a src layout and Vercel
installs from requirements.txt without installing the project itself. Adding both
directories is more predictable here than trying to get an editable install to
work inside the build.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from saas.app import app  # noqa: E402

__all__ = ["app"]
