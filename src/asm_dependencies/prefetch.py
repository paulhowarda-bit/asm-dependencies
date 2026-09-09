"""Stage 1: close over the COPY members and macros a module is assembled from.

**Discovered by record-and-replay, not by scanning.** Parse the module with a resolver
that fetches nothing and merely records what it was asked for, retrieve those, then
re-parse with them in hand - repeating until the parse stops asking for anything new.

A lexical scan for ``COPY`` would have been easy and would have been wrong twice over.
A macro invokes macros, and the inner invocation only becomes visible once the outer
macro's text is in hand. Worse, in assembler you cannot scan for the macro calls at all:
they have no marker. ``@LINK PGM=PAYCALC`` is a macro call and ``LR R15,R0`` is a machine
instruction, and the only thing that distinguishes them is whether the operation code is
in the assembler's tables - so the questions have to be asked in the order the assembler
asks them, which is what replaying the parse does.

Why it matters more here than anywhere else in this family: a site macro wrapping
``LINK``/``XCTL``/``LOAD`` is the normal way large shops write assembler. A module parsed
without its macro libraries has **no calls at all**, and its manifest looks exactly like
the manifest of a module that genuinely calls nothing.

That is also the contract this file depends on, and it is easy to break from the other
side: anything that memoizes resolution inside ``macros.MacroExpander._resolve``,
short-circuits when the resolver returns ``None``, or adds a second resolution path will
silently shorten the closure - no error, just a module that reads as though it had fewer
dependencies than it has.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from mainframe_artifacts.prefetch import (PrefetchResult, Prefetcher,  # noqa: F401
                                          member_key)

from .parser import parse_asm

#: Extensions tried on the local search path before the estate is asked. The shared engine
#: already tries the COBOL and JCL ones; these are the names an assembler source, macro or
#: copy member is kept under. Without them a macro sitting right beside the module would be
#: reported MISSING and then fetched from the estate - shadowing the local file that the
#: ``-I`` flag pointed at.
MACRO_EXTS: Tuple[str, ...] = (".mac", ".MAC", ".asm", ".ASM", ".mlc", ".MLC",
                               ".hlasm", ".HLASM", ".cpy", ".CPY", ".copy", ".COPY",
                               ".inc", ".INC")

#: Why each member was wanted, printed verbatim in the retrieval report.
_WHY = "referenced by the module (COPY member or macro)"


def prefetch_asm(source: str, fetcher: Optional[Callable],
                 paths: Optional[List[str]] = None, dest: Optional[str] = None,
                 source_name: str = "<asm>", max_rounds: int = 12,
                 unavailable: Optional[str] = None,
                 result: Optional[PrefetchResult] = None,
                 jobs: int = 1,
                 exts: Sequence[str] = (),
                 sysparm: Optional[str] = None,
                 seen: Optional[Iterable[str]] = None,
                 producer: Optional[str] = None) -> PrefetchResult:
    """Close over the members a module needs, by replaying the parse until it stops asking.

    No type hint is passed: the estate service auto-detects, and its ``detected_type`` is a
    better answer than anything that could be inferred from an operation code - which, for
    a site macro, is exactly what we do not yet know the meaning of.

    ``sysparm`` matters here and not only at parse time. Conditional assembly decides which
    ``COPY`` members exist, so the same module asks for different members under different
    ``PARM=`` values; without one, both arms are followed and both are retrieved, which is
    the conservative direction.
    """
    pf = Prefetcher(fetcher, paths, dest, unavailable, result,
                    exts=tuple(exts) + MACRO_EXTS, seen=seen,
                    producer=producer)
    pf.name_source(source_name)

    # COPY members are closed FIRST, and only then are unknown operations treated as
    # library macros. The order matters because a COPY member routinely carries the shop's
    # linkage macros: asking for `SITELINK` while `COPY PAYREC` is still unresolved gets a
    # not-found for a macro that PAYREC defines inline one round later - and that
    # not-found is then permanent, because a member is only ever asked for once.
    resolve_macros = False

    for _ in range(max_rounds):
        asked: List[str] = []

        def recording(name: str, _asked=asked) -> Optional[str]:
            _asked.append(name)
            return pf.store_text(name) or pf.result.resolver()(name)

        parse_asm(source, resolver=recording, source_name=source_name, sysparm=sysparm,
                  resolve_macros=resolve_macros)
        fresh = [n for n in asked if member_key(n) not in pf.seen]
        if not fresh:
            if resolve_macros:
                break
            resolve_macros = True       # the COPY closure is complete; now the macros
            continue
        # One round IS a level: everything the parse asked for this time round was asked
        # for before any of it came back, so it can all be retrieved together.
        pf.obtain_wave([(n, _WHY) for n in fresh], None, jobs)
    else:
        pf.note_closure_bound(max_rounds)
    return pf.result
