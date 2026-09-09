"""The assembler front-end as a library: analyze a module, get its dependencies.

The same shape as the COBOL, JCL and Easytrieve sides' ``api`` modules, and for the same
reason: driving this from another Python program should be the code path the command line
takes, not a second one that drifts from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

from mainframe_artifacts.bundle import EstateBundle, recording_fetcher, write_bundle
from mainframe_artifacts.fetch import fetch_dependencies
from mainframe_artifacts.prefetch import PrefetchResult
from mainframe_artifacts.profiling import StageTimer
from mainframe_artifacts.synonyms import SynonymLookup

from .lexer import Margins
from .model import Module
from .parser import parse_asm
from .prefetch import prefetch_asm
from .views import bind_jcl_ddnames, build_asm_artifacts, build_asm_lineage

_log = logging.getLogger(__name__)


@dataclass
class ModuleAnalysis:
    """One analyzed assembler module, and the views projected from it."""

    module: Module
    prefetch: PrefetchResult
    source_name: str = "<asm>"
    fetch: Optional[dict] = None
    #: Db2 SYNONYM/ALIAS knowledge (a map, a host resolver, or both) - None when the
    #: run opened neither door, and then every table is reported as written.
    synonyms: Optional[SynonymLookup] = None

    _lineage: Optional[dict] = field(default=None, repr=False)
    _artifacts: Optional[dict] = field(default=None, repr=False)

    def lineage(self) -> dict:
        """Every site in source order, with the evidence for each target."""
        if self._lineage is None:
            self._lineage = build_asm_lineage(self.module)
        return self._lineage

    def artifacts(self) -> dict:
        """The manifest: what this module provides, and everything it depends on."""
        if self._artifacts is None:
            self._artifacts = build_asm_artifacts(self.module, synonyms=self.synonyms)
        return self._artifacts

    def bind(self, jcl_lineage: dict, *, steps: Sequence[str] = ()) -> dict:
        """Close the ddname->dataset join using a JCL job's lineage view.

        Takes a plain dict and returns one - this package never imports the JCL one.
        """
        return bind_jcl_ddnames(self.artifacts(), jcl_lineage, steps=steps)


def analyze(source: str, *, source_name: str = "<asm>",
            bundle: Optional[EstateBundle] = None,
            fetcher: Optional[Any] = None,
            retrieve: bool = True,
            paths: Sequence[str] = (), dest: Optional[str] = None,
            unavailable: Optional[str] = None,
            program_name: Optional[str] = None,
            margins: Optional[Margins] = None,
            sysparm: Optional[str] = None,
            exts: Sequence[str] = (),
            max_rounds: int = 12, jobs: int = 1,
            timer: Optional[StageTimer] = None,
            synonyms: Optional[Dict[str, str]] = None,
            synonym_resolver: Optional[Callable[[str], Optional[str]]] = None,
            ) -> ModuleAnalysis:
    """Retrieve, parse and model one assembler module.

    Stage 1 is not optional decoration here. A site macro wrapping ``LINK``/``XCTL``/
    ``LOAD`` is the normal way large shops write assembler, so a module parsed without its
    macro libraries has **no calls at all** - and its manifest looks exactly like the
    manifest of a module that genuinely calls nothing.

    ``sysparm`` is the value of ``&SYSPARM``, which on a real assembly arrives as ``PARM=``
    on the assemble step's JCL. Supply it and conditional branches that read it are
    decided; leave it out and both arms are kept, each carrying the test that governs it.

    The estate is reached the same four ways as everywhere else in the family: through
    ``fetcher``, not at all (``fetcher=None``), deliberately off (``retrieve=False``), or
    replayed from a gathered ``bundle``.
    """
    timer = timer or StageTimer(_log, False, source_name)

    if bundle is not None:
        fetcher = bundle.fetcher()
        unavailable = unavailable or bundle.unavailable
    elif not retrieve:
        fetcher = None
        unavailable = unavailable or ("retrieval was disabled for this run, so this "
                                      "member was never looked for")

    with timer.stage("prefetch"):
        pre = prefetch_asm(source, fetcher, paths=list(paths), dest=dest,
                           source_name=source_name, unavailable=unavailable,
                           max_rounds=max_rounds, jobs=jobs, exts=exts,
                           sysparm=sysparm)
    with timer.stage("parse"):
        module = parse_asm(source, resolver=pre.resolver(), source_name=source_name,
                           program_name=program_name, margins=margins, sysparm=sysparm)

    lookup = (SynonymLookup(synonyms, synonym_resolver)
              if (synonyms or synonym_resolver is not None) else None)
    analysis = ModuleAnalysis(module=module, prefetch=pre, source_name=source_name,
                              synonyms=lookup)
    with timer.stage("asm-lineage"):
        analysis.lineage()
    with timer.stage("asm-artifacts"):
        art = analysis.artifacts()
    with timer.stage("fetch"):
        analysis.fetch = fetch_dependencies(art, fetcher, dest=dest,
                                            prefetched=pre.store,
                                            unavailable=unavailable, jobs=jobs)
    return analysis


def gather(source: str, *, source_name: str = "<asm>",
           fetcher: Optional[Any] = None,
           paths: Sequence[str] = (), dest: str,
           unavailable: Optional[str] = None,
           sysparm: Optional[str] = None,
           margins: Optional[Margins] = None, exts: Sequence[str] = (),
           max_rounds: int = 12, jobs: int = 1) -> str:
    """Run the retrieval half where the estate is reachable; return the bundle manifest."""
    recorder, answers = recording_fetcher(fetcher) if fetcher is not None else (None, [])
    analysis = analyze(source, source_name=source_name, fetcher=recorder, paths=paths,
                       dest=dest, unavailable=unavailable, margins=margins,
                       sysparm=sysparm, exts=exts, max_rounds=max_rounds, jobs=jobs)
    return write_bundle(dest, subject_name=source_name, subject_text=source,
                        kind="asm", prefetch=analysis.prefetch, answers=answers,
                        fetch=analysis.fetch)
