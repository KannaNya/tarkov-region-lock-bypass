"""PyInstaller entry point for the Windows distribution."""

import sys

from tarkov_cis.cli import main


if __name__ == "__main__":
    # A normal double-click should open the control panel.  Explicit CLI
    # arguments (including --help) retain the command-line behavior.
    raise SystemExit(main(sys.argv[1:] or ["gui"]))
