"""What a parsed assembler module is.

The thing this model exists to carry is not a call graph. It is a call graph **plus how
each edge was established**, because in assembler that is the difference between a fact
and a guess:

* ``LINK EP=PAYCALC`` names its target in the statement.
* ``LINK EPLOC=PGMNAME`` names a *field*; the target is whatever ``PGMNAME`` holds, which
  is knowable only if a ``DC`` initialises it and nothing stores over it first.
* ``LINK EPLOC=(R1)`` names a register. The target is not in the source at all.

All three are real calls. Reporting the first two and dropping the third would describe a
module that dispatches a hundred programs from a table as having no dependencies, so every
site is recorded and carries its :data:`EVIDENCE_LITERAL`-to-:data:`EVIDENCE_DYNAMIC`
grading. ``conditional`` is kept separate from that grading rather than folded into it,
because it answers a different question: evidence says *how well the name is known*,
conditional says *whether the statement is assembled at all*, and a literal name inside an
undecided ``AIF`` branch is both perfectly known and possibly absent.

Every dataclass that can be short carries its own ``flags``. Nothing here is JSON;
:mod:`asm_dependencies.views` decides what is emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: How well a dependency's NAME is known. Serialized verbatim into the manifest, so the
#: string is the output contract.
#: The name is a token in the statement.
EVIDENCE_LITERAL = "literal"
#: The operand names a field a ``DC`` initialises, and no store into it reaches the site.
EVIDENCE_ASSIGNED = "assigned"
#: The name is one of a set of ``DC`` literals selected at run time. The SET is known; the
#: choice is not, so these are candidates rather than proven dependencies.
EVIDENCE_TABLE = "table"
#: Computed at run time - a register, a COMMAREA, a parameter, a store from elsewhere.
#: Nothing in the source names it.
EVIDENCE_DYNAMIC = "dynamic"

#: Section kinds. ``dsect`` and ``dxd`` define layout and generate no object code; only the
#: others become externally visible control sections.
SECTION_CSECT = "csect"
SECTION_RSECT = "rsect"
SECTION_START = "start"
SECTION_COM = "com"
SECTION_DSECT = "dsect"
SECTION_DXD = "dxd"

#: Section kinds the binder can resolve a reference to.
EXTERNALLY_VISIBLE = frozenset({SECTION_CSECT, SECTION_RSECT, SECTION_START, SECTION_COM})

#: How an external symbol reference was established. ``V-con`` is an IMPLICIT ``EXTRN`` -
#: naming a symbol in a V-type address constant declares it external without an ``EXTRN``
#: statement, and it is the highest-confidence dependency signal assembler has.
VIA_VCON = "V-con"
VIA_EXTRN = "EXTRN"
VIA_WXTRN = "WXTRN"
VIA_CALL = "CALL"
VIA_QCON = "Q-con"
VIA_JCON = "J-con"
VIA_RCON = "R-con"

#: References that are storage-layout dependencies rather than call dependencies: they name
#: an external dummy section or a PSECT, not something to transfer control to.
LAYOUT_REFS = frozenset({VIA_QCON, VIA_JCON, VIA_RCON})


@dataclass
class Condition:
    """One open conditional-assembly branch a statement sits inside.

    ``decided`` is False when the test could not be evaluated - typically because it reads
    ``&SYSPARM``, which is supplied by ``PARM=`` on the assemble step and is not in the
    source. Everything found under an undecided condition is reported WITH the condition
    rather than reported flatly or dropped.
    """

    test: str
    line: int
    decided: bool = False
    taken: Optional[bool] = None


@dataclass
class Section:
    """A ``CSECT``, ``START``, ``RSECT``, ``COM``, ``DSECT`` or ``DXD``."""

    name: str
    kind: str
    line: int
    origin: Optional[str] = None
    depth: int = 0
    amode: Optional[str] = None
    rmode: Optional[str] = None
    #: An ``ALIAS`` rewrite. When set, THIS is the name the binder sees; ``name`` is only
    #: what the source refers to it by.
    alias: Optional[str] = None
    flags: List[str] = field(default_factory=list)

    @property
    def external_name(self) -> str:
        return self.alias or self.name

    @property
    def is_visible(self) -> bool:
        return self.kind in EXTERNALLY_VISIBLE and bool(self.name)


@dataclass
class Entry:
    """An ``ENTRY`` operand - a name this module makes available to the binder.

    ``CSECT``/``START``/``RSECT`` names are entry points too, without needing an ``ENTRY``
    statement, so the provides index is the union of the two.
    """

    name: str
    line: int
    origin: Optional[str] = None
    depth: int = 0
    alias: Optional[str] = None
    amode: Optional[str] = None

    @property
    def external_name(self) -> str:
        return self.alias or self.name


@dataclass
class ExternalRef:
    """A symbol this module needs the binder to resolve."""

    name: str
    via: str
    line: int
    origin: Optional[str] = None
    depth: int = 0
    #: ``WXTRN``, or an ``EXTRN PART``: the binder does not search libraries for it and
    #: leaving it unresolved is not a broken build. A distinct edge, not a weaker one.
    weak: bool = False
    #: ``=V(X)`` rather than ``DC V(X)`` - the same dependency, from the literal pool.
    literal_pool: bool = False
    conditions: List[Condition] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    @property
    def is_layout(self) -> bool:
        return self.via in LAYOUT_REFS


@dataclass
class DataDef:
    """A labelled ``DC``/``DS``, kept so an ``EPLOC=`` or ``PROGRAM()`` operand naming it
    can be resolved to the characters it was initialised with.

    ``values`` holds every character constant in the operand, in order, so a dispatch table
    (``DC CL8'PAY001',CL8'PAY002'``) yields its whole candidate set rather than only its
    first entry.
    """

    label: str
    line: int
    operand: str = ""
    values: List[str] = field(default_factory=list)
    origin: Optional[str] = None
    depth: int = 0
    section: Optional[str] = None

    @property
    def value(self) -> Optional[str]:
        return self.values[0] if len(self.values) == 1 else None


@dataclass
class Store:
    """An instruction that writes into a labelled field.

    This is the decoy check. ``PGMNAME DC CL8'PAYCALC'`` followed by
    ``MVC PGMNAME,SELECTED`` means the literal in the source is not what the field holds
    when the call is made - and a tool that reports ``PAYCALC`` with confidence is worse
    than one that reports nothing, because it is confidently wrong.
    """

    target: str
    operation: str
    line: int
    section: Optional[str] = None
    #: The source operand, when it is itself a literal - ``MVC PGMNAME,=CL8'OTHER'`` sets a
    #: knowable value rather than an unknowable one.
    literal: Optional[str] = None


@dataclass
class Invocation:
    """One site that passes control to, or loads, another module."""

    verb: str                          # LINK, XCTL, ATTACH, LOAD, DELETE, CALL, CEEFETCH
    line: int
    target: Optional[str] = None
    evidence: str = EVIDENCE_DYNAMIC
    #: Why the target is not a literal, in words that name the operand form responsible.
    reason: Optional[str] = None
    #: Other names the site could reach, when it selects from a table.
    candidates: List[str] = field(default_factory=list)
    #: The ddname of the library the module is loaded from (``DCB=`` / ``TASKLIB=``).
    library: Optional[str] = None
    #: The label whose CONTENTS name the target - ``EPLOC=PGMNAME``, ``DE=BLDLNAME``.
    #: ``dataflow`` resolves it to the characters a ``DC`` put there, or leaves it dynamic
    #: when a store reaches the site first.
    via_field: Optional[str] = None
    #: ``MF=L`` / ``SF=L``: this statement DEFINES a parameter list and calls nothing. The
    #: name and the call are then hundreds of lines apart, so counting a list form as a
    #: call double-counts, and reading only the execute form finds no name at all.
    list_form: bool = False
    #: The parameter list this site defines (list form) or uses (``MF=(E,LINKLST)``).
    list_label: Optional[str] = None
    #: ``XCTL`` transfers and does not return; ``LINK``/``CALL`` return. A different edge.
    transfers: bool = False
    #: The control section the site is in. Pairing a register form with the LOAD that
    #: fed it is only defensible inside one section.
    section: Optional[str] = None
    origin: Optional[str] = None
    depth: int = 0
    conditions: List[Condition] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class FileRef:
    """A ``DCB`` or ``ACB``, and therefore a ddname."""

    ddname: Optional[str]
    line: int
    kind: str = "dcb"                  # dcb | acb
    label: Optional[str] = None
    dsorg: Optional[str] = None
    macrf: Optional[str] = None
    io: str = "unknown"                # read | write | read-write | unknown
    #: Whether an OPEN names this control block. A DCB can be declared and never
    #: opened, which is a dead declaration rather than a file the module uses.
    opened: bool = False
    evidence: str = EVIDENCE_LITERAL
    reason: Optional[str] = None
    #: ``EODAD``/``SYNAD`` targets. Intra-module, but the only way to find end-of-file and
    #: I/O-error handling.
    exits: Dict[str, str] = field(default_factory=dict)
    origin: Optional[str] = None
    depth: int = 0
    conditions: List[Condition] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class ResourceRef:
    """A subsystem resource named by an ``EXEC CICS``, an ``EXEC SQL`` or a DL/I call.

    One type for all three because the question is the same in each: a name, a kind, which
    way the data moves, and whether the name was a literal or a data area. What differs is
    only the vocabulary, and that lives in ``kind``.
    """

    name: str
    kind: str                          # db2-table | terminal-map | cics-transaction |
                                       # file | queue | segment | db2-plan
    line: int
    subsystem: str = ""                # CICS | Db2 | IMS
    verb: str = ""                     # the command that named it
    io: Optional[str] = None
    evidence: str = EVIDENCE_LITERAL
    reason: Optional[str] = None
    origin: Optional[str] = None
    depth: int = 0
    conditions: List[Condition] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class MemberUse:
    """A ``COPY`` member or a macro this module pulled in."""

    name: str
    line: int
    kind: str = "copybook"             # copybook | macro
    resolved: bool = False
    origin: Optional[str] = None
    depth: int = 0
    #: ``COPY &MEM`` - the member name is built by conditional assembly and is not lexical.
    computed: bool = False
    conditions: List[Condition] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class Module:
    """One assembled member: what it provides, what it needs, and what it could not say."""

    name: str
    source_name: str = "<asm>"
    sections: List[Section] = field(default_factory=list)
    entries: List[Entry] = field(default_factory=list)
    externals: List[ExternalRef] = field(default_factory=list)
    invocations: List[Invocation] = field(default_factory=list)
    files: List[FileRef] = field(default_factory=list)
    resources: List[ResourceRef] = field(default_factory=list)
    members: List[MemberUse] = field(default_factory=list)
    data: List[DataDef] = field(default_factory=list)
    stores: List[Store] = field(default_factory=list)
    #: The ``END`` operand: where control goes when loading completes.
    end_label: Optional[str] = None
    margins: Optional[object] = None
    flags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def provides(self) -> List[str]:
        """Every name the binder can resolve a reference TO in this module.

        The union of the visible section names and the ``ENTRY`` operands, under whatever
        ``ALIAS`` rewrote them - because an ``ALIAS`` means the name in the source is not
        the name another module binds to.
        """
        names = [s.external_name for s in self.sections if s.is_visible]
        names += [e.external_name for e in self.entries]
        return sorted(set(n for n in names if n))
