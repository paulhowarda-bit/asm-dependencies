"""Statements -> a :class:`~asm_dependencies.model.Module`.

The parser knows what an operation means. It does not know about columns (the lexer does),
about which text exists (``conditional.py`` does), about where a member comes from
(``macros.py`` does), or about JSON (``views.py`` does).

Two rules govern what it records:

**An unrecognised operation is a macro call, not noise.** That is literally how HLASM
resolves an operation code, and it is the single most important thing to get right on real
source: shops wrap ``LINK``/``XCTL``/``LOAD`` in site macros, so the interesting calls are
behind names this module has never heard of. Every unknown operation becomes a macro the
retrieval stage goes looking for, and one that cannot be found is flagged rather than
dropped - because behind it may be every call the module makes.

**A V-type address constant is an implicit EXTRN.** ``DC V(PAYCALC)`` and ``L R15,=V(PAYCALC)``
declare an external dependency without any ``EXTRN`` statement, and they are the highest-
confidence dependency signal assembler has. ``A``-type constants are *not* external unless
the symbol is separately declared, which is why external resolution is a second pass: the
``EXTRN`` may come after the use.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .lexer import (
    STATEMENT, SYMBOL, Margins, Statement, is_literal, is_register_form, keyword,
    literal_value, logical_lines, operands,
)
from .model import (
    EVIDENCE_DYNAMIC, EVIDENCE_LITERAL, VIA_CALL, VIA_EXTRN, VIA_JCON, VIA_QCON,
    VIA_RCON, VIA_VCON, VIA_WXTRN, DataDef, Entry, ExternalRef, FileRef, Invocation,
    MemberUse, Module, ResourceRef, Section, Store,
)
from . import dataflow, opcodes

logger = logging.getLogger(__name__)

Resolver = Callable[[str], Optional[str]]

#: A ``DC``/``DS`` address-constant operand: ``V(PAYCALC)``, ``3AL4(X)``, ``Q(MYDXD)``.
_ADCON = re.compile(
    r"^(?P<dup>\d+)?(?P<type>[AVYQJRS])(?:L(?P<len>\d+))?\((?P<body>.*)\)$", re.I)

#: The same, written as a literal so it lands in the literal pool: ``=V(PAYCALC)``. The
#: body pattern allows one level of nested parentheses, which covers ``=A(X-(Y+1))``.
_LITERAL_ADCON = re.compile(
    r"=(?P<dup>\d+)?(?P<type>[AVYQJRS])(?:L\d+)?"
    r"\((?P<body>[^()]*(?:\([^()]*\)[^()]*)*)\)", re.I)

#: A character constant, as an operand or as a literal.
_CHAR_DC = re.compile(
    r"^(?P<dup>\d+)?C(?:L(?P<length>\d+))?'(?P<body>(?:[^']|'')*)'$", re.I)
_CHAR_LITERAL = re.compile(r"=(?:\d+)?C(?:L\d+)?'(?P<body>(?:[^']|'')*)'", re.I)

#: ``PGMNAME``, ``PGMNAME(8)`` - the label a store writes into. A displacement form
#: (``0(R5)``, ``8(,R1)``) yields nothing, because no label is named.
_TARGET_LABEL = re.compile(r"^(?P<label>[A-Z@#$_][A-Z0-9@#$_]{0,62})(?:\(.*\))?$", re.I)

#: Address-constant types that always name an external symbol.
_EXTERNAL_ADCON = {"V": VIA_VCON, "Q": VIA_QCON, "J": VIA_JCON, "R": VIA_RCON}


def _sized(body: str, length: Optional[str]) -> str:
    """A character constant as the assembler lays it in storage.

    ``CL8'PAYCALC'`` occupies eight bytes, blank-padded; ``CL4'PAYCALC'`` is TRUNCATED to
    four. Both matter here because these values are compared against the eight-byte name
    fields that ``EPLOC=`` and ``DE=`` point at, and a truncated module name is a
    different module.
    """
    if not length:
        return body
    n = int(length)
    return body[:n] if len(body) > n else body.ljust(n)


def default_program_name(source_name: str) -> str:
    """The member name, as a fallback identity.

    Unlike Easytrieve, assembler HAS a real identity - the first control section's name -
    so this is only used when a module has no named section, which happens in a copy
    member or a fragment. Up to two extensions are stripped so ``PAYCALC.asm`` and
    ``PAYCALC.asm.txt`` give the same answer.
    """
    base = os.path.basename(source_name or "")
    for _ in range(2):
        stem, ext = os.path.splitext(base)
        if not ext:
            break
        base = stem
    return (base or "MODULE").upper()


def parse_asm(text: str, resolver: Optional[Resolver] = None,
              source_name: str = "<asm>", program_name: Optional[str] = None,
              margins: Optional[Margins] = None,
              sysparm: Optional[str] = None,
              resolve_macros: bool = True) -> Module:
    """Parse one assembler member.

    ``resolver`` supplies COPY and macro members. ``sysparm`` is the value of ``&SYSPARM``,
    which on a real assembly arrives as ``PARM=`` on the assemble step's JCL - supply it and
    conditional branches that read it are decided; leave it out and both arms are kept,
    each carrying the test that governs it.
    """
    return _Parser(text, resolver, source_name, program_name, margins, sysparm,
                   resolve_macros).parse()


class _Parser:

    def __init__(self, text: str, resolver: Optional[Resolver], source_name: str,
                 program_name: Optional[str], margins: Optional[Margins],
                 sysparm: Optional[str] = None,
                 resolve_macros: bool = True) -> None:
        self.module = Module(name=program_name or default_program_name(source_name),
                             source_name=source_name)
        self._named = program_name is not None
        self.resolver = resolver
        self._text = text
        self._margins = margins
        self._sysparm = sysparm
        self._resolve_macros = resolve_macros
        # section state
        self._section: Optional[str] = None
        self._sections: Dict[str, Section] = {}
        # deferred decisions - an EXTRN may come after the A-con that needs it, and a
        # V-con may name a section this module itself defines
        self._acons: List[ExternalRef] = []
        self._declared: Set[str] = set()          # EXTRN / WXTRN operands
        self._local: Set[str] = set()             # sections, ENTRYs, EQU labels
        self._aliases: Dict[str, str] = {}
        self._unknown: Dict[str, int] = {}        # operation -> first line seen
        self._unmodelled_cics: Dict[str, int] = {}

    # -- plumbing ------------------------------------------------------------------

    def _flag(self, text: str) -> None:
        if text not in self.module.flags:
            self.module.flags.append(text)

    def _flag_all(self, texts: Sequence[str]) -> None:
        for t in texts:
            self._flag(t)

    def parse(self) -> Module:
        from .conditional import Environment
        from .macros import MacroExpander

        stmts, lex_flags = logical_lines(self._text, margins=self._margins)
        self._flag_all(lex_flags)

        expander = MacroExpander(
            self.resolver, env=Environment(sysparm=self._sysparm),
            resolve_macros=self._resolve_macros)
        stmts = expander.expand(stmts)
        self.module.members = expander.uses
        self._flag_all(expander.flags)

        for stmt in stmts:
            if stmt.kind != STATEMENT:
                continue
            try:
                self._statement(stmt)
            except Exception as exc:            # one bad statement must not lose the rest
                self._flag("line {0}: could not be parsed ({1!r}) - {2!r}".format(
                    stmt.line, exc, stmt.text[:60]))
                logger.debug("assembler statement failed to parse", exc_info=True)

        self._resolve_externals()
        self._report_unknown()
        self._report_cics()
        self._report_hidden_structure()
        dataflow.resolve(self.module)
        return self.module

    def _statement(self, stmt: Statement) -> None:
        op = stmt.operation.upper()
        if not op:
            return
        handler = getattr(self, "_do_" + op.lower(), None)
        if handler is not None:
            handler(stmt)
        elif op in opcodes.IGNORED:
            pass
        elif op in opcodes.MACHINE:
            self._machine(stmt)
        else:
            self._unknown.setdefault(op, stmt.line)
        # A literal address constant can appear in ANY operand, including a macro call's,
        # so the literal-pool scan is unconditional rather than part of one handler.
        self._scan_literals(stmt)

    # -- sections and symbols ------------------------------------------------------

    def _section_statement(self, stmt: Statement, kind: str) -> None:
        section = Section(name=stmt.name, kind=kind, line=stmt.line,
                          origin=stmt.origin, depth=stmt.depth)
        if stmt.name:
            self._local.add(stmt.name.upper())
            # A repeated CSECT/DSECT/COM with the same name RESUMES that section rather
            # than defining a second one. Counting occurrences would multiply every
            # control section by however many times the source switched back to it.
            existing = self._sections.get(stmt.name.upper())
            if existing is not None:
                self._section = stmt.name.upper()
                return
            self._sections[stmt.name.upper()] = section
        self.module.sections.append(section)
        self._section = stmt.name.upper() if stmt.name else None
        if not self._named and section.is_visible and self._first_visible() is section:
            self.module.name = section.name.upper()

    def _first_visible(self) -> Optional[Section]:
        for s in self.module.sections:
            if s.is_visible:
                return s
        return None

    def _do_csect(self, stmt: Statement) -> None:
        self._section_statement(stmt, "csect")

    def _do_rsect(self, stmt: Statement) -> None:
        self._section_statement(stmt, "rsect")

    def _do_start(self, stmt: Statement) -> None:
        self._section_statement(stmt, "start")

    def _do_com(self, stmt: Statement) -> None:
        self._section_statement(stmt, "com")

    def _do_dsect(self, stmt: Statement) -> None:
        self._section_statement(stmt, "dsect")

    def _do_dxd(self, stmt: Statement) -> None:
        self._section_statement(stmt, "dxd")

    def _do_entry(self, stmt: Statement) -> None:
        for name in operands(stmt.operand):
            if SYMBOL.match(name):
                self.module.entries.append(Entry(name=name.upper(), line=stmt.line,
                                                 origin=stmt.origin, depth=stmt.depth))
                self._local.add(name.upper())
            else:
                self._flag("line {0}: ENTRY operand {1!r} is not a symbol this tool can "
                           "read, so it is not in the provides index".format(
                               stmt.line, name))

    def _external_declaration(self, stmt: Statement, via: str, weak: bool) -> None:
        for part in operands(stmt.operand):
            name = part
            # `EXTRN PART(name)` references a GOFF part rather than a section.
            m = re.match(r"^PART\((?P<n>[^)]+)\)$", part, re.I)
            if m:
                name, weak = m.group("n").strip(), True
            if not SYMBOL.match(name):
                self._flag("line {0}: {1} operand {2!r} is not a symbol this tool can "
                           "read".format(stmt.line, via, part))
                continue
            self._declared.add(name.upper())
            self.module.externals.append(ExternalRef(
                name=name.upper(), via=via, line=stmt.line, weak=weak,
                origin=stmt.origin, depth=stmt.depth,
                conditions=list(stmt.conditions)))

    def _do_extrn(self, stmt: Statement) -> None:
        self._external_declaration(stmt, VIA_EXTRN, weak=False)

    def _do_wxtrn(self, stmt: Statement) -> None:
        self._external_declaration(stmt, VIA_WXTRN, weak=True)

    def _do_alias(self, stmt: Statement) -> None:
        """``symbol ALIAS C'external.name'`` - the binder sees the alias, not the symbol."""
        if not stmt.name:
            self._flag("line {0}: an ALIAS with no name field renames nothing".format(
                stmt.line))
            return
        value = literal_value(stmt.operand.strip())
        self._aliases[stmt.name.upper()] = value

    def _do_amode(self, stmt: Statement) -> None:
        self._addressing(stmt, "amode")

    def _do_rmode(self, stmt: Statement) -> None:
        self._addressing(stmt, "rmode")

    def _addressing(self, stmt: Statement, attribute: str) -> None:
        target = stmt.name.upper() if stmt.name else (self._section or "")
        section = self._sections.get(target)
        if section is not None:
            setattr(section, attribute, stmt.operand.strip().upper())
            return
        for entry in self.module.entries:
            if entry.name == target and attribute == "amode":
                entry.amode = stmt.operand.strip().upper()
                return

    def _do_xattr(self, stmt: Statement) -> None:
        """``XATTR PSECT(name)`` and ``SCOPE(IMPORT|EXPORT)`` are real linkage edges."""
        for part in operands(stmt.operand):
            key, value = keyword(part)
            if key is None:
                m = re.match(r"^(?P<k>[A-Z]+)\((?P<v>[^)]*)\)$", part, re.I)
                if not m:
                    continue
                key, value = m.group("k").upper(), m.group("v")
            if key == "PSECT" and SYMBOL.match(value.strip()):
                self.module.externals.append(ExternalRef(
                    name=value.strip().upper(), via=VIA_RCON, line=stmt.line,
                    origin=stmt.origin, depth=stmt.depth,
                    conditions=list(stmt.conditions)))

    def _do_equ(self, stmt: Statement) -> None:
        if stmt.name:
            self._local.add(stmt.name.upper())

    def _do_cxd(self, stmt: Statement) -> None:
        if stmt.name:
            self._local.add(stmt.name.upper())

    def _do_end(self, stmt: Statement) -> None:
        target = stmt.operand.strip()
        if target and SYMBOL.match(target):
            self.module.end_label = target.upper()

    def _do_copy(self, stmt: Statement) -> None:
        """Recorded by the expander, which is what actually fetches it. Reaching here
        means the expander left it in place - an unresolved or computed member."""

    # -- constants -----------------------------------------------------------------

    def _do_dc(self, stmt: Statement) -> None:
        self._constant(stmt, initialised=True)

    def _do_ds(self, stmt: Statement) -> None:
        self._constant(stmt, initialised=False)

    def _constant(self, stmt: Statement, *, initialised: bool) -> None:
        parts = operands(stmt.operand)
        values: List[str] = []
        for part in parts:
            m = _ADCON.match(part)
            if m and initialised:
                self._adcon(stmt, m.group("type").upper(), m.group("body"),
                            literal_pool=False)
                continue
            c = _CHAR_DC.match(part)
            if c and initialised:
                values.append(_sized(c.group("body").replace("''", "'"),
                                     c.group("length")))
        if stmt.name:
            self._local.add(stmt.name.upper())
            self.module.data.append(DataDef(
                label=stmt.name.upper(), line=stmt.line, operand=stmt.operand,
                values=values, origin=stmt.origin, depth=stmt.depth,
                section=self._section))

    def _adcon(self, stmt: Statement, type_letter: str, body: str, *,
               literal_pool: bool) -> None:
        via = _EXTERNAL_ADCON.get(type_letter)
        for name in self._adcon_symbols(body):
            ref = ExternalRef(name=name, via=via or VIA_EXTRN, line=stmt.line,
                              origin=stmt.origin, depth=stmt.depth,
                              literal_pool=literal_pool,
                              conditions=list(stmt.conditions))
            if via is None:
                # A- and Y-type constants are external ONLY when the symbol is separately
                # declared. Deciding now would be wrong either way: the EXTRN can come
                # later in the member, and an A-con to a local label is not a dependency.
                self._acons.append(ref)
            else:
                self.module.externals.append(ref)

    def _adcon_symbols(self, body: str) -> List[str]:
        """The symbols an address-constant body names, if any.

        ``V(SORT,MERGE)`` names two. ``V(MOD1:MOD2)`` is the conditional-sequential form,
        where the binder takes the first that resolves - so every alternative is a
        candidate. ``A(*+8)`` and ``A(X-Y)`` name no single symbol and yield nothing.
        """
        out: List[str] = []
        for part in operands(body):
            for alternative in part.split(":"):
                token = alternative.strip()
                if SYMBOL.match(token):
                    out.append(token.upper())
        return out

    def _scan_literals(self, stmt: Statement) -> None:
        for m in _LITERAL_ADCON.finditer(stmt.operand):
            self._adcon(stmt, m.group("type").upper(), m.group("body"),
                        literal_pool=True)

    # -- machine instructions ------------------------------------------------------

    def _machine(self, stmt: Statement) -> None:
        if not opcodes.is_store(stmt.operation):
            return
        parts = operands(stmt.operand)
        if not parts:
            return
        m = _TARGET_LABEL.match(parts[0])
        if not m:
            return                          # a displacement form names no label
        literal = None
        if len(parts) > 1:
            c = _CHAR_LITERAL.search(parts[1])
            if c:
                literal = c.group("body").replace("''", "'")
        self.module.stores.append(Store(
            target=m.group("label").upper(), operation=stmt.operation.upper(),
            line=stmt.line, section=self._section, literal=literal))

    # -- second pass ---------------------------------------------------------------

    def _resolve_externals(self) -> None:
        """Decide what the A-cons meant, drop self-references, and apply ALIAS.

        Three things can only be settled once the whole member has been read: an ``EXTRN``
        may follow the ``A``-con that needs it, a ``V``-con may name a section this member
        itself defines, and an ``ALIAS`` may rename either.
        """
        for ref in self._acons:
            if ref.name in self._declared:
                ref.via = VIA_EXTRN
                self.module.externals.append(ref)

        visible = {s.name.upper() for s in self.module.sections if s.is_visible}
        kept: List[ExternalRef] = []
        for ref in self.module.externals:
            if ref.name in visible and ref.via in (VIA_VCON, VIA_CALL):
                # The binder resolves this inside the module. It is not a dependency on
                # anything outside, and reporting it would make every module that calls
                # its own entry point look like it depends on itself.
                self.module.notes.append(
                    "line {0}: {1} names {2}, which this member defines - resolved "
                    "internally, not an external dependency".format(
                        ref.line, ref.via, ref.name))
                continue
            ref.name = self._aliases.get(ref.name, ref.name)
            kept.append(ref)
        self.module.externals = kept

        for section in self.module.sections:
            section.alias = self._aliases.get(section.name.upper())
        for entry in self.module.entries:
            entry.alias = self._aliases.get(entry.name)

        if self._aliases:
            self.module.notes.append(
                "ALIAS renames {0} symbol(s) for the binder, so the name another module "
                "binds to is not the name this source refers to it by: {1}".format(
                    len(self._aliases),
                    ", ".join("{0} -> {1}".format(k, v)
                              for k, v in sorted(self._aliases.items()))))

    def _report_hidden_structure(self) -> None:
        """A module with no control section, and unresolved macros, has a hidden one.

        Found on real source: IBM's own IFOX assembler declares its control section as
        ``JCSECT (X0A00)`` and its entry points as ``JENTRY (X0A01=START)`` - site macros,
        defined in a member the module ``COPY``s. Parsed without that member, the module
        genuinely provides nothing this tool can see, and an empty ``provides`` list reads
        as "this module exposes no entry point" when the truth is "the names are behind a
        macro I could not retrieve".

        The difference matters more than most flags here: the provides index is one half of
        the estate's call graph, so a module silently contributing nothing to it is a hole
        in the graph rather than a gap in one manifest.
        """
        if any(s.is_visible for s in self.module.sections):
            return
        unresolved = sorted({m.name for m in self.module.members
                             if not m.resolved and m.kind == "macro"})
        if not unresolved:
            return
        self._flag(
            "this module declares no control section of its own, and {0} macro(s) could "
            "not be resolved ({1}) - a control section and its entry points are very often "
            "generated BY such a macro, so 'provides' is empty because the names are "
            "hidden rather than because the module exposes none{2}".format(
                len(unresolved), ", ".join(unresolved[:6]),
                ". The END statement names {0}, which is this module's entry "
                "point".format(self.module.end_label) if self.module.end_label else ""))

    def _report_unknown(self) -> None:
        """Every operation code that is neither a machine instruction nor a directive.

        In HLASM that means a macro, so each one is recorded as an unresolved macro member
        rather than skipped. A site macro that wraps ``LINK`` hides a real call, and a
        module whose calls are all behind such macros would otherwise read as having none.
        """
        known = {m.name for m in self.module.members}
        for op, line in sorted(self._unknown.items()):
            if op in known:
                continue        # the expander already recorded it, and already said so
            self.module.members.append(MemberUse(
                name=op, line=line, kind="macro", resolved=False))
            self._flag(
                "line {0}: operation {1} is not a machine instruction or an assembler "
                "directive, so it is a macro - and it was not resolved. Any CALL, LINK, "
                "XCTL, LOAD, DCB or EXEC it expands to is NOT in this manifest".format(
                    line, op))

    # -- program management --------------------------------------------------------
    #
    # LINK/XCTL/ATTACH/LOAD/DELETE all take the same three mutually exclusive name
    # operands, and the difference between them is the whole confidence question:
    #
    #   EP=PAYCALC      the name is a token in the statement          -> literal
    #   EPLOC=PGMNAME   the name is in an 8-byte field                -> dataflow decides
    #   EPLOC=(R1)      the name is in a register at run time         -> dynamic
    #   DE=BLDLNAME     the name is in a BLDL list built earlier      -> dataflow decides
    #
    # All four are real calls. Only the first can be read off the page.

    def _do_link(self, stmt: Statement) -> None:
        self._invocation(stmt, "LINK")

    def _do_linkx(self, stmt: Statement) -> None:
        self._invocation(stmt, "LINKX")

    def _do_xctl(self, stmt: Statement) -> None:
        self._invocation(stmt, "XCTL", transfers=True)

    def _do_xctlx(self, stmt: Statement) -> None:
        self._invocation(stmt, "XCTLX", transfers=True)

    def _do_attach(self, stmt: Statement) -> None:
        self._invocation(stmt, "ATTACH")

    def _do_attachx(self, stmt: Statement) -> None:
        self._invocation(stmt, "ATTACHX")

    def _do_load(self, stmt: Statement) -> None:
        self._invocation(stmt, "LOAD")

    def _do_delete(self, stmt: Statement) -> None:
        # DELETE must name what LOAD named, from the same task - so a DELETE with a
        # literal is a second, independent statement of a name a dynamic LOAD hid.
        self._invocation(stmt, "DELETE")

    def _do_ceefetch(self, stmt: Statement) -> None:
        self._invocation(stmt, "CEEFETCH")

    def _do_synch(self, stmt: Statement) -> None:
        self._synch(stmt, "SYNCH")

    def _do_synchx(self, stmt: Statement) -> None:
        self._synch(stmt, "SYNCHX")

    def _synch(self, stmt: Statement, verb: str) -> None:
        """SYNCH never names a module - it takes an entry-point ADDRESS.

        In practice it is the second half of a LOAD/SYNCH pair, so the name is on the
        LOAD. Recording nothing would lose the call; recording it as an independent
        unknown would double-count the one the LOAD already named. So it is recorded, and
        ``dataflow`` pairs it with a preceding LOAD in the same section.
        """
        self.module.invocations.append(Invocation(
            verb=verb, line=stmt.line, evidence=EVIDENCE_DYNAMIC,
            section=self._section,
            reason=("SYNCH takes an entry-point address, not a name - the module is "
                    "whatever put that address in place"),
            origin=stmt.origin, depth=stmt.depth, conditions=list(stmt.conditions)))

    def _invocation(self, stmt: Statement, verb: str, *, transfers: bool = False) -> None:
        call = Invocation(verb=verb, line=stmt.line, transfers=transfers,
                          section=self._section, origin=stmt.origin,
                          depth=stmt.depth, conditions=list(stmt.conditions))
        for part in operands(stmt.operand):
            key, value = keyword(part)
            if key == "EP":
                name = literal_value(value.strip())
                if SYMBOL.match(name):
                    call.target, call.evidence = name.upper(), EVIDENCE_LITERAL
                else:
                    call.reason = "EP={0} is not a member name".format(value)
            elif key in ("EPLOC", "DE"):
                if is_register_form(value):
                    call.reason = (
                        "{0}={1} takes the address from a register, so the eight-byte "
                        "name it points at is built at run time and is not in this "
                        "source".format(key, value))
                else:
                    m = _TARGET_LABEL.match(value.strip())
                    if m:
                        call.via_field = m.group("label").upper()
                        call.reason = "{0}={1} names a field rather than a module".format(
                            key, value.strip())
                    else:
                        call.reason = "{0}={1} is not a label this tool can follow".format(
                            key, value)
            elif key in ("DCB", "TASKLIB"):
                m = _TARGET_LABEL.match(value.strip())
                if m:
                    call.library = m.group("label").upper()
            elif key in ("MF", "SF"):
                self._list_form(call, stmt, value)
        self.module.invocations.append(call)

    def _list_form(self, call: Invocation, stmt: Statement, value: str) -> None:
        """``MF=L`` defines a parameter list; ``MF=(E,LINKLST)`` executes one.

        The two can be hundreds of lines and a whole control section apart. Counting a
        list form as a call double-counts every one of them, and reading only the execute
        form finds no name at all - so they are recorded as two halves and joined later.
        """
        text = value.strip()
        if text.upper() == "L":
            call.list_form = True
            call.list_label = stmt.name.upper() or None
            return
        m = re.match(r"^\(\s*E\s*,\s*(?P<list>[^)]+)\)$", text, re.I)
        if not m:
            return
        target = m.group("list").strip()
        if is_register_form(target):
            call.reason = (
                "the parameter list is at an address in {0}, so which list - and "
                "therefore which module - is decided at run time".format(target))
            return
        label = _TARGET_LABEL.match(target)
        if label:
            call.list_label = label.group("label").upper()

    def _do_call(self, stmt: Statement) -> None:
        """``CALL SYMBOL`` generates a V-con; ``CALL (15)`` creates no external reference.

        That is the cleanest static/dynamic split in the language: the symbol form is a
        bind-time dependency the binder resolves, and the register form is whatever a
        preceding LOAD put in the register.
        """
        parts = operands(stmt.operand)
        call = Invocation(verb="CALL", line=stmt.line, section=self._section,
                          origin=stmt.origin, depth=stmt.depth,
                          conditions=list(stmt.conditions))
        first = parts[0].strip() if parts else ""
        for part in parts[1:]:
            key, value = keyword(part)
            if key in ("MF", "SF"):
                self._list_form(call, stmt, value)
        if is_register_form(first):
            call.evidence = EVIDENCE_DYNAMIC
            call.reason = ("CALL {0} branches to an address already in a register, so the "
                           "target is whatever put it there - commonly a preceding "
                           "LOAD".format(first))
        elif SYMBOL.match(first):
            call.target, call.evidence = first.upper(), EVIDENCE_LITERAL
            self.module.externals.append(ExternalRef(
                name=first.upper(), via=VIA_CALL, line=stmt.line, origin=stmt.origin,
                depth=stmt.depth, conditions=list(stmt.conditions)))
        elif not first:
            call.evidence = EVIDENCE_DYNAMIC
            call.reason = "CALL with no entry name - a list or execute form"
        else:
            call.evidence = EVIDENCE_DYNAMIC
            call.reason = "CALL operand {0!r} is not a symbol or a register".format(first)
        self.module.invocations.append(call)

    # -- data management -----------------------------------------------------------

    def _do_dcb(self, stmt: Statement) -> None:
        self._control_block(stmt, "dcb")

    def _do_acb(self, stmt: Statement) -> None:
        self._control_block(stmt, "acb")

    def _do_dcbe(self, stmt: Statement) -> None:
        """A DCBE's EODAD/SYNAD OVERRIDE the DCB's, so a tool that reads only DCB operands
        attributes end-of-file handling to the wrong routine, or finds none at all."""
        self._exits_only(stmt)

    def _do_rpl(self, stmt: Statement) -> None:
        """An RPL points at an ACB and the ACB carries the ddname, so there is nothing to
        record here beyond keeping the operation off the unknown-operation list."""

    def _do_exlst(self, stmt: Statement) -> None:
        self._exits_only(stmt)

    def _exits_only(self, stmt: Statement) -> None:
        exits = self._exit_routines(stmt)
        if exits and self.module.files:
            self.module.files[-1].exits.update(exits)

    def _exit_routines(self, stmt: Statement) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for part in operands(stmt.operand):
            key, value = keyword(part)
            if key in ("EODAD", "SYNAD", "LERAD", "JRNAD", "UPAD"):
                out[key] = value.strip()
        return out

    def _control_block(self, stmt: Statement, kind: str) -> None:
        ref = FileRef(ddname=None, line=stmt.line, kind=kind,
                      label=stmt.name.upper() or None, origin=stmt.origin,
                      depth=stmt.depth, conditions=list(stmt.conditions))
        access_method = None
        for part in operands(stmt.operand):
            key, value = keyword(part)
            if key == "DDNAME":
                name = literal_value(value.strip())
                if SYMBOL.match(name):
                    ref.ddname, ref.evidence = name.upper(), EVIDENCE_LITERAL
            elif key == "DSORG":
                ref.dsorg = value.strip().upper()
            elif key == "MACRF":
                ref.macrf = value.strip().upper()
                ref.io = _io_from_macrf(ref.macrf)
            elif key == "AM":
                access_method = value.strip().upper()
            elif key == "APPLID":
                access_method = access_method or "VTAM"
        if kind == "acb" and access_method == "VTAM":
            # A VTAM ACB names an APPLID, not a ddname. Two entirely different macros
            # share one name, and reading a VTAM ACB as a VSAM one invents a file.
            self.module.notes.append(
                "line {0}: this ACB is a VTAM application control block (AM=VTAM), which "
                "names an APPLID rather than a ddname - it is not a file".format(
                    stmt.line))
            return
        if ref.ddname is None and kind == "acb" and ref.label:
            # A VSAM ACB with no DDNAME= takes its ddname from the statement's NAME FIELD.
            # Scanning only for DDNAME= misses it entirely.
            ref.ddname, ref.evidence = ref.label, EVIDENCE_LITERAL
            ref.reason = ("the ACB declares no DDNAME=, so the ddname is the label on the "
                          "ACB statement")
        elif ref.ddname is None:
            ref.evidence = EVIDENCE_DYNAMIC
            ref.ddname = ref.label
            ref.reason = ("the {0} declares no DDNAME=, so it is moved in before OPEN and "
                          "the ddname is not in this statement".format(kind.upper()))
        ref.exits.update(self._exit_routines(stmt))
        self.module.files.append(ref)

    def _do_open(self, stmt: Statement) -> None:
        self._open_close(stmt, opening=True)

    def _do_close(self, stmt: Statement) -> None:
        self._open_close(stmt, opening=False)

    def _open_close(self, stmt: Statement, *, opening: bool) -> None:
        """OPEN says which control blocks are actually used, and in which direction.

        A DCB can be declared and never opened, and a QSAM DCB with no MACRF does not say
        whether the file is read or written - so this is where direction comes from.
        """
        if not opening:
            return
        text = stmt.operand.strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        parts = operands(text)
        index = 0
        while index < len(parts):
            target = parts[index].strip()
            direction = None
            if index + 1 < len(parts):
                nxt = parts[index + 1].strip().strip("()").upper()
                if nxt in _DIRECTIONS:
                    direction = _DIRECTIONS[nxt]
                    index += 1
            index += 1
            if is_register_form(target):
                self._flag(
                    "line {0}: OPEN {1} opens a control block at an address in a "
                    "register, so which file it opens is not in this source".format(
                        stmt.line, target))
                continue
            m = _TARGET_LABEL.match(target)
            if not m:
                continue
            label = m.group("label").upper()
            for ref in self.module.files:
                if ref.label == label:
                    ref.opened = True
                    if direction:
                        ref.io = direction

    # -- EXEC CICS and EXEC SQL ----------------------------------------------------
    #
    # Neither is assembler. Both are a preprocessor's language sitting in the operand
    # field, and the lexer has already handled the two rules that differ (blank-separated
    # operands, no remarks field, and CICS's own continue column).
    #
    # The literal/data-area split is the whole confidence question again, and CICS states
    # it in its own syntax: PROGRAM('PAYCALC') is a literal, PROGRAM(PGMNAME) is a data
    # area whose contents are decided at run time.

    def _do_exec(self, stmt: Statement) -> None:
        operand = stmt.operand.strip()
        head = operand.split(None, 1)
        dialect = head[0].upper() if head else ""
        rest = head[1] if len(head) > 1 else ""
        if dialect == "CICS":
            self._exec_cics(stmt, rest)
        elif dialect == "SQL":
            self._exec_sql(stmt, rest)
        elif dialect == "DLI":
            self._exec_dli(stmt, rest)
        else:
            self._flag("line {0}: EXEC {1} is not a preprocessor this tool reads, so "
                       "anything it names is not in this manifest".format(
                           stmt.line, dialect or "(nothing)"))

    def _cics_options(self, text: str) -> Dict[str, str]:
        """``PROGRAM('X') COMMAREA(Y) LENGTH(=H'250')`` -> ``{PROGRAM: "'X'", ...}``.

        Keyword-only options (``MAPONLY``, ``ERASE``, ``NOHANDLE``) map to ``""``, so a
        caller can test for their presence without them colliding with a value.
        """
        out: Dict[str, str] = {}
        i, n = 0, len(text)
        while i < n:
            m = _CICS_OPTION.match(text, i)
            if not m:
                i += 1
                continue
            name = m.group("name").upper()
            if m.group("value") is not None:
                out[name] = m.group("value").strip()
                i = m.end()
                continue
            out.setdefault(name, "")
            i = m.end()
        return out

    def _cics_name(self, value: str) -> Tuple[Optional[str], str, Optional[str]]:
        """``(name, evidence, reason)`` for one CICS argument.

        IBM states the rule in the command reference: *use quotes for literal names, omit
        quotes for data area variables*. So the quoting IS the evidence.
        """
        text = (value or "").strip()
        if not text:
            return None, EVIDENCE_DYNAMIC, "the option was given no argument"
        if is_literal(text):
            name = literal_value(text).strip().upper()
            return (name, EVIDENCE_LITERAL, None) if name else (
                None, EVIDENCE_DYNAMIC, "the literal is blank")
        m = _TARGET_LABEL.match(text)
        if m:
            return None, EVIDENCE_DYNAMIC, (
                "{0} is a data area, not a literal - what it holds at the command is "
                "decided at run time".format(text))
        return None, EVIDENCE_DYNAMIC, (
            "{0} is neither a literal nor a label this tool can follow".format(text))

    def _cics_io(self, kind: str, command: str,
                 fixed: Optional[str]) -> Optional[str]:
        """Which way the data moves, for the kinds where that is a fact about the artifact.

        A mapset and a transaction get none, the same way `program`, `proc` and `macro`
        carry no `io` anywhere else in the family: SEND and RECEIVE describe the terminal
        conversation, not a direction of access to the load module. Giving them one would
        make a consumer's `io` rule mean two different things.
        """
        if kind in _NO_IO_KINDS:
            return None
        return fixed if fixed is not None else _CICS_IO.get(command)

    def _resource(self, stmt: Statement, name: Optional[str], kind: str, *,
                  subsystem: str, verb: str, io: Optional[str] = None,
                  evidence: str = EVIDENCE_LITERAL,
                  reason: Optional[str] = None) -> None:
        self.module.resources.append(ResourceRef(
            name=name or "", kind=kind, line=stmt.line, subsystem=subsystem, verb=verb,
            io=io, evidence=evidence, reason=reason, origin=stmt.origin,
            depth=stmt.depth, conditions=list(stmt.conditions)))

    def _exec_cics(self, stmt: Statement, text: str) -> None:
        parts = text.split(None, 1)
        command = parts[0].upper() if parts else ""
        # READQ TS / WRITEQ TD / STARTBR - the command is two words for the queue verbs.
        options = self._cics_options(parts[1] if len(parts) > 1 else "")
        second = ""
        if len(parts) > 1:
            following = parts[1].split(None, 1)
            second = following[0].upper() if following else ""
        if command in _CICS_QUEUE_VERBS and second in ("TS", "TD"):
            command = "{0} {1}".format(command, second)

        handled = False
        if command in _CICS_PROGRAM_VERBS and "PROGRAM" in options:
            handled = True
            name, evidence, reason = self._cics_name(options["PROGRAM"])
            self.module.invocations.append(Invocation(
                verb="EXEC CICS {0}".format(command), line=stmt.line, target=name,
                evidence=evidence, reason=reason,
                transfers=command == "XCTL", section=self._section,
                origin=stmt.origin, depth=stmt.depth,
                conditions=list(stmt.conditions)))
            if options.get("SYSID"):
                self.module.notes.append(
                    "line {0}: EXEC CICS {1} carries SYSID({2}), so the target runs in "
                    "ANOTHER CICS region - the name is not resolvable in this one".format(
                        stmt.line, command, options["SYSID"]))

        for option, (kind, io) in _CICS_RESOURCE_OPTIONS.items():
            if option not in options:
                continue
            if option == "PROGRAM" and handled:
                continue
            handled = True
            name, evidence, reason = self._cics_name(options[option])
            self._resource(stmt, name, kind, subsystem="CICS",
                           verb="EXEC CICS {0}".format(command),
                           io=self._cics_io(kind, command, io),
                           evidence=evidence, reason=reason)

        if (command in ("SEND", "RECEIVE") and "MAP" in options
                and "MAPSET" not in options):
            handled = True
            # IBM: "If this option is not specified, the name given in the MAP option is
            # assumed to be that of the mapset." The mapset is the load module, so a tool
            # that only reads MAPSET() finds no dependency for the commonest form.
            name, evidence, reason = self._cics_name(options["MAP"])
            self._resource(stmt, name, "terminal-map", subsystem="CICS",
                           verb="EXEC CICS {0}".format(command),
                           io=self._cics_io("terminal-map", command, None),
                           evidence=evidence,
                           reason=reason or ("no MAPSET was given, so the mapset is the "
                                             "name in MAP() - which is the load module"))

        if command == "ASSIGN":
            # Every ASSIGN option is a RECEIVER. It never names a dependency; it DISCOVERS
            # one at run time, and a PROGRAM()/FILE() built from what it returns is the
            # canonical unresolvable CICS dispatch.
            self.module.notes.append(
                "line {0}: EXEC CICS ASSIGN reads values INTO data areas - it names no "
                "dependency. A later PROGRAM(), FILE() or QUEUE() built from one of them "
                "is decided at run time.".format(stmt.line))
            handled = True

        if command in ("ENQ", "DEQ"):
            handled = True
            self._flag("line {0}: EXEC CICS {1} identifies its resource by the ADDRESS or "
                       "CONTENTS of a data area, not by a name - the serialization "
                       "contract it takes part in is not in this source".format(
                           stmt.line, command))

        if not handled and command:
            self._unmodelled_cics.setdefault(command, stmt.line)

    def _exec_sql(self, stmt: Statement, text: str) -> None:
        upper = " ".join(text.upper().split())
        if not upper:
            return
        if upper.startswith("INCLUDE "):
            member = upper.split(None, 1)[1].strip()
            if member in ("SQLCA", "SQLDA"):
                return                      # an IBM-supplied layout, not estate source
            if SYMBOL.match(member):
                self.module.members.append(MemberUse(
                    name=member, line=stmt.line, kind="copybook", resolved=False,
                    origin=stmt.origin, depth=stmt.depth,
                    conditions=list(stmt.conditions)))
            return

        if any(upper.startswith(v) or " {0} ".format(v) in upper
               for v in _SQL_DYNAMIC_VERBS):
            self._flag(
                "line {0}: this module builds SQL at run time ({1}), so the statement "
                "text - and therefore the tables it touches - is in a host variable and "
                "not in this source. The table list below is INCOMPLETE.".format(
                    stmt.line, upper.split()[0]))

        for pattern, io in _SQL_TABLE_PATTERNS:
            for m in pattern.finditer(upper):
                name = m.group("table").strip().rstrip(",;")
                if not _SQL_NAME.match(name) or name in _SQL_NOT_TABLES:
                    continue
                self._resource(stmt, name, "db2-table", subsystem="Db2",
                               verb="EXEC SQL", io=io)

    def _exec_dli(self, stmt: Statement, text: str) -> None:
        """``EXEC DLI SCHD PSB(name)`` - the one place a PSB name IS in the source."""
        options = self._cics_options(text)
        parts = text.split(None, 1)
        command = parts[0].upper() if parts else ""
        if "PSB" in options:
            name, evidence, reason = self._cics_name(options["PSB"])
            self._resource(stmt, name, "psb", subsystem="IMS",
                           verb="EXEC DLI {0}".format(command),
                           evidence=evidence, reason=reason)
        if "SEGMENT" in options:
            name, evidence, reason = self._cics_name(options["SEGMENT"])
            self._resource(stmt, name, "segment", subsystem="IMS",
                           verb="EXEC DLI {0}".format(command),
                           evidence=evidence, reason=reason)

    def _report_cics(self) -> None:
        for command, line in sorted(self._unmodelled_cics.items()):
            self._flag(
                "line {0}: EXEC CICS {1} is not modelled - if it names a program, map, "
                "file, queue or transaction, that dependency is NOT in this "
                "manifest".format(line, command))



#: OPEN's direction operands, mapped onto the same io vocabulary the Easytrieve `file`
#: row uses, so one consumer rule reads a `file` row from either package.
_DIRECTIONS = {
    "INPUT": "read", "RDBACK": "read",
    "OUTPUT": "write", "EXTEND": "write", "OUTIN": "write", "OUTINX": "write",
    "UPDAT": "read-write", "INOUT": "read-write",
}


def _io_from_macrf(macrf: str) -> str:
    """QSAM/BSAM ``(GM)``/``(PL)`` and VSAM ``(KEY,SEQ,IN)`` reduced to one vocabulary."""
    text = macrf.upper()
    if "UPDAT" in text:
        return "read-write"
    read = bool(re.search(r"\bIN\b", text)) or bool(re.search(r"[(,]\s*G", text))
    write = bool(re.search(r"\bOUT\b", text)) or bool(re.search(r"[(,]\s*P", text))
    if read and write:
        return "read-write"
    if read:
        return "read"
    if write:
        return "write"
    return "unknown"
#: One CICS option: ``NAME(value)`` or a bare keyword such as ``MAPONLY``.
_CICS_OPTION = re.compile(
    r"(?P<name>[A-Z][A-Z0-9]*)\s*(?:\(\s*(?P<value>[^()]*(?:\([^()]*\)[^()]*)*?)\s*\))?",
    re.I)

#: Commands whose ``PROGRAM()`` is a module this one reaches.
_CICS_PROGRAM_VERBS = frozenset({"LINK", "XCTL", "LOAD", "RELEASE"})

#: Queue commands whose second word (``TS``/``TD``) is part of the command.
_CICS_QUEUE_VERBS = frozenset({"READQ", "WRITEQ", "DELETEQ"})

#: option -> (artifact kind, fixed io or None to take it from the command)
_CICS_RESOURCE_OPTIONS = {
    "MAPSET": ("terminal-map", None),
    "TRANSID": ("cics-transaction", None),
    "FILE": ("file", None),
    "DATASET": ("file", None),
    "QUEUE": ("queue", None),
    "QNAME": ("queue", None),
}

#: Kinds that carry no `io`, matching `program`/`proc`/`macro` elsewhere in the family:
#: a mapset is a load module and a transaction is a CSD entry, and neither is read or
#: written.
_NO_IO_KINDS = frozenset({"terminal-map", "cics-transaction"})

#: Which way the data moves, per command, in the family's `file` vocabulary.
_CICS_IO = {
    "READ": "read", "READNEXT": "read", "READPREV": "read", "STARTBR": "read",
    "READQ": "read", "READQ TS": "read", "READQ TD": "read", "RECEIVE": "read",
    "WRITE": "write", "REWRITE": "write", "DELETE": "write", "PUT": "write",
    "WRITEQ": "write", "WRITEQ TS": "write", "WRITEQ TD": "write", "SEND": "write",
    "DELETEQ": "write", "DELETEQ TS": "write", "DELETEQ TD": "write",
}

#: Where a static SQL statement names a table, and which way the data moves.
_SQL_TABLE_PATTERNS = [
    # The lookbehind matters: `DELETE FROM T` matches the bare FROM pattern too, so
    # without it every deleted-from table is reported as read AND written. The text is
    # whitespace-normalised before matching, so the lookbehind is fixed width.
    (re.compile(r"(?<!DELETE )\bFROM\s+(?P<table>[A-Z0-9_$#@.]+)"), "read"),
    (re.compile(r"\bJOIN\s+(?P<table>[A-Z0-9_$#@.]+)"), "read"),
    (re.compile(r"\bINSERT\s+INTO\s+(?P<table>[A-Z0-9_$#@.]+)"), "write"),
    (re.compile(r"\bUPDATE\s+(?P<table>[A-Z0-9_$#@.]+)"), "write"),
    (re.compile(r"\bDELETE\s+FROM\s+(?P<table>[A-Z0-9_$#@.]+)"), "write"),
    (re.compile(r"\bDECLARE\s+(?P<table>[A-Z0-9_$#@.]+)\s+TABLE\b"), "read"),
]

#: SQL that builds its statement text at run time. The tables then live in a host
#: variable and are not in the source at all.
_SQL_DYNAMIC_VERBS = ("PREPARE", "EXECUTE IMMEDIATE", "DESCRIBE", "ALLOCATE CURSOR")

_SQL_NAME = re.compile(r"^[A-Z@#$][A-Z0-9_$#@]*(\.[A-Z@#$][A-Z0-9_$#@]*)?$")

#: Words that follow FROM/UPDATE without naming a table.
_SQL_NOT_TABLES = frozenset({"SELECT", "TABLE", "WHERE", "SET", "VALUES", "CURRENT"})
