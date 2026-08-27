"""Source-tree entry point for the Python application.

The packaged executable uses ``packaging/entry.py``.  This small bootstrap keeps
the repository runnable without installing the package into the user's global
Python environment.
"""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = PROJECT_ROOT / "python"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from tarkov_cis.cli import main  # noqa: E402


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments:
        arguments = ["gui", "--config", str(PROJECT_ROOT / "config.json")]
    raise SystemExit(main(arguments))
