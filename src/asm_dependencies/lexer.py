"""HLASM source text -> logical statements with their fields already split.

Assembler is a column language, and four things have to happen before a single statement
can be read. Each of them silently corrupts the model if it is skipped:

* **The scan area is columns 1-71.** Column 72 is the continuation indicator and 73-80 is
  the identification-sequence field, which the assembler never reads. Real members carry
  member names, dates and change-ticket ids there, so a reader that takes the whole line
  finds ``PAYCALC`` in the sequence field of every line of the module and reports it as a
  dependency on itself.
* **Continuation is a COLUMN, not a character.** A non-blank in column 72 continues the
  statement, and the continuation resumes at column 16 - columns 1-15 of that line are not
  part of it. Reading continued statements as separate lines loses every operand after the
  first break, which for a ``DC`` list or a long ``EXEC CICS`` command is most of them.
* **The margins are settable.** ``ICTL begin,end,continue`` moves all three, and it does
  **not** propagate into COPY members or library macros - they always use 1/71/16. So one
  assembly can have two column geometries, and the expander has to lex copied text with
  the defaults rather than with the containing member's margins.
* **The operand ends at the first blank outside quotes and parentheses.** There is no
  comment character: everything after the operand is remarks. ``DC C'A,B'`` and
  ``DC C'THIS IS NOT A COMMENT'`` both defeat splitting on blanks or commas naively.

Comments are passed through rather than dropped. ``*`` in the begin column is an ordinary
comment and ``.*`` is a macro comment, and both matter downstream: the CICS translator
leaves the original ``EXEC CICS`` command as a comment beside the ``DFHECALL`` it
generated, and ``*ASM XOPTS(...)`` carries translator options. A comment whose column 72
is non-blank **swallows the following line**, which is the one comment rule that changes
what the next statement is.

Nothing here knows what a CSECT or a DCB is. This module produces statements; the parser
decides what they mean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

#: HLASM's defaults, and what COPY members and library macros always use.
DEFAULT_BEGIN = 1
DEFAULT_END = 71
DEFAULT_CONTINUE = 16

#: The identification-sequence field, for deciding whether a tail past the end column is
#: the ordinary sequence area (say nothing) or dropped source (say so).
_ID_FIELD_END = 80

#: How many ``*PROCESS`` statements the assembler honours before treating them as comments.
_MAX_PROCESS = 10

_SEQUENCE_TAIL = re.compile(r"^[A-Z0-9$#@._-]+$", re.I)

#: A symbol: 1-63 characters, first alphabetic or @ # $, rest alphanumeric or @ # $ _.
#: Underscore is legal in HLASM symbols; the hyphen is NOT, which is what separates an
#: assembler symbol from a COBOL or Easytrieve name.
SYMBOL = re.compile(r"^[A-Z@#$_][A-Z0-9@#$_]{0,62}$", re.I)

#: A sequence symbol - a period and then a symbol. In a NAME field this means "no name":
#: ``.RETRY CSECT`` starts the UNNAMED control section, it does not name one ``.RETRY``.
SEQUENCE_SYMBOL = re.compile(r"^\.[A-Z@#$_][A-Z0-9@#$_]{0,61}$", re.I)

#: A variable symbol. Its presence in a name, operation or operand field means the value is
#: produced by conditional assembly and is not lexically knowable.
VARIABLE_SYMBOL = re.compile(r"&[A-Z@#$_][A-Z0-9@#$_]{0,61}", re.I)

STATEMENT = "statement"
COMMENT = "comment"
MACRO_COMMENT = "macro-comment"
PROCESS = "process"


@dataclass(frozen=True)
class Margins:
    """The three column boundaries, as ``ICTL`` sets them."""

    begin: int = DEFAULT_BEGIN
    end: int = DEFAULT_END
    cont: int = DEFAULT_CONTINUE

    @property
    def indicator(self) -> int:
        """The continuation-indicator column: always the one after the end column."""
        return self.end + 1

    @property
    def continuation_allowed(self) -> bool:
        """``ICTL`` with ``end`` at 80, or with no ``continue``, forbids continuation."""
        return self.end < _ID_FIELD_END and self.cont > self.begin


DEFAULT_MARGINS = Margins()

#: Where an ``EXEC CICS`` continuation may begin. IBM: without a trailing period or comma
#: "the following line must begin at or after column 2"; with one, "between column 2 and
#: column 16". Column 2 covers both, and leading blanks are stripped either way.
CICS_CONTINUE = 2


def is_exec(operation: str) -> bool:
    """``EXEC CICS`` and ``EXEC SQL`` are not assembler statements at all.

    They are a preprocessor's language embedded in the operand field, and two ordinary
    assembler rules do not hold for them. Their operands are separated by BLANKS rather
    than commas, so the operand does not end at the first blank - and they have no remarks
    field, so everything to the right margin is part of the command. Splitting one the
    assembler's way keeps `CICS` and throws the command away as a remark.
    """
    return operation.upper() == "EXEC"


def _exec_operand(content: str, margins: "Margins") -> str:
    """Everything after the operation, verbatim: an EXEC command has no remarks."""
    body = content.rstrip()
    i = 0
    if body[:1].strip():                       # a name field, if there is one
        while i < len(body) and body[i] != " ":
            i += 1
    while i < len(body) and body[i] == " ":
        i += 1
    while i < len(body) and body[i] != " ":    # the operation
        i += 1
    return body[i:].strip()


def _exec_cics_margins(margins: "Margins", operation: str, operand: str) -> "Margins":
    """The margins to join THIS statement's continuations with.

    Only an ``EXEC CICS`` differs, and only in its continue column. Everything else -
    including ``EXEC SQL``, which follows the assembler rule - joins the ordinary way.
    """
    if operation.upper() != "EXEC" or not operand.upper().startswith("CICS"):
        return margins
    if margins.cont <= CICS_CONTINUE:
        return margins
    return Margins(begin=margins.begin, end=margins.end, cont=CICS_CONTINUE)


@dataclass
class Statement:
    """One assembler statement, continuations already joined and fields already split."""

    operation: str
    #: The name field. ``""`` when it was blank OR held a sequence symbol - the assembler
    #: treats both as "no name", and collapsing them here is what stops ``.LOOP CSECT``
    #: from being read as a control section called ``.LOOP``.
    name: str = ""
    operand: str = ""
    remarks: str = ""
    #: ``statement`` | ``comment`` | ``macro-comment`` | ``process``. Comments carry their
    #: whole text in ``operand`` and an empty ``operation``.
    kind: str = STATEMENT
    line: int = 0                   # 1-relative physical line the statement starts on
    raw: str = ""                   # the physical line(s), before trimming and joining
    #: The COPY member or macro this statement came out of - ``None`` for the module's own
    #: text. Carried onto the artifact rows, because "this V-con is in macro SITELINK" is
    #: a question a reader of the expanded text cannot otherwise ask.
    origin: Optional[str] = None
    #: Expansion nesting depth at which it was produced (0 = the module source).
    depth: int = 0
    #: The sequence symbol in the name field, if there was one. Kept because ``AIF``/``AGO``
    #: branch to it, so the conditional-assembly pass needs it even though the assembler
    #: treats the name field as empty.
    sequence_symbol: Optional[str] = None
    #: The open ``AIF``/``AGO`` nesting this statement sits inside. The lexer NEVER sets
    #: this - ``conditional.py`` stamps it and the parser copies it onto whatever the
    #: statement produces. It lives here because it has to travel with the statement
    #: through macro expansion, and the expander moves statements, not model objects.
    conditions: List[object] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    @property
    def is_statement(self) -> bool:
        return self.kind == STATEMENT

    @property
    def text(self) -> str:
        """The statement as the assembler would read it, without remarks."""
        head = "{0} {1}".format(self.name, self.operation).strip()
        return "{0} {1}".format(head, self.operand).rstrip()

    @property
    def has_variable_symbol(self) -> bool:
        """True when any field is built by substitution, so its value is not lexical."""
        return bool(VARIABLE_SYMBOL.search(
            "{0} {1} {2}".format(self.name, self.operation, self.operand)))


def _looks_like_sequence(tail: str) -> bool:
    """The identification-sequence area: a short run of identifier characters.

    Recognised only so the ordinary case - every line of every member in an 80-byte PDS -
    does not emit a flag. A tail that is not shaped like this is very likely source that
    the assembler would have ignored, and saying so is the point.
    """
    t = tail.strip()
    return bool(t) and len(t) <= 8 and _SEQUENCE_TAIL.match(t) is not None


def _content(line: str, margins: Margins, *, first: bool) -> str:
    """The part of a physical line the assembler reads, without the indicator column."""
    start = (margins.begin if first else margins.cont) - 1
    return line[start:margins.end]


def _continues(line: str, margins: Margins) -> bool:
    """True when the indicator column holds a non-blank."""
    if not margins.continuation_allowed:
        return False
    idx = margins.indicator - 1
    return len(line) > idx and line[idx].strip() != ""


def _check_tail(line: str, margins: Margins, lineno: int, flags: List[str]) -> None:
    """Report source past the end column that is not the sequence area."""
    tail = line[margins.indicator:]
    if not tail.strip():
        return
    beyond_id = line[_ID_FIELD_END:].strip()
    if beyond_id or not _looks_like_sequence(tail):
        flags.append(
            "line {n}: text past column {m} was ignored, as the assembler ignores it - "
            "{t!r}. If this member is not in fixed 80-byte format, set the margins with "
            "--right-margin or an ICTL statement".format(
                n=lineno, m=margins.end, t=tail.strip()[:30]))


def split_operand(text: str, start: int = 0) -> Tuple[str, str]:
    """``(operand, remarks)`` - the operand runs to the first blank outside quotes.

    Parenthesis depth is tracked as well as quoting, because ``DC 0CL8(L'A B)`` and
    sublists like ``ATTACH EP=X,PARAM=(A, B)`` put blanks inside parentheses where they do
    not end the operand. A quote inside an operand may be doubled to embed one.
    """
    i, n = start, len(text)
    depth = 0
    in_quote = False
    while i < n:
        ch = text[i]
        if in_quote:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == " " and depth == 0:
            break
        i += 1
    return text[start:i], text[i:].strip()


def split_fields(content: str, margins: Margins) -> Tuple[str, str, str, str]:
    """One physical line's content -> ``(name, operation, operand, remarks)``.

    A non-blank in the begin column starts a name; otherwise the line has none. Everything
    after the operation is handed to :func:`split_operand`, which is the only thing that
    knows where remarks start.
    """
    body = content.rstrip()
    if not body.strip():
        return "", "", "", ""
    name = ""
    i = 0
    if body[:1].strip():
        j = 0
        while j < len(body) and body[j] != " ":
            j += 1
        name, i = body[:j], j
    while i < len(body) and body[i] == " ":
        i += 1
    j = i
    while j < len(body) and body[j] != " ":
        j += 1
    operation = body[i:j]
    while j < len(body) and body[j] == " ":
        j += 1
    operand, remarks = split_operand(body, j)
    return name, operation, operand, remarks


def read_ictl(physical: Sequence[str], flags: List[str]) -> Margins:
    """The margins in force for this member, from an ``ICTL`` if it has one.

    ``ICTL`` is valid only as the very first statement, so this looks no further than the
    leading comments. A malformed or out-of-range operand is flagged and the defaults are
    kept - guessing a geometry would mis-read every line of the member.
    """
    for lineno, raw in enumerate(physical, start=1):
        stripped = raw.strip()
        if not stripped or raw[:1] == "*":
            continue
        name, operation, operand, _ = split_fields(raw[:DEFAULT_END], DEFAULT_MARGINS)
        if operation.upper() != "ICTL":
            return DEFAULT_MARGINS
        parts = [p.strip() for p in operand.split(",")] if operand else []
        try:
            begin = int(parts[0])
            end = int(parts[1]) if len(parts) > 1 and parts[1] else DEFAULT_END
            cont = int(parts[2]) if len(parts) > 2 and parts[2] else 0
        except (IndexError, ValueError):
            flags.append(
                "line {n}: ICTL operand {o!r} could not be read - the default margins "
                "1/{e}/{c} were used, so any statement this member continues differently "
                "is mis-read".format(n=lineno, o=operand, e=DEFAULT_END,
                                     c=DEFAULT_CONTINUE))
            return DEFAULT_MARGINS
        if not (1 <= begin <= 40 and 41 <= end <= 80 and end >= begin + 5
                and (cont == 0 or (begin < cont <= 40 and cont < end))):
            flags.append(
                "line {n}: ICTL {o} is outside the ranges the assembler accepts "
                "(begin 1-40, end 41-80 and at least begin+5, continue begin+1 to 40) - "
                "the default margins were used instead".format(n=lineno, o=operand))
            return DEFAULT_MARGINS
        if cont == 0:
            flags.append(
                "line {n}: ICTL {o} gives no continue column, so this member cannot "
                "continue statements - a non-blank in column {i} is part of no "
                "statement".format(n=lineno, o=operand, i=end + 1))
        return Margins(begin=begin, end=end, cont=cont or (end + 1))
    return DEFAULT_MARGINS


def logical_lines(text: str, *, margins: Optional[Margins] = None
                  ) -> Tuple[List[Statement], List[str]]:
    """Physical source -> (statements, flags).

    Continuations are joined, fields are split, and comments are passed through tagged
    rather than dropped. ``margins`` defaults to whatever an ``ICTL`` in this text sets,
    falling back to 1/71/16 - pass it explicitly for COPY members and library macros,
    which always use the defaults whatever the containing member did.
    """
    flags: List[str] = []
    physical = text.splitlines()
    if margins is None:
        margins = read_ictl(physical, flags)

    out: List[Statement] = []
    i, n = 0, len(physical)
    process_count = 0
    in_header = True

    while i < n:
        lineno = i + 1
        raw = physical[i]

        if not raw.strip():
            i += 1
            continue

        begin_char = raw[margins.begin - 1:margins.begin] if len(raw) >= margins.begin \
            else ""
        is_macro_comment = raw[margins.begin - 1:margins.begin + 1] == ".*"
        is_comment = begin_char == "*"

        if is_comment or is_macro_comment:
            body = _content(raw, margins, first=True)
            upper = body.upper()
            if (upper.startswith("*PROCESS") and in_header
                    and process_count < _MAX_PROCESS and not is_macro_comment):
                process_count += 1
                out.append(Statement(operation="*PROCESS",
                                     operand=body[len("*PROCESS"):].strip(),
                                     kind=PROCESS, line=lineno, raw=raw))
                i += 1
                continue
            # A comment whose indicator column is non-blank continues, and the line it
            # continues onto is part of the COMMENT - not a statement of its own.
            raws = [raw]
            while _continues(physical[i], margins) and i + 1 < n:
                i += 1
                raws.append(physical[i])
                swallowed = physical[i]
                _, operation, _, _ = split_fields(
                    _content(swallowed, margins, first=True), margins)
                if operation:
                    # The assembler really does absorb this line into the comment, so the
                    # parse is right - but a comment whose prose merely happens to reach
                    # column 72 has just eaten a statement, and nothing else in the output
                    # would ever show it.
                    flags.append(
                        "line {n}: the comment starting at line {c} runs into column {i}, "
                        "so the assembler treats this line as part of it - the statement "
                        "{o} on it is NOT assembled. If the comment was not meant to "
                        "continue, its text reaches the continuation column.".format(
                            n=i + 1, c=lineno, i=margins.indicator, o=operation))
                body = body + _content(swallowed, margins, first=False)
            out.append(Statement(
                operation="", operand=body.rstrip(),
                kind=MACRO_COMMENT if is_macro_comment else COMMENT,
                line=lineno, raw="\n".join(raws)))
            i += 1
            continue

        stmt_flags: List[str] = []
        _check_tail(raw, margins, lineno, flags)
        content = _content(raw, margins, first=True)
        name, operation, operand, remarks = split_fields(content, margins)
        exec_command = is_exec(operation)
        if exec_command:
            operand, remarks = _exec_operand(content, margins), ""
        raws = [raw]

        # An EXEC CICS command continues at column 2 or after, NOT at the assembler's
        # continue column. Taking such a line from column 16 silently drops the first
        # operand of every continuation written in the ordinary CICS style, and the
        # command has no END-EXEC for the loss to show up against. EXEC SQL is NOT the
        # same: it follows the assembler rule, and only `EXEC SQL` itself must fit on one
        # line. `margins` itself is untouched - this applies to one statement.
        joining = _exec_cics_margins(margins, operation, operand)

        while _continues(physical[i], margins):
            i += 1
            if i >= n:
                stmt_flags.append(
                    "line {n}: the statement ends the member with a continuation "
                    "character in column {c} and nothing to continue onto".format(
                        n=lineno, c=margins.indicator))
                break
            nxt = physical[i]
            raws.append(nxt)
            _check_tail(nxt, margins, i + 1, flags)
            gutter = nxt[joining.begin - 1:joining.cont - 1]
            if gutter.strip():
                stmt_flags.append(
                    "line {n}: a continuation line has text before the continue column "
                    "({c}), which the assembler ignores - {t!r}".format(
                        n=i + 1, c=joining.cont, t=gutter.strip()[:20]))
            body = _content(nxt, joining, first=False).strip()
            if exec_command:
                # Blank-separated, and all of it: an EXEC command's operands are not
                # commas-and-remarks, so joining them the assembler's way would run
                # PROGRAM('X') into COMMAREA(Y) and drop everything after the first.
                operand = "{0} {1}".format(operand, body).strip()
            else:
                more, tail = split_operand(body)
                operand = operand + more
                if tail:
                    remarks = "{0} {1}".format(remarks, tail).strip()
            if not _continues(nxt, margins):
                break

        sequence_symbol = None
        if SEQUENCE_SYMBOL.match(name):
            # A sequence symbol in the name field IS no name. Keeping it in `name` would
            # invent a control section, a macro or an EQU called `.RETRY`.
            sequence_symbol, name = name, ""

        out.append(Statement(name=name, operation=operation, operand=operand,
                             remarks=remarks, kind=STATEMENT, line=lineno,
                             raw="\n".join(raws), sequence_symbol=sequence_symbol,
                             flags=stmt_flags))
        if operation.upper() not in ("ICTL", "*PROCESS"):
            in_header = False
        i += 1

    for stmt in out:
        flags.extend(stmt.flags)
    return out, flags


def operands(operand: str) -> List[str]:
    """Split an operand field on top-level commas, keeping quotes and sublists whole.

    ``EP=PAYCALC,PARAM=(A,B),VL=1`` -> ``["EP=PAYCALC", "PARAM=(A,B)", "VL=1"]``. A comma
    inside quotes or parentheses is part of its operand, not a separator - which is what
    keeps ``DC C'A,B'`` one operand and ``PARAM=(A,B)`` one keyword.
    """
    out: List[str] = []
    cur: List[str] = []
    depth = 0
    in_quote = False
    i, n = 0, len(operand)
    while i < n:
        ch = operand[i]
        if in_quote:
            cur.append(ch)
            if ch == "'":
                if i + 1 < n and operand[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            del cur[:]
        else:
            cur.append(ch)
        i += 1
    if cur or out:
        out.append("".join(cur))
    return [o.strip() for o in out]


def keyword(operand_part: str) -> Tuple[Optional[str], str]:
    """``EP=PAYCALC`` -> ``("EP", "PAYCALC")``; ``(2,12)`` -> ``(None, "(2,12)")``.

    Only an ``=`` outside quotes and parentheses separates a keyword from its value, so
    ``DDNAME=X`` splits and ``=V(PAYCALC)`` (a literal, which begins with ``=``) does not.
    """
    if operand_part.startswith("="):
        return None, operand_part
    depth = 0
    in_quote = False
    for i, ch in enumerate(operand_part):
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
            key = operand_part[:i]
            if SYMBOL.match(key):
                return key.upper(), operand_part[i + 1:]
            return None, operand_part
    return None, operand_part


def is_register_form(value: str) -> bool:
    """``(1)``, ``(R1)``, ``(15)`` - a parenthesised register, so the value is in a
    register at run time and no name is lexically present."""
    v = value.strip()
    return v.startswith("(") and v.endswith(")") and "," not in v and bool(v[1:-1].strip())


_QUOTED = re.compile(r"^(?:[A-Z]+L?\d*)?'(?P<body>(?:[^']|'')*)'$", re.I)


def is_literal(token: str) -> bool:
    """A quoted or typed constant: ``'ABC'``, ``C'ABC'``, ``CL8'ABC'``, ``X'0C'``."""
    return bool(_QUOTED.match(token.strip()))


def literal_value(token: str) -> str:
    """The characters inside a quoted or typed constant, with ``''`` unescaped."""
    m = _QUOTED.match(token.strip())
    return m.group("body").replace("''", "'") if m else token
