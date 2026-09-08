"""Find mainframe-artifacts: installed, or in the sibling mainframe-common checkout.

This package's one dependency ships from the mainframe-common repository. Installed,
it is simply importable. From a bare dual-checkout - that repo and this one side by
side, nothing installed - its src goes on sys.path. Override the checkout location
with MAINFRAME_COMMON_REPO.

Shared by conftest.py (which ignores the suite when nothing is found) and the sentinel
test module that reports the absence as ONE clean skip naming the exact pip command.
Same pattern, same names as the cobol-xstate-json, jcl-dependencies and
eztrieve-dependencies copies - every
consumer should fail and recover identically.
"""

import importlib.util
import os
import sys
from pathlib import Path

CHECKOUT = Path(os.environ.get(
    "MAINFRAME_COMMON_REPO",
    Path(__file__).resolve().parents[1].parent / "mainframe-common"))


def ensure_on_path():
    """Make mainframe_artifacts importable if possible; return None, or why not.

    The reason string names the exact pip command, because the failure has to be
    actionable from the message alone.
    """
    if importlib.util.find_spec("mainframe_artifacts") is not None:
        return None
    src = CHECKOUT / "mainframe-artifacts" / "src"
    if (src / "mainframe_artifacts" / "__init__.py").is_file():
        sys.path.insert(0, str(src))
        return None
    return (f"mainframe_artifacts is neither installed nor found in a "
            f"mainframe-common checkout at {CHECKOUT} - install it with "
            f"`python -m pip install -e {CHECKOUT / 'mainframe-artifacts'}` "
            f"(or set MAINFRAME_COMMON_REPO to a checkout)")
