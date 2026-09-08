#!/usr/bin/env python3
"""The cross-repository contract with jcl-dependencies.

``tests/fixtures/paycalc.jcl.lineage.json`` is a committed JCL **lineage** view - a real
one, produced by running ``jcl-dependencies`` over ``examples/paycalc.jcl``. It is this
repository's half of the join, so the test suite needs no JCL install.

A committed fixture goes stale, and this particular staleness does not fail loudly: an
unbound manifest looks **exactly** like a manifest nobody tried to bind. Every file row
still says it needs JCL, no exception is raised, no flag appears, and the only difference
is the three keys that were supposed to arrive and did not. So the shape the binder reads
is asserted here, and ``--check`` is run by the suite.

    python tools/refresh_fixture.py --check     # assert the shape (the suite runs this)
    python tools/refresh_fixture.py --record    # regenerate from a jcl-dependencies run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "paycalc.jcl.lineage.json"
JOB = REPO / "examples" / "paycalc.jcl"

#: Where jcl-dependencies lives when it is not installed. Same convention as
#: MAINFRAME_COMMON_REPO: a sibling checkout beside this one.
JCL_REPO = REPO.parent / "jcl-dependencies"

#: Exactly what `views.bind_jcl_ddnames` reads. Nothing else about the view is this
#: package's business, and asserting more would make an unrelated JCL change a red test
#: here.
REQUIRED_TOP = ("format", "job", "source", "ddBindings")
REQUIRED_BINDING = ("program", "step", "ddname", "dataset", "io")

#: The ddnames paycalc.asm declares. If the job stops binding one of them, the join this
#: package exists to close is silently narrower.
EXPECTED_DDNAMES = {"CUSTMAST", "PAYHIST", "PAYRPT"}


def check() -> List[str]:
    problems: List[str] = []
    if not FIXTURE.is_file():
        return ["{0} is missing - regenerate it with --record".format(FIXTURE)]
    try:
        view = json.loads(FIXTURE.read_text(encoding="utf-8"))
    except ValueError as exc:
        return ["{0} is not readable JSON ({1})".format(FIXTURE, exc)]

    for key in REQUIRED_TOP:
        if key not in view:
            problems.append("the view has no {0!r} key".format(key))
    if view.get("format") != "jcl-dependencies-lineage":
        problems.append(
            "the view's 'format' is {0!r}, not 'jcl-dependencies-lineage' - the binder "
            "refuses anything else, so this fixture would bind nothing".format(
                view.get("format")))

    bindings = view.get("ddBindings") or []
    if not bindings:
        problems.append("the view has no ddBindings - there is nothing to bind against")
    for index, binding in enumerate(bindings):
        for key in REQUIRED_BINDING:
            if key not in binding:
                problems.append(
                    "ddBindings[{0}] has no {1!r} key - the binder reads it".format(
                        index, key))

    found = {b.get("ddname") for b in bindings if b.get("program") == "PAYCALC"}
    missing = EXPECTED_DDNAMES - found
    if missing:
        problems.append(
            "the job no longer binds {0} for PAYCALC - the ddname->dataset join this "
            "package exists to close is narrower than the tests assume".format(
                ", ".join(sorted(missing))))
    return problems


def record() -> int:
    """Regenerate by actually running jcl-dependencies, not by hand-editing the JSON."""
    src = JCL_REPO / "src"
    if not (src / "jcl_dependencies" / "__init__.py").is_file():
        print("error: jcl-dependencies is not at {0} - clone it beside this repo, or "
              "install it and re-run".format(JCL_REPO), file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        env_path = "{0}{1}{2}".format(src, ";" if sys.platform == "win32" else ":",
                                      REPO.parent / "mainframe-common" /
                                      "mainframe-artifacts" / "src")
        proc = subprocess.run(
            [sys.executable, "-m", "jcl_dependencies", str(JOB), "--outdir", tmp,
             "--no-fetch", "--target", "lineage", "-q"],
            capture_output=True, text=True,
            env={**dict(__import__("os").environ), "PYTHONPATH": env_path})
        produced = Path(tmp) / "paycalc.jcl.lineage.json"
        if not produced.is_file():
            print("error: jcl-dependencies produced no lineage view\n{0}".format(
                proc.stderr), file=sys.stderr)
            return 1
        FIXTURE.write_text(produced.read_text(encoding="utf-8"), encoding="utf-8",
                           newline="\n")
    print("recorded {0} from a jcl-dependencies run over {1}".format(FIXTURE, JOB.name))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="assert the shape the binder reads (the test suite runs this)")
    group.add_argument("--record", action="store_true",
                       help="regenerate the fixture by running jcl-dependencies")
    args = parser.parse_args()

    if args.record:
        return record()
    problems = check()
    if problems:
        print("CONTRACT DRIFT: {0} problem(s) with {1}".format(len(problems), FIXTURE),
              file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print("contract intact: {0}".format(FIXTURE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
