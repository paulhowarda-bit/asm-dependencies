"""``COPY`` members and macros: the text a module is assembled from but does not contain.

Following these is not optional, and not for completeness. A site macro that wraps
``LINK``/``XCTL``/``LOAD`` - ``@LINK PGM=PAYCALC`` - is the normal way large shops write
assembler, and a module parsed without its macro libraries has *no calls at all* while
still producing a manifest that looks finished. The same goes for a ``COPY`` member
carrying the ``DCB`` for every file the module opens.

**The single invariant this file must preserve.** :meth:`MacroExpander._resolve` is the one
call that reaches outside, it is called on **every** encounter, and it never memoizes or
short-circuits. ``prefetch.py`` closes the member set by *replaying the parse* under a
recording resolver until it stops asking for members it has not got - which works only
while every external member goes through that one un-memoized call. Caching resolution
here, or skipping the call when a member was already seen, silently shortens the closure:
no error, just a module that reads as though it had fewer dependencies than it has.

A lexical scan for ``COPY`` would have been easy and would have been wrong. A macro
invokes macros, and the inner invocation only becomes visible once the outer macro's text
is in hand - so the questions have to be asked in the order the assembler asks them, which
is what replaying the parse does.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set

from .lexer import (
    DEFAULT_MARGINS, STATEMENT, SYMBOL, VARIABLE_SYMBOL, Statement, logical_lines,
    operands,
)
from .model import MemberUse
from . import classify, conditional, opcodes

logger = logging.getLogger(__name__)

Resolver = Callable[[str], Optional[str]]

#: How deep COPY and macro nesting may go before the expander stops and says so. IBM sets
#: no limit on COPY nesting, so a bound is the only defence against a resolver that keeps
#: answering - and a member set this deep is far more likely a loop than a real module.
MAX_DEPTH = 12

#: Statements that DECLARE a conditional-assembly variable.
_DECLARES = frozenset({"LCLA", "LCLB", "LCLC", "GBLA", "GBLB", "GBLC"})

#: A variable symbol and the period that may terminate it. The period is a concatenation
#: mark and is consumed by substitution: ``&PFX.NAME`` becomes the value of ``&PFX``
#: followed immediately by ``NAME``.
_VARIABLE = re.compile(r"&(?P<name>[A-Z@#$_][A-Z0-9@#$_]{0,61})(?P<dot>\.?)", re.I)


@dataclass
class MacroDef:
    """One ``MACRO``/``MEND`` definition and the prototype that names its parameters."""

    name: str
    positional: List[str] = field(default_factory=list)
    keywords: Dict[str, str] = field(default_factory=dict)
    name_parameter: Optional[str] = None
    #: Variables the BODY declares (LCLA/GBLC/...) or assigns (SETA/SETB/SETC).
    #: They are conditional-assembly locals, not parameters the caller forgot, and
    #: flagging them as unsupplied buries the real unsupplied-parameter flag under one
    #: per local per invocation - 45 of them in a single IFOX module.
    variables: Set[str] = field(default_factory=set)
    body: List[Statement] = field(default_factory=list)
    source: Optional[str] = None            # the library member it came from


class MacroExpander:
    """Expands ``COPY`` and macro calls in a statement stream."""

    def __init__(self, resolver: Optional[Resolver] = None, *,
                 max_depth: int = MAX_DEPTH,
                 env: Optional[conditional.Environment] = None,
                 resolve_macros: bool = True) -> None:
        self.resolver = resolver
        self.max_depth = max_depth
        #: Whether an unknown operation may be fetched as a LIBRARY macro. The
        #: prefetch loop turns this off for the rounds that are still closing COPY
        #: members, because a COPY member routinely carries the shop's linkage macros
        #: - so asking for one before the COPYs are in hand reports a member as
        #: missing that the very next round defines inline.
        self.resolve_macros = resolve_macros
        #: Conditional-assembly variables. Shared across the whole expansion, because
        #: GBLA/GBLB/GBLC symbols and &SYSPARM span open code and every macro it calls.
        self.env = env or conditional.Environment()
        self.uses: List[MemberUse] = []
        self.flags: List[str] = []
        self._definitions: Dict[str, MacroDef] = {}
        self._sysndx = 0

    # -- the one route outside -----------------------------------------------------

    def _resolve(self, name: str) -> Optional[str]:
        """Ask the caller's resolver for a member. THE funnel - see the module docstring.

        Deliberately not memoized. A repeated ask is what lets ``prefetch`` see the whole
        member set on every replay; answering from a cache would make the second round
        silent and the closure short.
        """
        if self.resolver is None:
            return None
        try:
            return self.resolver(name)
        except Exception as exc:
            self._flag("member {0} could not be retrieved ({1!r}) - a failed request is "
                       "not the same fact as a member that does not exist, and anything "
                       "it defines is missing from this model".format(name, exc))
            logger.debug("resolver raised for %s", name, exc_info=True)
            return None

    def _flag(self, text: str) -> None:
        if text not in self.flags:
            self.flags.append(text)

    # -- the walk ------------------------------------------------------------------

    def expand(self, stmts: Sequence[Statement]) -> List[Statement]:
        return self._walk(list(stmts), depth=0, stack=())

    def _walk(self, stmts: List[Statement], *, depth: int,
              stack: Sequence[str]) -> List[Statement]:
        # Definitions come out FIRST, and only then is conditional assembly evaluated.
        # The order is load-bearing: an AIF inside a macro body is decided when the macro
        # is INVOKED, with its parameters bound. Evaluating it here, at definition time,
        # would decide it against an empty environment and drop the branch that the call
        # actually selects.
        stmts = self._definitions_out(stmts)
        stmts = conditional.evaluate(stmts, env=self.env, flags=self.flags)

        out: List[Statement] = []
        i, n = 0, len(stmts)
        while i < n:
            stmt = stmts[i]
            if stmt.kind != STATEMENT:
                out.append(stmt)
                i += 1
                continue
            op = stmt.operation.upper()

            if op == "COPY":
                out.extend(self._copy(stmt, depth=depth, stack=stack))
                i += 1
                continue
            if op in self._definitions:
                out.extend(self._invoke(self._definitions[op], stmt, depth=depth,
                                        stack=stack))
                i += 1
                continue
            if op and not opcodes.is_known(op):
                out.extend(self._library_macro(stmt, depth=depth, stack=stack))
                i += 1
                continue
            out.append(stmt)
            i += 1
        return out

    # -- MACRO / MEND --------------------------------------------------------------

    def _definitions_out(self, stmts: List[Statement]) -> List[Statement]:
        """Register every ``MACRO``/``MEND`` block and return the stream without them."""
        out: List[Statement] = []
        i, n = 0, len(stmts)
        while i < n:
            stmt = stmts[i]
            if stmt.kind == STATEMENT and stmt.operation.upper() == "MACRO":
                i = self._capture(stmts, i)
                continue
            out.append(stmt)
            i += 1
        return out

    def _capture(self, stmts: List[Statement], start: int) -> int:
        """Read a ``MACRO``/``MEND`` definition out of the stream and register it."""
        i = start + 1
        n = len(stmts)
        prototype: Optional[Statement] = None
        body: List[Statement] = []
        nesting = 0
        while i < n:
            stmt = stmts[i]
            op = stmt.operation.upper() if stmt.kind == STATEMENT else ""
            if op == "MACRO":
                nesting += 1
            elif op == "MEND":
                if nesting == 0:
                    break
                nesting -= 1
            if prototype is None and stmt.kind == STATEMENT and stmt.operation:
                prototype = stmt
            else:
                body.append(stmt)
            i += 1
        if prototype is None or i >= n:
            self._flag("line {0}: a MACRO definition was not closed by MEND, so it was "
                       "not registered - every call to it is unexpanded".format(
                           stmts[start].line))
            return i + 1
        end_label = stmts[i].sequence_symbol if i < n else None
        if end_label:
            # `.EXIT MEND` puts a sequence symbol on the MEND. Dropping it loses the
            # target of every `AIF (...).EXIT` in the body, which then reports as a branch
            # to a label that does not exist - IFOX's JPHASE macro does exactly this.
            body.append(Statement(operation="ANOP", sequence_symbol=end_label,
                                  line=stmts[i].line, origin=stmts[i].origin,
                                  depth=stmts[i].depth))
        definition = self._prototype(prototype, body)
        self._definitions[definition.name] = definition
        return i + 1

    def _prototype(self, prototype: Statement, body: List[Statement]) -> MacroDef:
        definition = MacroDef(name=prototype.operation.upper(), body=body)
        for stmt in body:
            if stmt.kind != STATEMENT:
                continue
            op = stmt.operation.upper()
            if op in _DECLARES:
                definition.variables.update(
                    v.strip().upper() for v in operands(stmt.operand) if v.strip())
            elif op in ("SETA", "SETB", "SETC") and stmt.name.startswith("&"):
                definition.variables.add(stmt.name.upper())
        if prototype.name.startswith("&"):
            definition.name_parameter = prototype.name.upper()
        for part in operands(prototype.operand):
            if "=" in part:
                key, _, default = part.partition("=")
                # Stored WITHOUT the ampersand, so it is keyed the same way the call site
                # spells it (`PGM=X`). Keeping the prototype's `&` here made every lookup
                # `&&PGM`, so no default was ever found and every supplied keyword was
                # reported as one the prototype does not declare.
                definition.keywords[key.strip().upper().lstrip("&")] = default.strip()
            elif part.strip():
                definition.positional.append(part.strip().upper())
        return definition

    # -- COPY ----------------------------------------------------------------------

    def _copy(self, stmt: Statement, *, depth: int,
              stack: Sequence[str]) -> List[Statement]:
        member = stmt.operand.strip()
        if VARIABLE_SYMBOL.search(member):
            # `COPY &MEM` is legal in open code: the member name is built by conditional
            # assembly, typically from &SYSPARM. Guessing which member would be inventing
            # a dependency, so it is recorded as computed and left unresolved.
            self.uses.append(MemberUse(name=member.upper(), line=stmt.line,
                                       kind="copybook", resolved=False, computed=True,
                                       origin=stmt.origin, depth=stmt.depth,
                                       conditions=list(stmt.conditions)))
            self._flag("line {0}: COPY {1} builds its member name by substitution, so "
                       "which member is copied is decided at assembly time and is not in "
                       "this source - whatever it defines is missing from this "
                       "model".format(stmt.line, member))
            return [stmt]
        if not SYMBOL.match(member):
            self._flag("line {0}: COPY operand {1!r} is not a member name".format(
                stmt.line, member))
            return [stmt]
        return self._splice(stmt, member.upper(), "copybook", depth=depth, stack=stack)

    def _splice(self, stmt: Statement, member: str, kind: str, *, depth: int,
                stack: Sequence[str]) -> List[Statement]:
        use = MemberUse(name=member, line=stmt.line, kind=kind, resolved=False,
                        origin=stmt.origin, depth=stmt.depth,
                        conditions=list(stmt.conditions))
        self.uses.append(use)

        if member in stack:
            self._flag("line {0}: {1} {2} is already being expanded - the assembler "
                       "forbids recursive COPY, so the expansion was stopped".format(
                           stmt.line, kind, member))
            return [stmt]
        if depth >= self.max_depth:
            self._flag("line {0}: {1} {2} is nested deeper than {3} levels, so it and "
                       "anything below it were not expanded".format(
                           stmt.line, kind, member, self.max_depth))
            return [stmt]

        text = self._resolve(member)
        if text is None:
            self._flag("line {0}: {1} {2} was not resolved - anything it defines (record "
                       "layouts, DCBs, calls) is missing from this model".format(
                           stmt.line, kind, member))
            return [stmt]

        use.resolved = True
        # Copied text always uses the DEFAULT margins: an ICTL in the containing member
        # does not propagate into what it copies, so one assembly can carry two column
        # geometries and lexing the member with the container's would mis-read every line.
        inner, flags = logical_lines(text, margins=DEFAULT_MARGINS)
        for f in flags:
            self._flag("in {0} {1}: {2}".format(kind, member, f))
        for s in inner:
            s.origin = member
            s.depth = stmt.depth + 1
            s.conditions = list(stmt.conditions) + list(s.conditions)
        return self._walk(inner, depth=depth + 1, stack=tuple(stack) + (member,))

    # -- macro invocation ----------------------------------------------------------

    def _library_macro(self, stmt: Statement, *, depth: int,
                       stack: Sequence[str]) -> List[Statement]:
        """An operation that is not a machine instruction or a directive: a macro.

        Look for it in the libraries. If it is found and carries a ``MACRO``/``MEND``
        definition, register and invoke it; otherwise record it as an unresolved macro,
        because whatever it expands to is a hole in this manifest.
        """
        name = stmt.operation.upper()
        if not self.resolve_macros:
            # Not yet: a COPY member still being closed may define this macro inline.
            return [stmt]
        if depth >= self.max_depth or name in stack:
            self._flag("line {0}: macro {1} is nested deeper than {2} levels or is "
                       "recursive, so it was not expanded".format(
                           stmt.line, name, self.max_depth))
            return [stmt]

        text = self._resolve(name)
        if text is None:
            self.uses.append(MemberUse(name=name, line=stmt.line, kind="macro",
                                       resolved=False, origin=stmt.origin,
                                       depth=stmt.depth,
                                       conditions=list(stmt.conditions)))
            owner = classify.subsystem(name)
            if owner is None:
                self._flag(
                    "line {0}: {1} is not a machine instruction or an assembler "
                    "directive, so it is a macro - and it was not resolved. Any CALL, "
                    "LINK, XCTL, LOAD, DCB or EXEC it expands to is NOT in this "
                    "manifest".format(stmt.line, name))
            else:
                # A named IBM macro is a different kind of gap from a site macro: it
                # generates runtime housekeeping, not the shop's calls. Saying "any CALL
                # it expands to is missing" of DFHEIENT would send a reader looking for a
                # dependency that was never there.
                self._flag(
                    "line {0}: {1} is a {2} macro and was not retrieved - it generates "
                    "{2} runtime code rather than this shop's calls, so the gap is "
                    "narrower than an unresolved site macro, but it is still a "
                    "gap".format(stmt.line, name, owner))
            return [stmt]

        inner, flags = logical_lines(text, margins=DEFAULT_MARGINS)
        for f in flags:
            self._flag("in macro {0}: {1}".format(name, f))
        captured: List[Statement] = []
        walked = self._walk(inner, depth=depth + 1, stack=tuple(stack) + (name,))
        captured.extend(walked)

        self.uses.append(MemberUse(name=name, line=stmt.line, kind="macro", resolved=True,
                                   origin=stmt.origin, depth=stmt.depth,
                                   conditions=list(stmt.conditions)))
        definition = self._definitions.get(name)
        if definition is None:
            self._flag("line {0}: macro member {1} was retrieved but contains no "
                       "MACRO/MEND definition of {1}, so the call was not expanded".format(
                           stmt.line, name))
            return [stmt] + [s for s in captured if s.kind == STATEMENT]
        return self._invoke(definition, stmt, depth=depth, stack=stack)

    def _invoke(self, definition: MacroDef, stmt: Statement, *, depth: int,
                stack: Sequence[str]) -> List[Statement]:
        if depth >= self.max_depth or definition.name in stack:
            self._flag("line {0}: macro {1} is nested deeper than {2} levels or is "
                       "recursive, so it was not expanded".format(
                           stmt.line, definition.name, self.max_depth))
            return [stmt]

        self._sysndx += 1
        params = self._arguments(definition, stmt)
        out: List[Statement] = []
        for body_stmt in definition.body:
            if body_stmt.kind != STATEMENT:
                continue
            produced = Statement(
                name=self._substitute(body_stmt.name, params, stmt,
                     definition.variables),
                operation=self._substitute(body_stmt.operation, params, stmt,
                     definition.variables),
                operand=self._substitute(body_stmt.operand, params, stmt,
                     definition.variables),
                remarks=body_stmt.remarks, kind=STATEMENT, line=stmt.line,
                raw=body_stmt.raw, origin=definition.name, depth=stmt.depth + 1,
                sequence_symbol=body_stmt.sequence_symbol,
                conditions=list(stmt.conditions) + list(body_stmt.conditions))
            out.append(produced)
        return self._walk(out, depth=depth + 1,
                          stack=tuple(stack) + (definition.name,))

    def _arguments(self, definition: MacroDef, stmt: Statement) -> Dict[str, str]:
        params: Dict[str, str] = {"&SYSNDX": "{0:04d}".format(self._sysndx)}
        for key, default in definition.keywords.items():
            params["&" + key] = default
        if definition.name_parameter:
            params[definition.name_parameter] = stmt.name

        positional = list(definition.positional)
        supplied: List[str] = []
        for part in operands(stmt.operand):
            key, value = _split_keyword(part)
            if key is not None and "&" + key in params:
                params["&" + key] = value
            elif key is not None and ("&" + key) not in params:
                params["&" + key] = value
                self._flag("line {0}: macro {1} was passed keyword {2}=, which its "
                           "prototype does not declare".format(
                               stmt.line, definition.name, key))
            else:
                supplied.append(part)

        for index, name in enumerate(positional):
            if index < len(supplied):
                params[name] = supplied[index]
            # An unsupplied parameter is deliberately left OUT of the map, so _substitute
            # leaves `&NAME` visible and flags it rather than blanking it. A DCB generated
            # with DDNAME='' is a file binding that is wrong rather than absent, and the
            # wrong one is the one nobody notices.
        if len(supplied) > len(positional):
            params["&SYSLIST"] = ",".join(supplied)
        return params

    def _substitute(self, text: str, params: Dict[str, str], stmt: Statement,
                    variables: Optional[Set[str]] = None) -> str:
        if "&" not in text:
            return text
        variables = variables or set()
        missing: List[str] = []

        def replace(m: "re.Match[str]") -> str:
            name = "&" + m.group("name").upper()
            if name in params:
                return params[name]
            if name not in variables and not name.startswith("&SYS"):
                missing.append(name)
            # Left as written either way: conditional.py resolves the macro's own
            # variables, and an unsupplied parameter must stay visible rather than blank.
            return m.group(0)

        out = _VARIABLE.sub(replace, text)
        for name in dict.fromkeys(missing):
            self._flag(
                "line {0}: {1} was not supplied and is left visible in the generated "
                "statement - it was NOT blanked, because a field generated at position "
                "'' is a layout that is wrong rather than one that is absent".format(
                    stmt.line, name))
        return out


def _split_keyword(part: str) -> "tuple[Optional[str], str]":
    """``KEY=VALUE`` at parenthesis depth zero; anything else is positional."""
    depth = 0
    in_quote = False
    for i, ch in enumerate(part):
        if in_quote:
            in_quote = ch != "'"
            continue
        if ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            key = part[:i].strip()
            if SYMBOL.match(key):
                return key.upper(), part[i + 1:].strip()
            return None, part
    return None, part
