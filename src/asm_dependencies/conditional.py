"""Conditional assembly: deciding which statements exist at all.

This is the property that makes assembler a different problem from its sibling languages.
A ``COPY`` or a ``CALL`` can sit inside an ``AIF`` branch gated on ``&SYSPARM``, and
``&SYSPARM`` arrives as ``PARM=`` on the **assemble step's JCL**. So the source alone does
not determine what the module depends on::

         GBLC  &SITE
    &SITE    SETC  '&SYSPARM'
             AIF   ('&SITE' EQ 'PROD').PROD
             COPY  TESTPARM
             AGO   .DONE
    .PROD    ANOP
             COPY  PRODPARM
    .DONE    ANOP

Two wrong answers are available. Reporting both copybooks flatly says the module depends
on both, which is false for every real assembly. Picking one is worse - it is a guess
presented as a fact. So this pass does neither:

* Where the test **can** be decided from the source, it is decided, and the branch not
  taken is dropped. ``--sysparm PROD`` supplies the missing value and collapses the whole
  question to a precise answer.
* Where it **cannot**, both branches are kept and every statement in them is stamped with
  the condition that governs it. The manifest then says "PRODPARM, when
  ``'&SITE' EQ 'PROD'``" instead of either lie.

The evaluator returns ``None`` for anything it cannot work out, and ``None`` propagates:
an unknown operand makes the whole test unknown. That is deliberate. Guessing an operand
would decide a branch, and a wrongly decided branch does not produce a flag - it produces
a manifest that is quietly missing everything on the other side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .lexer import STATEMENT, Statement
from .model import Condition

#: Statements this pass consumes. Everything else is passed through untouched.
CONDITIONAL_OPS = frozenset({
    "AIF", "AGO", "ANOP", "ACTR", "MEXIT", "SETA", "SETB", "SETC",
    "LCLA", "LCLB", "LCLC", "GBLA", "GBLB", "GBLC",
})

_RELATIONAL = {"EQ", "NE", "LT", "GT", "LE", "GE"}
_VARIABLE = re.compile(r"&(?P<name>[A-Z@#$_][A-Z0-9@#$_]{0,61})(?P<dot>\.?)", re.I)
#: ``AIF (test).LABEL`` and the extended form ``AIF (t1).L1,(t2).L2``.
_AIF_BRANCH = re.compile(r"\((?P<test>.*?)\)\s*(?P<label>\.[A-Z@#$_][A-Z0-9@#$_]*)",
                         re.I | re.S)
_SEQ = re.compile(r"^\.[A-Z@#$_][A-Z0-9@#$_]*$", re.I)
_NUMBER = re.compile(r"^[+-]?\d+$")


@dataclass
class Environment:
    """The conditional-assembly variables whose values are known.

    A symbol that is *absent* is unknown, which is not the same as zero or null. Seeding it
    with the declared-but-unset defaults the assembler uses would make every ``AIF`` on an
    unsupplied variable decidable, and decide it wrongly.
    """

    values: Dict[str, object] = field(default_factory=dict)
    #: ``&SYSPARM``, when the caller supplied one. Without it, every test that reads it is
    #: undecidable - which is the truth, because it comes from the assemble step's JCL.
    sysparm: Optional[str] = None

    def __post_init__(self) -> None:
        if self.sysparm is not None:
            self.values["&SYSPARM"] = self.sysparm

    def get(self, name: str) -> Optional[object]:
        return self.values.get(name.upper())

    def set(self, name: str, value: object) -> None:
        self.values[name.upper()] = value

    def substitute(self, text: str) -> Tuple[str, bool]:
        """``(text with known variables replaced, whether all of them were known)``."""
        known = [True]

        def replace(m: "re.Match[str]") -> str:
            value = self.get("&" + m.group("name"))
            if value is None:
                known[0] = False
                return m.group(0)
            return str(value)

        return _VARIABLE.sub(replace, text), known[0]


# -- expression evaluation ---------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            j = i + 1
            lit = ["'"]
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        lit.append("''")
                        j += 2
                        continue
                    break
                lit.append(text[j])
                j += 1
            lit.append("'")
            out.append("".join(lit))
            i = j + 1
            continue
        if ch in "(),":
            out.append(ch)
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in "(),'":
            j += 1
        out.append(text[i:j])
        i = j
    return out


class _Expr:
    """A very small recursive-descent evaluator for HLASM logical expressions.

    ``None`` means "not decidable", and it propagates through every operator except the
    short-circuits: ``0 AND unknown`` is 0 and ``1 OR unknown`` is 1, which are real
    precision wins on the ``(&DEBUG EQ 1) AND ('&SYSPARM' EQ 'TEST')`` shape.
    """

    def __init__(self, tokens: Sequence[str], env: Environment) -> None:
        self.tokens = list(tokens)
        self.env = env
        self.pos = 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> Optional[str]:
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    def logical(self) -> Optional[bool]:
        left = self.conjunction()
        while (self.peek() or "").upper() == "OR":
            self.take()
            right = self.conjunction()
            if left is True or right is True:
                left = True
            elif left is None or right is None:
                left = None
            else:
                left = bool(left or right)
        return left

    def conjunction(self) -> Optional[bool]:
        left = self.negation()
        while (self.peek() or "").upper() == "AND":
            self.take()
            right = self.negation()
            if left is False or right is False:
                left = False
            elif left is None or right is None:
                left = None
            else:
                left = bool(left and right)
        return left

    def negation(self) -> Optional[bool]:
        if (self.peek() or "").upper() == "NOT":
            self.take()
            value = self.negation()
            return None if value is None else not value
        return self.relation()

    def relation(self) -> Optional[bool]:
        if self.peek() == "(":
            save = self.pos
            self.take()
            inner = self.logical()
            if self.peek() == ")":
                self.take()
                if (self.peek() or "").upper() not in _RELATIONAL:
                    return inner
            self.pos = save
        left = self.term()
        operator = (self.peek() or "").upper()
        if operator not in _RELATIONAL:
            if left is None:
                return None
            if isinstance(left, int):
                return left != 0
            return None
        self.take()
        right = self.term()
        return _compare(left, operator, right)

    def term(self) -> Optional[object]:
        token = self.take()
        if token is None:
            return None
        if token == "(":                       # a parenthesised arithmetic sub-expression
            depth, inner = 1, []
            while self.peek() is not None and depth:
                nxt = self.take()
                if nxt == "(":
                    depth += 1
                elif nxt == ")":
                    depth -= 1
                    if not depth:
                        break
                inner.append(nxt)
            return _arithmetic(inner, self.env)
        if token.startswith("'"):
            body = token[1:-1].replace("''", "'")
            resolved, known = self.env.substitute(body)
            value: Optional[object] = resolved if known else None
            if self.peek() == "(":             # substring: '&X'(1,4)
                start, length = self._substring()
                if value is None or start is None or length is None:
                    return None
                return str(value)[start - 1:start - 1 + length]
            return value
        if _NUMBER.match(token):
            return int(token)
        resolved, known = self.env.substitute(token)
        if not known:
            return None
        return int(resolved) if _NUMBER.match(resolved) else resolved

    def _substring(self) -> Tuple[Optional[int], Optional[int]]:
        self.take()                            # "("
        parts: List[str] = []
        current: List[str] = []
        while self.peek() is not None:
            token = self.take()
            if token == ")":
                break
            if token == ",":
                parts.append("".join(current))
                current = []
                continue
            current.append(token)
        parts.append("".join(current))
        try:
            resolved = [self.env.substitute(p)[0] for p in parts]
            return int(resolved[0]), int(resolved[1])
        except (IndexError, ValueError):
            return None, None


def _compare(left: Optional[object], operator: str,
             right: Optional[object]) -> Optional[bool]:
    if left is None or right is None:
        return None
    if isinstance(left, int) != isinstance(right, int):
        left, right = str(left), str(right)
    checks = {
        "EQ": left == right, "NE": left != right,
        "LT": left < right, "GT": left > right,
        "LE": left <= right, "GE": left >= right,
    }
    return bool(checks[operator])


def _arithmetic(tokens: Sequence[str], env: Environment) -> Optional[object]:
    """``&A+1``, ``&A*2`` - enough to follow a counter, and ``None`` for anything else."""
    text = " ".join(tokens)
    resolved, known = env.substitute(text)
    if not known:
        return None
    if _NUMBER.match(resolved.strip()):
        return int(resolved.strip())
    if re.fullmatch(r"[\d\s+\-*/()]+", resolved):
        try:
            return int(eval(resolved, {"__builtins__": {}}, {}))    # noqa: S307
        except Exception:
            return None
    return resolved.strip()


def evaluate_test(text: str, env: Environment) -> Optional[bool]:
    """A logical expression's value, or ``None`` when it cannot be decided."""
    try:
        return _Expr(_tokenize(text), env).logical()
    except Exception:
        return None


