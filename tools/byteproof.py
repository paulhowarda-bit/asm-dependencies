#!/usr/bin/env python3
"""Byte-stability ratchet: hash every view of every example, and refuse to let a refactor
change one byte of it.

Output here is a contract, not a rendering: a dependency manifest is read, diffed, joined
against a JCL job's DD bindings and loaded into a graph. A refactor that should not change
output must produce identical bytes, and a green test run does not prove that - the ORDER
of a candidate list, the presence of a reason, the key order of a row are all output and
none of them is asserted anywhere.

What is hashed is the EXACT TEXT the CLI would write - ``json.dumps(obj, indent=2) +
"\\n"`` - not a normalized or re-parsed form. A view that reorders its keys, changes its
indent, or gains a trailing newline is a changed view, and this must say so.

The VIEWS are deliberately estate-free: every module is parsed with NO resolver, so an
unresolved macro stays unresolved and is hashed as such. Supplying one would make the
hashes depend on what an estate happened to answer. The two RETRIEVAL REPORTS are not
estate-free - their contents ARE what a service answered - so they run against
``tests/fakes/estate.py``, which answers from a fixed table.

Each module is also hashed **twice for &SYSPARM**: once with none supplied and once with a
value. Conditional assembly decides which members and calls exist at all, so the two runs
are genuinely different output, and a change that collapsed them - or stopped collapsing
them - would otherwise be invisible.

    python tools/byteproof.py --record goldens/views.sha256
    python tools/byteproof.py --check  goldens/views.sha256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Callable, Dict, List

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
FIXTURES = REPO / "tests" / "fixtures"

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))     # the recorded fake estate client

# mainframe-artifacts arrives installed or from the sibling mainframe-common checkout
# (override with MAINFRAME_COMMON_REPO) - the same discovery the test suite performs.
from _mainframe_common import ensure_on_path                        # noqa: E402
_missing = ensure_on_path()
if _missing is not None:
    raise SystemExit("error: {0}".format(_missing))

from fakes.estate import fetch_artifact                             # noqa: E402

from mainframe_artifacts.fetch import fetch_dependencies            # noqa: E402

from asm_dependencies.parser import parse_asm                       # noqa: E402
from asm_dependencies.prefetch import prefetch_asm                  # noqa: E402
from asm_dependencies.views import (bind_jcl_ddnames,               # noqa: E402
                                    build_asm_artifacts,
                                    build_asm_lineage)

INDENT = 2  # the CLI default; the hashes are of what a default run would write

#: Every module here is hashed. `.cpy` is absent on purpose: a copy member is a fragment,
#: not a module, and analysing one as though it were would hash a model of something that
#: never runs on its own.
PROGRAM_SUFFIXES = (".asm", ".mlc")

#: The &SYSPARM values every module is hashed under. `None` is the honest default - no
#: value supplied, so both arms of every undecided AIF are reported.
SYSPARMS = (None, "PROD")

#: The one example the committed JCL lineage fixture covers.
BOUND_EXAMPLE = "paycalc.asm"


def normalize(text: str, run_dir: Path = None) -> str:
    """Replace this checkout's directories - and a run's own output directory - with
    stable tokens, so goldens are portable.

    Three forms of each path, because a JSON-escaped Windows path is neither the native
    form nor the forward-slash one. Nothing else is normalized: the point is to catch a
    changed byte, not to launder one.
    """
    out = text
    roots = [(EXAMPLES, "<EXAMPLES>"), (REPO, "<REPO>")]
    if run_dir is not None:
        roots.insert(0, (run_dir, "<RUNDIR>"))
    for path, token in roots:
        native = str(path)
        for form in (native, native.replace("\\", "/"), native.replace("\\", "\\\\")):
            out = out.replace(form, token)
    return out


def json_text(obj) -> str:
    return json.dumps(obj, indent=INDENT) + "\n"


def digest(text: str, run_dir: Path = None) -> str:
    return hashlib.sha256(normalize(text, run_dir).encode("utf-8")).hexdigest()


def guarded(build: Callable[[], str]) -> str:
    """A view that stops being produced is exactly the kind of silent loss this exists to
    catch, so an exception is hashed rather than raised."""
    try:
        return build()
    except Exception:
        return "ERROR:\n" + traceback.format_exc(limit=0)


def views(path: Path, sysparm) -> Dict[str, str]:
    """The two views, with NO estate: an unresolved macro stays unresolved and is hashed
    as such."""
    source = path.read_text(encoding="utf-8")
    out: Dict[str, str] = {}

    def parse():
        return parse_asm(source, resolver=None, source_name=path.name, sysparm=sysparm)

    out["asm.artifacts"] = guarded(lambda: json_text(build_asm_artifacts(parse())))
    out["asm.lineage"] = guarded(lambda: json_text(build_asm_lineage(parse())))

    if path.name == BOUND_EXAMPLE:
        lineage = json.loads(
            (FIXTURES / "paycalc.jcl.lineage.json").read_text(encoding="utf-8"))
        out["asm.artifacts.bound"] = guarded(
            lambda: json_text(bind_jcl_ddnames(build_asm_artifacts(parse()), lineage)))
    return out


def reports(path: Path, sysparm, run_dir: Path) -> Dict[str, str]:
    """The two retrieval reports, against the fake estate.

    This is the half of the ratchet that guards the record-and-replay closure - the
    ordering of its rounds, the wording of a MISSING row, the fetch plan's vocabulary -
    which is the machinery a refactor is most likely to shorten silently.
    """
    source = path.read_text(encoding="utf-8")
    dest = str(run_dir / "deps")

    def build() -> Dict[str, str]:
        pre = prefetch_asm(source, fetch_artifact, paths=[str(EXAMPLES)], dest=dest,
                           source_name=path.name, jobs=1, sysparm=sysparm)
        module = parse_asm(source, resolver=pre.resolver(), source_name=path.name,
                           sysparm=sysparm)
        art = build_asm_artifacts(module)
        fetched = fetch_dependencies(art, fetch_artifact, dest=dest,
                                     prefetched=pre.store, jobs=1)
        return {"asm.prefetch": json_text(pre.report()),
                "asm.fetch": json_text(fetched)}

    try:
        return build()
    except Exception:
        text = "ERROR:\n" + traceback.format_exc(limit=0)
        return {"asm.prefetch": text, "asm.fetch": text}


def build_manifest() -> Dict[str, str]:
    out: Dict[str, str] = {}
    modules = sorted(p for p in EXAMPLES.iterdir()
                     if p.suffix.lower() in PROGRAM_SUFFIXES)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        for path in modules:
            for sysparm in SYSPARMS:
                label = "sysparm={0}".format(sysparm or "-")
                for name, text in views(path, sysparm).items():
                    out["{0}::{1}::{2}".format(path.name, label, name)] = digest(text)
                for name, text in reports(path, sysparm, run_dir).items():
                    out["{0}::{1}::{2}".format(path.name, label, name)] = digest(
                        text, run_dir)
    return dict(sorted(out.items()))


def dump(manifest: Dict[str, str], path: Path) -> None:
    lines = ["{0}  {1}".format(sha, key) for key, sha in manifest.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sha, _, key = line.partition("  ")
        out[key] = sha
    return out


def compare(now: Dict[str, str], golden: Dict[str, str]) -> List[str]:
    problems: List[str] = []
    for key in sorted(set(golden) | set(now)):
        if key not in now:
            problems.append("MISSING  {0}".format(key))
        elif key not in golden:
            problems.append("ADDED    {0}".format(key))
        elif now[key] != golden[key]:
            problems.append("CHANGED  {0}\n         golden {1}\n         now    {2}".format(
                key, golden[key], now[key]))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hash every view of every example and compare against the goldens.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", metavar="FILE",
                       help="rebuild the goldens. DELIBERATELY only - an output change "
                            "must be intended and reviewed before it is recorded.")
    group.add_argument("--check", metavar="FILE", help="compare against the goldens")
    args = parser.parse_args()

    now = build_manifest()

    if args.record:
        target = Path(args.record)
        target.parent.mkdir(parents=True, exist_ok=True)
        dump(now, target)
        print("recorded {0} view digests -> {1}".format(len(now), target))
        return 0

    target = Path(args.check)
    if not target.is_file():
        print("error: no goldens at {0} - record them first with --record".format(target),
              file=sys.stderr)
        return 2
    problems = compare(now, load(target))
    if problems:
        print("BYTE-STABILITY FAILURE: {0} difference(s)".format(len(problems)),
              file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print("byte-stable: {0} view digests match {1}".format(len(now), target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
