"""Development convenience: find mainframe-artifacts in its sibling checkout.

Installed (pip install), the dependency is simply importable and none of this runs.
From a bare dual-checkout - mainframe-common beside this repo, nothing installed -
every module here would die collecting (they all import mainframe_artifacts, directly
or through asm_dependencies), which on a developer machine is a wall of collection
errors for no reason. So _mainframe_common.py puts the sibling's src on sys.path, and
when NEITHER is present every module except the sentinel
(test_sibling_distribution.py) is ignored, so the run ends as one clean skip naming
the exact pip command.
"""

from pathlib import Path

from _mainframe_common import ensure_on_path

_HERE = Path(__file__).resolve().parent

if ensure_on_path() is not None:
    collect_ignore = sorted(
        p.name for p in _HERE.glob("test_*.py")
        if p.name != "test_sibling_distribution.py")
