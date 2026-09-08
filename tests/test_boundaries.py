"""This package must never depend on the COBOL, JCL or Easytrieve ones.

The four are peers: the COBOL tools say what a program does, the JCL one says which dataset
a ddname is, the Easytrieve one says which bytes of one record become which bytes of
another, and this one says what a module calls and what it can be called AS. They meet at
plain dicts. Nothing about the source layout enforces that - a single stray import would
erase it while every other test still passed, and the cost is not abstract: an assembler
box would start carrying a COBOL modelling engine (``cobol_xstate``) or a JCL parser
(``jcl_dependencies``) it never executes, and this repository could no longer be released
on its own. An artifacts+asm install must be unable to find any of them.

A note on how, because getting it wrong is easy and silent: ``sys.meta_path`` finders are
consulted through ``find_spec``. ``find_module`` was REMOVED in Python 3.12, so a blocker
that only defines it is ignored entirely and every test here passes vacuously.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from _mainframe_common import CHECKOUT

SRC = Path(__file__).resolve().parents[1] / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = SRC.parent / "examples"
# The child interpreters cannot inherit conftest's sys.path insertion, so they get the same
# trees explicitly: this repo's src plus the sibling checkout's mainframe-artifacts (a
# nonexistent path is inert - the pip-installed distribution carries the run then).
_TREES = (str(CHECKOUT / "mainframe-artifacts" / "src"), str(SRC))

_BLOCKED = ("cobol_xstate", "cobol_parser", "jcl_dependencies", "eztrieve_dependencies")

#: Every module in the package. Hand-maintained on purpose - a new module missing from
#: here is silently unchecked, so adding one is meant to make this list fail first.
_MODULES = ["lexer", "model", "opcodes", "conditional", "macros", "parser", "dataflow",
            "classify", "views", "detect", "prefetch", "api", "cli", "__init__",
            "__main__"]

_PREAMBLE = textwrap.dedent("""
    import sys
    for _tree in %r:
        sys.path.insert(0, _tree)

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in %r:
                raise ImportError("BLOCKED " + name)
            return None

    sys.meta_path.insert(0, Blocker())
""")


def _isolated(body):
    """A fresh interpreter: blocking a module already in sys.modules does nothing."""
    return subprocess.run([sys.executable, "-c",
                           _PREAMBLE % (_TREES, _BLOCKED) + textwrap.dedent(body)],
                          capture_output=True, text=True)


@pytest.mark.parametrize("package", _BLOCKED)
def test_the_blocker_actually_blocks(package):
    """Guard the guard. If this passes when it should not, everything below is vacuous."""
    proc = _isolated("import {0}".format(package))
    assert proc.returncode != 0
    assert "BLOCKED {0}".format(package) in proc.stderr


def test_the_package_works_with_the_sibling_packages_unavailable():
    proc = _isolated("""
        from asm_dependencies.api import analyze
        a = analyze("PAYCALC  CSECT\\n"
                    "         DC    V(PAYVALD)\\n"
                    "         LINK  EP=PAYPOST\\n"
                    "         END   PAYCALC\\n", retrieve=False)
        assert a.module.name == "PAYCALC"
        names = [r["artifact"] for r in a.artifacts()["artifacts"]]
        assert names == ["PAYPOST", "PAYVALD"], names
        assert a.artifacts()["provides"][0]["name"] == "PAYCALC"
        print("OK")
    """)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_the_cli_works_with_the_sibling_packages_unavailable(tmp_path):
    proc = _isolated("""
        import io, contextlib, os
        from asm_dependencies.cli import run
        out = {out!r}
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run([{src!r}, "--outdir", out, "-q", "--no-fetch"])
        assert rc == 0, rc
        print("FILES", len([f for f in os.listdir(out) if f.endswith(".json")]))
    """.format(out=str(tmp_path / "o"), src=str(EXAMPLES / "paycalc.asm")))
    assert proc.returncode == 0, proc.stderr
    assert "FILES 4" in proc.stdout       # both views + both retrieval reports


def test_the_jcl_join_takes_a_dict_and_needs_no_jcl_types():
    """bind_jcl_ddnames is the one function that serves a JCL feature. It consumes a plain
    lineage dict, which is precisely what keeps this dependency from existing."""
    proc = _isolated("""
        import json, pathlib
        from asm_dependencies.parser import parse_asm
        from asm_dependencies.views import bind_jcl_ddnames, build_asm_artifacts
        lineage = json.loads(pathlib.Path({fixture!r}).read_text(encoding="utf-8"))
        module = parse_asm(
            pathlib.Path({src!r}).read_text(encoding="utf-8"),
            source_name="paycalc.asm")
        out = bind_jcl_ddnames(build_asm_artifacts(module), lineage)
        row = next(a for a in out["artifacts"] if a.get("ddname") == "CUSTMAST")
        print("DATASET", row["dataset"])
    """.format(fixture=str(FIXTURES / "paycalc.jcl.lineage.json"),
               src=str(EXAMPLES / "paycalc.asm")))
    assert proc.returncode == 0, proc.stderr
    assert "DATASET PROD.CUSTOMER.MASTER" in proc.stdout


@pytest.mark.parametrize("module", _MODULES)
def test_no_module_imports_a_sibling_package(module):
    """Read the source too: an import inside a rarely-taken branch would not show up in a
    passing import test."""
    src = (SRC / "asm_dependencies" / "{0}.py".format(module)).read_text(encoding="utf-8")
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")):
            for blocked in _BLOCKED:
                assert ("{0}.".format(blocked) not in s
                        and s != "import {0}".format(blocked)
                        and not s.startswith("from {0} ".format(blocked))), (
                    "asm_dependencies/{0}.py imports {1}: {2}".format(
                        module, blocked, s))


def test_the_module_inventory_is_complete():
    """The parametrize list above is hand-maintained, so a module added without being
    listed would be silently unchecked. This is what makes that a red test."""
    on_disk = sorted(p.stem for p in (SRC / "asm_dependencies").glob("*.py"))
    assert on_disk == sorted(_MODULES)
