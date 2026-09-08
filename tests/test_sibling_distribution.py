"""The sentinel for the mainframe-artifacts dependency.

When mainframe-artifacts is reachable (installed, or via the sibling mainframe-common
checkout), this is a real assertion that it is. When it is not, conftest.py ignores
every other module in this suite - none of them can even import - and THIS one
remains, so the run ends as one clean skip naming the exact pip command instead of a
wall of collection errors.
"""

import importlib.util

import pytest

from _mainframe_common import ensure_on_path


def test_the_mainframe_artifacts_distribution_is_reachable():
    reason = ensure_on_path()
    if reason is not None:
        pytest.skip(reason)
    assert importlib.util.find_spec("mainframe_artifacts") is not None
