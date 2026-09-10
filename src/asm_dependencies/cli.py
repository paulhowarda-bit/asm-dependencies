"""Command-line entry point: an assembler module -> what it provides and what it needs."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

from mainframe_artifacts.artifact_service import decode_member, load_fetcher
from mainframe_artifacts.bundle import open_bundle
from mainframe_artifacts.cliargs import (add_dependents_args, add_logging_args,
                                         add_output_args, add_retrieval_args,
                                         add_synonym_args, dependents_lookup,
                                         jobs as _jobs, synonym_lookup)
from mainframe_artifacts.errors import CobolXstateError
from mainframe_artifacts.logging_setup import PACKAGE_LOGGER as CORE_LOGGER
from mainframe_artifacts.logging_setup import configure_logging
from mainframe_artifacts.output import make_run_dir, run_dir, write_json
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.report import report_stages

from . import PACKAGE_LOGGER
from .api import analyze, gather
from .detect import classify
from .views import JCL_LINEAGE_FORMAT, bind_jcl_ddnames

# Explicit name, NOT __name__: this module is also run as
# `python -m asm_dependencies.cli`, where __name__ == "__main__" would put the logger
# outside the package hierarchy and out of configure_logging's reach (so INFO/progress
# would be silently dropped).
_log = logging.getLogger("asm_dependencies.cli")

# The dependents view is not a --target choice: it is not a view you ask for, it is an
# answer you were given, so it is written exactly when a lookup supplied one.
_SUFFIXES = (".asm.artifacts.json", ".asm.lineage.json", ".asm.dependents.json",
             ".asm.prefetch.json", ".asm.fetch.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="asm-dependencies",
        description="Parse an IBM assembler (HLASM) module and emit the entry points it "
                    "provides, the modules it calls or loads, the ddnames it declares, "
                    "and the manifest of everything it depends on - following COPY "
                    "members and macros, because a site macro wrapping LINK is where most "
                    "of the calls in a real estate live.",
    )
    p.add_argument("source", help="path to an assembler module ('-' for stdin)")
    p.add_argument("--target", choices=["both", "artifacts", "lineage"], default="both",
                   help="which views to write (default: both). artifacts = the "
                        "deduplicated manifest; lineage = every site in source order with "
                        "the evidence for each target, plus the unresolved inventory.")
    p.add_argument("-I", "--path", "--macro-path", dest="path", action="append",
                   default=[], metavar="DIR",
                   help="directory to search for COPY members and macros before asking "
                        "the estate (repeatable)")
    p.add_argument("--macro-ext", dest="macro_ext", action="append", default=[],
                   metavar="EXT",
                   help="extra file extension to try when looking for a member on the "
                        "search path, e.g. --macro-ext .lib (repeatable). The assembler "
                        "defaults (.mac/.asm/.cpy/...) are always tried as well.")
    p.add_argument("--sysparm", metavar="VALUE",
                   help="the value of &SYSPARM, which on a real assembly arrives as PARM= "
                        "on the assemble step's JCL. Conditional assembly decides which "
                        "COPY members and calls exist at all, so WITHOUT this both arms of "
                        "every undecided AIF are reported, each carrying the test that "
                        "governs it; WITH it they collapse to the one that is assembled.")
    p.add_argument("--right-margin", type=int, default=71, metavar="COL",
                   help="the last column the assembler scans (default: %(default)s). "
                        "Column 72 is the continuation indicator and 73-80 is the "
                        "identification-sequence field, which the assembler never reads. "
                        "An ICTL statement in the source overrides this.")
    p.add_argument("--program-name", metavar="NAME",
                   help="the module's identity (default: the name of its first control "
                        "section, falling back to the source member name).")
    p.add_argument("--bind-jcl", metavar="FILE",
                   help="a jcl-dependencies LINEAGE view (*.jcl.lineage.json). Its "
                        "ddBindings resolve this module's ddnames to real datasets, "
                        "closing the one thing assembler source cannot say. WHICH step "
                        "runs this module is found from EXEC PGM=, matched against every "
                        "name the module provides. Implies --target artifacts.")
    p.add_argument("--bind-step", action="append", default=[], metavar="STEP",
                   help="bind only from this JCL step (repeatable). Use it when a job "
                        "runs the module in more than one step against different data.")
    p.add_argument("--max-rounds", type=int, default=12, metavar="N",
                   help="how deep to follow COPY and macro nesting when closing over the "
                        "module (default: 12). The closure is bounded because a member set "
                        "deeper than this is more likely a resolver loop than a real "
                        "module; hitting the bound is REPORTED, never silently treated as "
                        "a complete closure.")
    add_retrieval_args(p)
    add_synonym_args(p)
    add_dependents_args(p)
    add_output_args(p, outdir_help=(
        "directory for output (default: ./out). EVERY file this run produces goes here, "
        "exactly as given with nothing appended - both views, both retrieval reports, and "
        "the members retrieved from the estate (under deps/). Created with parents if it "
        "does not exist."))
    add_logging_args(p)
    return p


def _service(args, source_name: str):
    """The estate artifact service for this run, and why it is missing if it is.

    Never fatal. A run without the service still parses whatever is on the local search
    path and still writes its reports - they simply say, per member, that nothing was ever
    looked for.
    """
    fetcher, why = load_fetcher(args.copybook_fetcher)
    if fetcher is None:
        _log.warning("[{0}] WARNING: {1}".format(source_name, why))
    return fetcher, why


def _load_jcl(path_text: str) -> dict:
    """The JCL lineage view: ddBindings, and the per-step EXEC PGM= this module matches."""
    path = Path(path_text)
    lineage = json.loads(path.read_text(encoding="utf-8"))
    if lineage.get("format") != JCL_LINEAGE_FORMAT:
        raise CobolXstateError(
            "--bind-jcl {0}: this is not a jcl-dependencies LINEAGE view (its 'format' is "
            "{1!r}). The binding reads 'ddBindings', which only that view carries - point "
            "it at the *.jcl.lineage.json a jcl-dependencies run wrote.".format(
                path, lineage.get("format")))
    return lineage


def run(argv: Optional[List[str]] = None, timing_sink=None) -> int:
    """Parse args, configure logging, and dispatch, behind the top-level error boundary."""
    args = build_parser().parse_args(argv)
    # BOTH roots: retrieval logs from mainframe_artifacts.*, everything else from
    # asm_dependencies.*. A root nobody configures propagates to the root logger, or
    # prints WARNING+ via logging's lastResort - which would end -qq's silence.
    configure_logging(verbose=args.verbose or (1 if args.debug else 0), quiet=args.quiet,
                      loggers=(CORE_LOGGER, PACKAGE_LOGGER))
    try:
        return _run(args, timing_sink=timing_sink)
    except CobolXstateError as exc:
        _log.error("%s", exc)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        _log.error("interrupted")
        return 130
    except Exception:
        if args.debug:
            raise
        _log.critical("internal error while processing %r - re-run with --debug for the "
                      "full traceback", args.source)
        _log.debug("internal error traceback", exc_info=True)
        return 1


def _run(args, timing_sink=None) -> int:
    paths = list(args.path)
    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
        default_stem = None
    else:
        path = Path(args.source)
        if not path.exists():
            _log.error("error: no such file: {0}".format(path))
            return 2
        source = decode_member(path.read_bytes())
        source_name = path.name
        default_stem = path.stem
        # A COPY member or a macro most often sits beside the module that uses it.
        paths.append(str(path.parent))

    ok, why = classify(source_name, source)
    if not ok:
        _log.warning("[{0}] WARNING: {1}".format(source_name, why))

    timer = StageTimer(_log, args.timing, source_name, sink=timing_sink)

    if args.gather_only and args.from_bundle:
        _log.error("error: --gather-only writes a bundle and --from-bundle reads one; "
                   "they cannot both apply to a single run")
        return 2

    bundle = None
    if args.from_bundle:
        try:
            bundle = open_bundle(args.from_bundle)
        except CobolXstateError as exc:
            _log.error("error: {0}".format(exc))
            return 2

    jcl_lineage = None
    if args.bind_jcl:
        jcl_path = Path(args.bind_jcl)
        if not jcl_path.is_file():
            _log.error("error: --bind-jcl {0}: no such file".format(jcl_path))
            return 2
        try:
            jcl_lineage = _load_jcl(args.bind_jcl)
        except ValueError as exc:
            _log.error("error: --bind-jcl {0} is not readable JSON ({1})".format(
                jcl_path, exc))
            return 2

    lookup, why_synonyms = synonym_lookup(args)
    if why_synonyms:
        _log.error(f"error: {why_synonyms}")
        return 2

    reverse, why_dependents = dependents_lookup(args)
    if why_dependents:
        _log.error(f"error: {why_dependents}")
        return 2

    fetcher, why_service = (None, None) if bundle is not None \
        else _service(args, source_name)

    out_dir = run_dir(args.outdir)
    err = make_run_dir(out_dir)
    if err:
        _log.error("error: {0}".format(err))
        return 2
    deps = str(out_dir / "deps")

    margins = _margins(args.right_margin)

    if args.gather_only:
        gathered = gather(source, source_name=source_name, fetcher=fetcher, paths=paths,
                          dest=args.gather_only, unavailable=why_service,
                          sysparm=args.sysparm, margins=margins,
                          exts=tuple(args.macro_ext), max_rounds=args.max_rounds,
                          jobs=_jobs(args),
                          dependents=reverse.mapping if reverse is not None else None,
                          dependents_resolver=(reverse.resolver if reverse is not None
                                               else None))
        _log.info("[{0}] wrote estate bundle {1}".format(source_name, gathered))
        _log.info("[{0}] model from it with: --from-bundle {1}".format(
            source_name, args.gather_only))
        timer.report()
        return 0

    analysis = analyze(source, source_name=source_name, bundle=bundle, fetcher=fetcher,
                       retrieve=not args.no_fetch, paths=paths, dest=deps,
                       unavailable=why_service, program_name=args.program_name,
                       margins=margins, sysparm=args.sysparm,
                       exts=tuple(args.macro_ext), max_rounds=args.max_rounds,
                       jobs=_jobs(args), timer=timer,
                       synonyms=lookup.mapping if lookup is not None else None,
                       synonym_resolver=lookup.resolver if lookup is not None else None,
                       dependents=reverse.mapping if reverse is not None else None,
                       dependents_resolver=(reverse.resolver if reverse is not None
                                            else None))
    module = analysis.module
    base = default_stem or module.name or "module"

    wanted = set({"both": ("artifacts", "lineage")}.get(args.target, (args.target,)))
    if jcl_lineage is not None and "artifacts" not in wanted:
        _log.info("[{0}] --bind-jcl binds the ARTIFACTS view, so it is written too".format(
            source_name))
        wanted.add("artifacts")

    artifacts = analysis.artifacts() if "artifacts" in wanted else None
    if artifacts is not None and jcl_lineage is not None:
        with timer.stage("bind-jcl"):
            artifacts = bind_jcl_ddnames(artifacts, jcl_lineage,
                                         steps=tuple(args.bind_step))
        binding = artifacts["jclBinding"]
        _log.info("[{0}] bound {1} ddname(s) from {2} via {3}".format(
            source_name, binding["boundFiles"], binding.get("source") or args.bind_jcl,
            binding["basis"]))

    written = {
        ".asm.artifacts.json": artifacts,
        ".asm.lineage.json": analysis.lineage() if "lineage" in wanted else None,
        # None when no door was opened, and the loop below writes nothing for a None -
        # so a run nobody told anything produces exactly the files it always did.
        ".asm.dependents.json": analysis.dependents(),
        ".asm.prefetch.json": analysis.prefetch.report(),
        ".asm.fetch.json": analysis.fetch,
    }
    for suffix in _SUFFIXES:
        obj = written.get(suffix)
        if obj is None:
            continue
        target = out_dir / "{0}{1}".format(base, suffix)
        write_json(target, obj, args.indent)
        _log.info("[{0}] wrote {1}".format(source_name, target))
    report_stages(_log, source_name, analysis.prefetch, analysis.fetch)

    if args.summary:
        _summary(module, analysis)

    timer.report()
    return 0


def _margins(right_margin: int):
    """``--right-margin`` as a :class:`~asm_dependencies.lexer.Margins`, or ``None``.

    ``None`` means "whatever an ICTL in the source says, else the defaults", which is the
    right behaviour for almost every member - so the flag only takes effect when it is
    actually given something other than the default.
    """
    from .lexer import DEFAULT_END, Margins
    if right_margin == DEFAULT_END:
        return None
    return Margins(begin=1, end=right_margin or 80, cont=16)


def _summary(module, analysis) -> None:
    """A scoreboard including the zeros: what was found, and what could not be."""
    lineage = analysis.lineage()
    unresolved = lineage["unresolved"]
    _log.info("[{0}] provides {1} entry point(s); {2} call site(s), {3} external "
              "reference(s), {4} ddname(s), {5} member(s); {6} unresolved site(s), "
              "{7} flag(s)".format(
                  module.name, len(lineage["provides"]), len(lineage["calls"]),
                  len(lineage["externals"]), len(lineage["files"]),
                  len(lineage["members"]), len(unresolved), len(lineage["flags"])))
    for row in lineage["provides"]:
        _log.info("  PROVIDES {0} ({1})".format(row["name"], row["kind"]))
    for call in lineage["calls"]:
        _log.info("  {0} {1} [{2}]".format(
            call["verb"], call["target"] or "?", call["evidence"]))
    for row in unresolved:
        _log.info("  UNRESOLVED {0} line {1}: {2}".format(
            row["site"], row["line"], row["reason"]))
    for flag in lineage["flags"]:
        _log.info("  FLAG {0}".format(flag))


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