# -- the pass ----------------------------------------------------------------------

def _set_value(stmt: Statement, env: Environment, kind: str,
               flags: List[str]) -> None:
    name = stmt.name.strip()
    if not name.startswith("&"):
        return
    operand = stmt.operand.strip()
    if kind == "SETC":
        resolved, known = env.substitute(_concatenation(operand))
        if known:
            env.set(name, resolved)
        else:
            env.values.pop(name.upper(), None)
        return
    if kind == "SETB":
        value = evaluate_test(operand.strip("()"), env)
        if value is None:
            env.values.pop(name.upper(), None)
        else:
            env.set(name, 1 if value else 0)
        return
    value = _arithmetic(_tokenize(operand), env)
    if isinstance(value, int):
        env.set(name, value)
    else:
        env.values.pop(name.upper(), None)


def _concatenation(operand: str) -> str:
    """``'A'.'&B'`` -> ``A&B``. The period is a concatenation mark, not a character."""
    out: List[str] = []
    for token in _tokenize(operand):
        if token == ".":
            continue
        if token.startswith("'") and token.endswith("'") and len(token) >= 2:
            out.append(token[1:-1].replace("''", "'"))
        else:
            out.append(token)
    return "".join(out).replace("'.'", "")


def _labels(stmts: Sequence[Statement]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for index, stmt in enumerate(stmts):
        if stmt.sequence_symbol:
            out.setdefault(stmt.sequence_symbol.upper(), index)
        elif stmt.kind == STATEMENT and _SEQ.match(stmt.name or ""):
            out.setdefault(stmt.name.upper(), index)
    return out


def evaluate(stmts: Sequence[Statement], *, env: Optional[Environment] = None,
             flags: Optional[List[str]] = None) -> List[Statement]:
    """Stamp conditions onto statements, and drop the branches that are decidably dead.

    Regions rather than control-flow simulation: an ``AIF (t).T`` makes everything between
    it and ``.T`` conditional on ``NOT t``, and an ``AGO .T`` makes everything between it
    and ``.T`` reachable only by a branch into it. That covers the structured idiom every
    real macro is written in, and a backward ``AGO`` - a conditional-assembly loop - is
    flagged rather than unrolled, because unrolling it would need the interpreter this
    deliberately is not.
    """
    env = env or Environment()
    flags = flags if flags is not None else []
    statements = list(stmts)
    labels = _labels(statements)

    #: index -> the conditions covering it
    covering: Dict[int, List[Condition]] = {}
    #: indices to drop, because a decided test says they are not assembled
    dead: set = set()
    #: label index -> the condition of the AIF that branches to it. Recorded for DECIDED
    #: branches too, not only undecided ones: an AGO's else-arm is reached exactly when
    #: some AIF jumped into it, so a decided AIF decides the else-arm as well. Recording
    #: only the undecided ones left every else-arm looking conditional, and kept the arm
    #: of a branch that was decidably not taken.
    reached_by: Dict[int, Condition] = {}

    for index, stmt in enumerate(statements):
        if stmt.kind != STATEMENT:
            continue
        op = stmt.operation.upper()
        if op in ("SETA", "SETB", "SETC"):
            _set_value(stmt, env, op, flags)
            continue
        if op == "AIF":
            for m in _AIF_BRANCH.finditer(stmt.operand):
                target = labels.get(m.group("label").upper())
                condition = Condition(test=m.group("test").strip(), line=stmt.line)
                value = evaluate_test(condition.test, env)
                if value is not None:
                    condition.decided, condition.taken = True, value
                if target is None:
                    flags.append(
                        "line {0}: AIF branches to {1}, which is not a sequence symbol in "
                        "this part of the program - the branch was not followed, so "
                        "anything it selects is not distinguished".format(
                            stmt.line, m.group("label")))
                    continue
                if target <= index:
                    flags.append(
                        "line {0}: AIF branches backwards to {1} - a conditional-assembly "
                        "loop is not unrolled here, so statements in it are reported once "
                        "and their repetition is not modelled".format(
                            stmt.line, m.group("label")))
                    continue
                reached_by.setdefault(target, condition)
                if condition.decided and condition.taken:
                    dead.update(range(index + 1, target))
                elif not condition.decided:
                    for i in range(index + 1, target):
                        covering.setdefault(i, []).append(condition)
        elif op in ("AGO", "MEXIT"):
            if index in dead:
                continue          # the AGO itself is not assembled, so it skips nothing
            if op == "MEXIT":
                # MEXIT ends generation, so everything after it in the body is reached
                # only by a branch - the same shape as an AGO to the end.
                label, target = "the end of the macro", len(statements)
            else:
                label = stmt.operand.strip().upper()
                target = labels.get(label)
            if target is None or target <= index:
                continue
            # Statements between an AGO and its target are never reached by falling
            # through. They are the "else" arm, entered only by an AIF that branched into
            # them - so their fate is that AIF's.
            region = range(index + 1, target)
            entry = next((reached_by[i] for i in region if i in reached_by), None)
            if entry is None:
                flags.append(
                    "line {0}: the statements between this AGO and {1} are branched over "
                    "and nothing branches into them, so they are not assembled - anything "
                    "they declare is not a dependency of this module".format(
                        stmt.line, label))
                dead.update(region)
            elif entry.decided:
                if not entry.taken:
                    dead.update(region)
            else:
                for i in region:
                    if entry not in covering.get(i, []):
                        covering.setdefault(i, []).append(entry)

    out: List[Statement] = []
    for index, stmt in enumerate(statements):
        if index in dead:
            continue
        if stmt.kind == STATEMENT and stmt.operation.upper() in CONDITIONAL_OPS:
            continue
        conditions = covering.get(index)
        if conditions:
            stmt.conditions = list(stmt.conditions) + conditions
        out.append(stmt)
    return out
