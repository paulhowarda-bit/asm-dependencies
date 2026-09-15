"""The two JSON views, in the shape the rest of the family emits.

A consumer that already reads ``jcl-dependencies`` and ``eztrieve-dependencies`` output
must be able to read this with the same code, so the structure is not this package's to
invent:

* top-level keys, in order: ``format``, ``formatVersion``, the subject, ``source``,
  ``note``, ``artifacts``, ``excluded``, ``flags``. ``formatVersion`` arrived with
  upstream ledger batch 10 item 30 and is family-wide;
* the subject key is spelled **``program``** - ``mainframe_artifacts.fetch`` reads
  ``subject``, then ``program``/``job``/``region``, to know what NOT to fetch, and a
  manifest keyed ``module`` would silently make the module request itself as its own
  dependency;
* every artifact row carries ``artifact``, ``kind``, ``dependency``, ``identity`` and one
  of ``resolvedBy``/``needs``, with ``io`` and ``touchedBy`` where the kind has them;
* ``dependency`` is ``runtime`` or ``compile-time`` and nothing else;
* a boolean flag is present-and-true or absent - ``false`` is never written;
* rows sort by ``(class order, artifact name)``, and every list built from a set is sorted
  before it is emitted, because iteration order would otherwise leak the hash seed into
  the bytes.

What this package adds to that shape is **``evidence``** on rows whose name had to be
established rather than read: ``literal``, ``assigned``, ``table`` or ``dynamic``. It is
the one genuinely new field, and it exists because in assembler a dependency's name is
often not in the source at all. ``conditional`` is kept separate from it - evidence says
how well the name is known, ``conditional`` says whether the statement is assembled at
all, and a literal name inside an undecided ``AIF`` branch is both perfectly known and
possibly absent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from mainframe_artifacts.dependents import output_rows
from mainframe_artifacts.synonyms import FROM_MAP

from . import VIEW_SCHEMA_VERSION
from . import classify
from .model import (
    EVIDENCE_DYNAMIC, EVIDENCE_LITERAL, EVIDENCE_TABLE, LAYOUT_REFS, Module,
)

FORMAT_ARTIFACTS = "asm-dependencies-artifacts"
FORMAT_LINEAGE = "asm-dependencies-lineage"
FORMAT_DEPENDENTS = "asm-dependencies-dependents"

#: Emission order for the manifest. An unknown kind sorts last rather than crashing.
_CLASS_ORDER = {
    "program": 0, "file": 1, "db2-table": 2, "terminal-map": 3, "cics-transaction": 4,
    "queue": 5, "dataset": 6, "copybook": 7, "macro": 8,
}

_ARTIFACTS_NOTE = (
    "One row per artifact this module is related to: modules it calls or loads "
    "(dependency: runtime), ddnames it opens (runtime - the JCL DD statement says which "
    "dataset each is), and COPY members and macros (compile-time: assembled into the "
    "source before it can be read at all). 'evidence' says how the name was established - "
    "'literal' means it is a token in the statement, 'assigned' means a DC initialises the "
    "field the operand names and no store into it reaches the site, 'table' means the name "
    "is one of a set of constants selected at run time, and 'dynamic' means it is computed "
    "at run time and is not in this source. A row marked 'conditional' sits inside an "
    "AIF branch this tool could not decide, and 'condition' names the test - supply "
    "--sysparm to decide it. 'provides' is what this module can be called AS, which is the "
    "other half of the call graph. 'candidates' are names a site COULD reach - a dispatch "
    "table's entries, say - kept out of 'artifacts' because which one a run reaches is "
    "control flow rather than text, and reported here because a module whose every call is "
    "table-driven would otherwise have an empty manifest."
)

_LINEAGE_NOTE = (
    "Every site, in source order, rather than the deduplicated manifest: what the module "
    "provides, each call and load with the evidence for its target, each external symbol "
    "the binder must resolve, each ddname it declares, and each COPY member and macro it "
    "was assembled from. 'unresolved' is the explicit inventory of sites whose target is "
    "decided at run time - they are real calls whose destination is not in the source, and "
    "listing them is the difference between a module that has no dependencies and one "
    "whose dependencies cannot be read."
)


def _conditions(rows) -> List[dict]:
    return [{"test": c.test, "line": c.line} for c in rows]


def _stamp_conditions(row: dict, conditions) -> None:
    """Presence-only, like every other boolean in the family."""
    if conditions:
        row["conditional"] = True
        row["condition"] = _conditions(conditions)


def _touch(line: int, origin: Optional[str]) -> dict:
    touch = {"line": line}
    if origin:
        touch["inMember"] = origin
    return touch


# -- the artifacts view ------------------------------------------------------------

def build_asm_artifacts(module: Module, synonyms=None) -> dict:
    """The manifest: one row per artifact, deduplicated, with how each was established.

    ``synonyms`` is optional Db2 SYNONYM/ALIAS knowledge (a ``SynonymLookup``). Without
    it every table is reported exactly as the source wrote it, which is honest but
    leaves a synonym looking like a table."""
    artifacts: List[dict] = []
    excluded: List[dict] = []

    _programs(module, artifacts, excluded)
    _files(module, artifacts)
    _resources(module, artifacts, excluded, synonyms)
    _members(module, artifacts, excluded)

    artifacts.sort(key=lambda r: (_CLASS_ORDER.get(r["kind"], 9), r["artifact"]))
    excluded.sort(key=lambda r: (r["kind"], r["name"]))

    return {
        "format": FORMAT_ARTIFACTS,
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": module.name,
        "source": module.source_name,
        "note": _ARTIFACTS_NOTE,
        "provides": _provides(module),
        "artifacts": artifacts,
        "candidates": _candidates(module),
        "excluded": excluded,
        "flags": list(module.flags) + (
            [("synonym resolver failed mid-run ({0}); synonyms it did not reach stay "
              "unresolved - fix the resolver and re-run").format(
                  synonyms.disabled_reason)]
            if synonyms is not None and synonyms.disabled_reason else []),
    }


def _candidates(module: Module) -> List[dict]:
    """Names a site COULD reach, kept OUT of ``artifacts`` and visible anyway.

    A dispatch table's entries are not proven dependencies - which one a run reaches is
    control flow, not text - so listing them among the artifacts would overclaim. Listing
    them nowhere is worse: a module that dispatches a hundred programs from a table would
    then have an empty manifest, and an empty manifest is indistinguishable from the
    manifest of a module that genuinely calls nothing.

    So they go here. A graph loader can add them as tentative edges and label them as such,
    and the retrieval stage can fetch them the way ``mainframe_artifacts.fetch``'s
    ``candidate_requests`` fetches a dynamic CALL's candidates - worth having, never
    counted as a dependency.
    """
    rows: Dict[str, dict] = {}
    for call in module.invocations:
        for name in call.candidates:
            if name == call.target:
                continue
            row = rows.get(name)
            if row is None:
                row = rows[name] = {
                    "candidate": name,
                    "kind": "program",
                    "evidence": call.evidence,
                    "needs": ("confirmation that this site reaches this name - the set is "
                              "in the source, the choice is made at run time"),
                    "forSite": [],
                }
            row["forSite"].append({"site": call.verb, "line": call.line,
                                   "reason": call.reason})
    return [rows[name] for name in sorted(rows)]


def _provides(module: Module) -> List[dict]:
    """What another module can bind to. No sibling emits this, and it is what answers
    'who calls PAYCALC' once a graph loader has both halves."""
    rows: Dict[str, dict] = {}
    for section in module.sections:
        if not section.is_visible:
            continue
        row = {"name": section.external_name, "kind": section.kind}
        if section.alias:
            row["symbol"] = section.name
            row["resolvedVia"] = "ALIAS"
        if section.amode:
            row["amode"] = section.amode
        if section.rmode:
            row["rmode"] = section.rmode
        rows[section.external_name] = row
    for entry in module.entries:
        row = rows.setdefault(entry.external_name,
                              {"name": entry.external_name, "kind": "entry"})
        if entry.alias:
            row["symbol"] = entry.name
            row["resolvedVia"] = "ALIAS"
        if entry.amode:
            row["amode"] = entry.amode
    if module.end_label:
        row = rows.get(module.end_label)
        if row is not None:
            row["entryPoint"] = True
    return [rows[name] for name in sorted(rows)]


def _programs(module: Module, artifacts: List[dict], excluded: List[dict]) -> None:
    """Modules this one needs: static-link references and dynamic invocations, merged.

    A module reached both ways - ``EXTRN`` plus a ``LOAD`` of the same name - is one
    artifact with both routes on it, not two rows that a reader has to notice are the same
    dependency.
    """
    rows: Dict[str, dict] = {}

    def row_for(name: str) -> dict:
        row = rows.get(name)
        if row is None:
            row = rows[name] = {
                "artifact": name,
                "kind": "program",
                "dependency": "runtime",
                "identity": "global",
                "resolvedBy": None,
                "needs": ("none - the load-module name is the estate-wide identity; which "
                          "library supplies it is decided by the SYSLIB concatenation at "
                          "bind time, not here"),
                "linkage": [],
                "touchedBy": [],
            }
        return row

    for ref in module.externals:
        if ref.via in LAYOUT_REFS:
            excluded.append({
                "name": ref.name, "kind": "external-dummy-section",
                "reason": ("a {0} names an external dummy section or PSECT - a storage "
                           "layout the binder fills in, not a module to call".format(
                               ref.via)),
            })
            continue
        row = row_for(ref.name)
        if ref.via not in row["linkage"]:
            row["linkage"].append(ref.via)
        row["evidence"] = EVIDENCE_LITERAL
        if ref.weak:
            # A WXTRN is not searched for in libraries and an unresolved one is not a
            # broken build. A distinct fact, not a weaker one.
            row["weak"] = True
            row["needs"] = ("nothing, necessarily - a weak external reference is left "
                            "unresolved unless another module in the same bind defines "
                            "it, so an absent target is not a broken build")
        if ref.literal_pool:
            row["literalPool"] = True
        row["touchedBy"].append(_touch(ref.line, ref.origin))
        _stamp_conditions(row, ref.conditions)

    for call in module.invocations:
        name = call.target
        if name is None:
            continue
        row = row_for(name)
        if call.verb not in row["linkage"]:
            row["linkage"].append(call.verb)
        row["evidence"] = _stronger(row.get("evidence"), call.evidence)
        if call.evidence != EVIDENCE_LITERAL and call.reason:
            row["evidenceNote"] = call.reason
        if call.candidates:
            row["candidates"] = sorted(set(call.candidates))
        if call.transfers:
            row["transfersControl"] = True
        if call.library:
            row["library"] = call.library
        if call.evidence == EVIDENCE_DYNAMIC:
            row["dynamic"] = True
        row["touchedBy"].append(_touch(call.line, call.origin))
        _stamp_conditions(row, call.conditions)

    for name in sorted(rows):
        row = rows[name]
        row["linkage"] = sorted(row["linkage"])
        owner = classify.subsystem(name)
        if owner is not None:
            excluded.append({"name": name, "kind": "program",
                             "reason": classify.reason(name)})
            continue
        artifacts.append(row)


_EVIDENCE_RANK = {EVIDENCE_LITERAL: 3, "assigned": 2, EVIDENCE_TABLE: 1,
                  EVIDENCE_DYNAMIC: 0}


def _stronger(current: Optional[str], candidate: str) -> str:
    """The best evidence any site gave for this name.

    A module both ``LOAD``ed by a literal and reached through a register is known: one site
    named it. Taking the weaker grading would report a knowable dependency as unknowable.
    """
    if current is None:
        return candidate
    return current if _EVIDENCE_RANK.get(current, 0) >= _EVIDENCE_RANK.get(candidate, 0) \
        else candidate


def _files(module: Module, artifacts: List[dict]) -> None:
    rows: Dict[str, dict] = {}
    for ref in module.files:
        if not ref.ddname:
            continue
        row = rows.get(ref.ddname)
        if row is None:
            row = rows[ref.ddname] = {
                "artifact": ref.ddname,
                "kind": "file",
                "dependency": "runtime",
                "io": ref.io,
                "ddname": ref.ddname,
                "identity": "program-local",
                "resolvedBy": "JCL DD statement",
                "needs": ("the JCL DD statement that binds this ddname to a dataset - the "
                          "assembler declares the ddname, the JCL says which dataset it "
                          "is"),
                "evidence": ref.evidence,
                "touchedBy": [],
            }
        row["io"] = _merge_io(row["io"], ref.io)
        row["evidence"] = _stronger(row.get("evidence"), ref.evidence)
        if ref.reason:
            row["evidenceNote"] = ref.reason
        if ref.evidence == EVIDENCE_DYNAMIC:
            row["dynamic"] = True
        if ref.dsorg:
            row["dsorg"] = ref.dsorg
        if ref.kind == "acb":
            row["vsam"] = True
        if ref.exits:
            row["exits"] = dict(sorted(ref.exits.items()))
        row["touchedBy"].append(_touch(ref.line, ref.origin))
        _stamp_conditions(row, ref.conditions)
    artifacts.extend(rows[name] for name in sorted(rows))


def _merge_io(current: str, incoming: str) -> str:
    """Same vocabulary the Easytrieve `file` row uses: read | write | read-write |
    unknown, so one consumer rule reads a `file` row from either package."""
    if current == incoming:
        return current
    known = {c for c in (current, incoming) if c != "unknown"}
    if not known:
        return "unknown"
    if len(known) == 1:
        return known.pop()
    return "read-write"


#: What each subsystem resource needs before its identity is estate-wide, and who says so.
_RESOURCE_IDENTITY = {
    "db2-table": ("global", "the DDL or DCLGEN that declares it",
                  "none - a qualified table name is the catalog-global identity; an "
                  "unqualified one is completed by the bind's QUALIFIER, not here. "
                  "Written under a Db2 ALIAS or SYNONYM the name is the alias - the "
                  "base table is catalog knowledge, supplied through --synonym-map / "
                  "--synonym-resolver, and reported as baseTable"),
    "terminal-map": ("global", "the BMS mapset the map belongs to",
                     "the BMS macro source (DFHMSD/DFHMDI/DFHMDF) - the mapset is a load "
                     "module, and its symbolic map is a separate copybook"),
    "cics-transaction": ("global", "the CICS TRANSACTION definition (CSD)",
                         "the CSD entry that says which program this transaction runs"),
    "queue": ("global", "the CICS TSMODEL or TDQUEUE definition (CSD)",
              "the CSD entry - a TD queue often has a real dataset behind it, and a TS "
              "queue may be shared across regions"),
    "file": ("program-local", "the CICS FILE definition (FCT/CSD)",
             "the CICS FILE definition that binds this name to a VSAM dataset - unlike a "
             "batch ddname, a CICS file is named in the CSD rather than in JCL"),
}

#: IMS resources this package records but does not put in the manifest. A PSB is a real
#: retrievable member and a segment is not a member at all, but neither kind is registered
#: in `mainframe_artifacts.fetch._KIND_TYPE` - so emitting them as artifacts would have
#: stage 2 report `skipped: no known retrieval type`, which is false for the PSB and
#: meaningless for the segment. They go in `excluded`, with the reason, until that repo
#: gains the entries.
_IMS_KINDS = {
    "psb": ("the PSB is named in the source, but resolving which DATABASES it opens needs "
            "the PSBGEN member - and `psb` is not yet a retrievable kind in "
            "mainframe_artifacts.fetch._KIND_TYPE"),
    "segment": ("a segment is a record type inside a database, not a retrievable member - "
                "which database it belongs to is in the PSBGEN and DBDGEN, not here"),
}


def _resources(module: Module, artifacts: List[dict], excluded: List[dict],
               synonyms=None) -> None:
    """CICS, Db2 and IMS resources, merged by (kind, name) the way programs are.

    ``synonyms`` is Db2 SYNONYM/ALIAS knowledge the caller holds
    (``mainframe_artifacts.synonyms.SynonymLookup``: a map, a host resolver, or both).
    Every ``db2-table`` row is asked of it, because a table name is this view's whole
    statement about the table, and a row written under a synonym gains ``baseTable``.
    The name as written stays the artifact - an assembler module naming a synonym is a
    fact about that module, the same way ``evidence`` records how a name was established
    rather than only its end state."""
    rows: Dict[tuple, dict] = {}
    for ref in module.resources:
        if ref.kind in _IMS_KINDS:
            excluded.append({"name": ref.name or "(not named)", "kind": ref.kind,
                             "reason": _IMS_KINDS[ref.kind]})
            continue
        if not ref.name:
            continue
        identity, resolved_by, needs = _RESOURCE_IDENTITY.get(
            ref.kind, ("global", None, "the definition that declares it"))
        key = (ref.kind, ref.name)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "artifact": ref.name,
                "kind": ref.kind,
                "dependency": "runtime",
                "identity": identity,
                "resolvedBy": resolved_by,
                "needs": needs,
                "subsystem": ref.subsystem,
                "evidence": ref.evidence,
                "touchedBy": [],
            }
            if ref.io is not None:
                row["io"] = ref.io
        if ref.io is not None:
            # `db2-table` follows the JCL side's `read+write`; every other kind follows the
            # Easytrieve `file` row's `read-write`. Two spellings of one idea in one output
            # family, kept per KIND so a consumer's rule is the same wherever the row came
            # from.
            row["io"] = (_merge_db2_io(row.get("io"), ref.io) if ref.kind == "db2-table"
                         else _merge_io(row.get("io", "unknown"), ref.io))
        row["evidence"] = _stronger(row.get("evidence"), ref.evidence)
        if ref.reason:
            row["evidenceNote"] = ref.reason
        if ref.evidence == EVIDENCE_DYNAMIC:
            row["dynamic"] = True
        row["touchedBy"].append({**_touch(ref.line, ref.origin), "verb": ref.verb})
        _stamp_conditions(row, ref.conditions)
    if synonyms is not None:
        for (kind, name), row in rows.items():
            if kind != "db2-table":
                continue
            hit = synonyms(name)
            if hit is not None:
                # A synonym's base is what the DDL declares and what cross-program
                # identity joins on; which door said so is provenance a reader may need.
                base, door = hit
                row["baseTable"] = base
                row["resolvedVia"] = ("synonym map" if door == FROM_MAP
                                      else "catalog resolver")
    artifacts.extend(rows[key] for key in sorted(rows))


def _merge_db2_io(current: Optional[str], incoming: str) -> str:
    """The JCL side spells a Db2 table's both-ways access `read+write`. Matched here so a
    `db2-table` row reads the same however the family produced it."""
    seen = {c for c in (current, incoming) if c and c != "unknown"}
    if {"read", "write"} <= {p for value in seen for p in value.split("+")}:
        return "read+write"
    return incoming if not current or current == "unknown" else current


def _members(module: Module, artifacts: List[dict],
             excluded: List[dict]) -> None:
    rows: Dict[tuple, dict] = {}
    for use in module.members:
        if use.kind == "macro" and classify.is_runtime(use.name):
            # SYS1.MACLIB, not the shop's source. Chasing it fills the retrieval
            # report with not-founds for GETMAIN and WTO, which buries the SITE macros
            # - the ones that actually hide this module's calls.
            excluded.append({"name": use.name, "kind": "macro",
                             "reason": classify.reason(use.name)})
            continue
        key = (use.kind, use.name)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "artifact": use.name,
                "kind": use.kind,
                "dependency": "compile-time",
                "identity": "library-member",
                "resolvedBy": "the SYSLIB concatenation of the assemble step",
                "needs": ("the member itself - it is assembled into this source, so "
                          "anything it declares is part of this module"),
                "status": "expanded" if use.resolved else "unresolved",
                "touchedBy": [],
            }
        if use.resolved:
            row["status"] = "expanded"
        if use.computed:
            row["computed"] = True
            row["needs"] = ("the member name is built by substitution, so which member is "
                            "copied is decided by the assemble step's SYSPARM= and is not "
                            "in this source")
        row["touchedBy"].append(_touch(use.line, use.origin))
        _stamp_conditions(row, use.conditions)
    artifacts.extend(rows[key] for key in sorted(rows, key=lambda k: (k[0], k[1])))


# -- the lineage view --------------------------------------------------------------

def build_asm_lineage(module: Module) -> dict:
    """Every site in source order, with the evidence for each, plus the unresolved set."""
    return {
        "format": FORMAT_LINEAGE,
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": module.name,
        "source": module.source_name,
        "note": _LINEAGE_NOTE,
        "provides": _provides(module),
        "sections": [_section_row(s) for s in module.sections],
        "storage": [_storage_row(d) for d in module.data],
        "calls": [_call_row(c) for c in module.invocations],
        "externals": [_external_row(e) for e in module.externals],
        "files": [_file_row(f) for f in module.files],
        "resources": [_resource_row(r) for r in module.resources],
        "members": [_member_row(m) for m in module.members],
        "unresolved": _unresolved(module),
        "notes": list(module.notes),
        "flags": list(module.flags),
    }


def _section_row(section) -> dict:
    row: Dict[str, Any] = {"name": section.name, "kind": section.kind,
                           "line": section.line}
    if section.alias:
        row["alias"] = section.alias
    if section.amode:
        row["amode"] = section.amode
    if section.rmode:
        row["rmode"] = section.rmode
    if section.origin:
        row["inMember"] = section.origin
    if not section.is_visible:
        row["externallyVisible"] = False
    return row


def _storage_row(item) -> dict:
    """One labelled DC/DS, as the parser recorded it. No offset and no length: nothing
    computes them, and deriving them means modelling duplication factors, type-implied
    lengths, alignment and ORG. ``operand`` is the declaration as written, so a zero-length
    ``DS 0H`` - often a code label rather than storage - reads as exactly that."""
    row: Dict[str, Any] = {"field": item.label, "line": item.line,
                           "operand": item.operand}
    if item.section:
        row["section"] = item.section
    if item.values:
        row["values"] = list(item.values)
    if item.origin:
        row["inMember"] = item.origin
    return row


def _call_row(call) -> dict:
    row: Dict[str, Any] = {"verb": call.verb, "line": call.line,
                           "target": call.target, "evidence": call.evidence}
    if call.reason:
        row["reason"] = call.reason
    if call.candidates:
        row["candidates"] = sorted(set(call.candidates))
    if call.transfers:
        row["transfersControl"] = True
    if call.library:
        row["library"] = call.library
    if call.list_form:
        # A list form is a DATA definition, not a transfer of control. Marked so a reader
        # counting call sites does not count it as one.
        row["listForm"] = True
    if call.list_label:
        row["parameterList"] = call.list_label
    if call.origin:
        row["inMember"] = call.origin
    _stamp_conditions(row, call.conditions)
    return row


def _external_row(ref) -> dict:
    row: Dict[str, Any] = {"name": ref.name, "via": ref.via, "line": ref.line}
    if ref.weak:
        row["weak"] = True
    if ref.literal_pool:
        row["literalPool"] = True
    if ref.is_layout:
        row["layoutOnly"] = True
    if ref.origin:
        row["inMember"] = ref.origin
    _stamp_conditions(row, ref.conditions)
    return row


def _file_row(ref) -> dict:
    row: Dict[str, Any] = {"ddname": ref.ddname, "kind": ref.kind, "line": ref.line,
                           "io": ref.io, "evidence": ref.evidence}
    if ref.label:
        row["label"] = ref.label
    if ref.dsorg:
        row["dsorg"] = ref.dsorg
    if ref.macrf:
        row["macrf"] = ref.macrf
    if ref.reason:
        row["reason"] = ref.reason
    if ref.exits:
        row["exits"] = dict(sorted(ref.exits.items()))
    if ref.origin:
        row["inMember"] = ref.origin
    _stamp_conditions(row, ref.conditions)
    return row


def _resource_row(ref) -> dict:
    row: Dict[str, Any] = {"name": ref.name, "kind": ref.kind, "line": ref.line,
                           "subsystem": ref.subsystem, "verb": ref.verb,
                           "evidence": ref.evidence}
    if ref.io is not None:
        row["io"] = ref.io
    if ref.reason:
        row["reason"] = ref.reason
    if ref.origin:
        row["inMember"] = ref.origin
    _stamp_conditions(row, ref.conditions)
    return row


def _member_row(use) -> dict:
    row: Dict[str, Any] = {"name": use.name, "kind": use.kind, "line": use.line,
                           "status": "expanded" if use.resolved else "unresolved"}
    if use.computed:
        row["computed"] = True
    if use.origin:
        row["inMember"] = use.origin
    _stamp_conditions(row, use.conditions)
    return row


def _unresolved(module: Module) -> List[dict]:
    """Every site whose target the source does not name.

    This list is the point of the package. A module that dispatches a hundred programs
    from a table has no literal dependencies at all, and a manifest that simply omits
    those sites describes it as depending on nothing.
    """
    rows: List[dict] = []
    for call in module.invocations:
        if call.evidence in (EVIDENCE_LITERAL, "assigned"):
            continue
        row: Dict[str, Any] = {
            "site": call.verb, "line": call.line,
            "evidence": call.evidence,
            "reason": call.reason or "the target is computed at run time",
        }
        if call.candidates:
            row["candidates"] = sorted(set(call.candidates))
        if call.library:
            row["library"] = call.library
        rows.append(row)
    for ref in module.files:
        if ref.evidence == EVIDENCE_LITERAL:
            continue
        rows.append({"site": ref.kind.upper(), "line": ref.line,
                     "evidence": ref.evidence,
                     "reason": ref.reason or "the ddname is set at run time"})
    for ref in module.resources:
        if ref.evidence == EVIDENCE_LITERAL:
            continue
        rows.append({"site": ref.verb or ref.kind, "line": ref.line,
                     "evidence": ref.evidence,
                     "kind": ref.kind,
                     "reason": ref.reason or "the name is a data area, not a literal"})
    for use in module.members:
        if use.resolved:
            continue
        rows.append({
            "site": "COPY" if use.kind == "copybook" else "macro", "line": use.line,
            "evidence": EVIDENCE_DYNAMIC if use.computed else "not-retrieved",
            "member": use.name,
            "reason": ("the member name is built by substitution, so which member is "
                       "copied is decided at assembly time"
                       if use.computed else
                       "the member was not retrieved, so anything it declares - record "
                       "layouts, DCBs, calls - is missing from this model"),
        })
    return rows


# -- the one place this package meets the JCL tool ---------------------------------

#: The JCL lineage view this binder reads. Checked before anything else, because feeding
#: it the ARTIFACTS view instead produces an empty binding that looks exactly like a
#: binding nobody attempted.
JCL_LINEAGE_FORMAT = "jcl-dependencies-lineage"


def jcl_steps_running(manifest: dict, jcl_lineage: dict) -> List[str]:
    """The JCL steps that run this module.

    Cleaner than the Easytrieve join, and for a reason worth stating: an Easytrieve step
    runs ``PGM=EZTPA00`` - the interpreter - so the step's program name cannot identify
    which program runs, and the join has to go through the SYSIN member. An assembler
    module IS the load module, so ``EXEC PGM=`` names it directly.

    The match is against the whole **provides** index rather than the module's own name.
    A load module is very often bound under an ``ENTRY`` name or an ``ALIAS`` rather than
    under the name of its first control section, and matching only the CSECT name would
    silently bind nothing for exactly those modules.
    """
    names = {str(manifest.get("program", "")).upper()}
    names |= {str(p.get("name", "")).upper() for p in manifest.get("provides", []) or []}
    names |= {str(p.get("symbol", "")).upper() for p in manifest.get("provides", []) or []}
    names.discard("")

    steps: List[str] = []
    for binding in jcl_lineage.get("ddBindings", []) or []:
        step = binding.get("step")
        if step and str(binding.get("program", "")).upper() in names \
                and step not in steps:
            steps.append(step)
    return steps


def bind_jcl_ddnames(manifest: dict, jcl_lineage: dict, *,
                     steps: Sequence[str] = ()) -> dict:
    """Resolve this module's ddnames against a JCL job's DD bindings.

    ``jcl_lineage`` is a ``jcl-dependencies-lineage`` dict - a plain dict, which is
    precisely what keeps this package from importing the JCL one. Returns a new manifest;
    the input is not mutated.

    Three honesty rules, the same three the rest of the family applies:

    * a ddname bound to DIFFERENT datasets across the chosen steps lists
      ``datasetCandidates`` rather than picking one;
    * an unmatched ddname is left exactly as it was, still saying it needs JCL;
    * when no step could be identified the binding is made on ddname alone and **says so**,
      because a job that runs several modules can mean several different things by
      ``SYSUT1``.

    A ddname this module could not read statically - the ``DCB`` with no ``DDNAME=`` - is
    not bound at all, and keeps its own reason. Binding it would attach a dataset to a
    file whose identity the module decides at run time.
    """
    import copy

    out = copy.deepcopy(manifest)
    flags: List[str] = out.setdefault("flags", [])

    if jcl_lineage.get("format") != JCL_LINEAGE_FORMAT:
        flags.append(
            "JCL binding was not attempted: the supplied view's 'format' is {0!r}, not "
            "{1!r}. The artifacts view will not do - its control-card rows are keyed on "
            "DSN alone, so two members of one library collapse into a single row.".format(
                jcl_lineage.get("format"), JCL_LINEAGE_FORMAT))
        out["jclBinding"] = {"job": None, "source": jcl_lineage.get("source"),
                             "steps": [], "basis": "not attempted", "boundFiles": 0}
        return out

    bindings = list(jcl_lineage.get("ddBindings", []) or [])
    chosen = [s.upper() for s in steps]
    basis = "steps named by the caller"
    if not chosen:
        found = jcl_steps_running(out, jcl_lineage)
        if found:
            chosen = [s.upper() for s in found]
            basis = "the JCL step(s) whose EXEC PGM= names this module or one of its entry points"
    if not chosen:
        basis = "ddname alone - no step could be identified as running this module"
        flags.append(
            "JCL binding was made on ddname alone: no step in {0} runs {1} or any name it "
            "provides. If the job runs more than one module, a ddname may have been bound "
            "from the wrong step - name the step explicitly to remove the doubt.".format(
                jcl_lineage.get("source", "the JCL"), out.get("program")))

    job = jcl_lineage.get("job") or jcl_lineage.get("source") or "<jcl>"
    by_ddname: Dict[str, List[dict]] = {}
    for binding in bindings:
        if chosen and str(binding.get("step", "")).upper() not in chosen:
            continue
        entry = {"job": job, "step": binding.get("step"),
                 "dataset": binding.get("dataset"), "io": binding.get("io")}
        for key in ("generation", "member", "conditions"):
            if binding.get(key):
                entry[key] = binding[key]
        by_ddname.setdefault(str(binding.get("ddname", "")).upper(), []).append(entry)

    matched = 0
    for row in out.get("artifacts", []) or []:
        if row.get("kind") != "file" or not row.get("ddname"):
            continue
        if row.get("evidence") == EVIDENCE_DYNAMIC:
            # The module decides this ddname at run time. Binding whatever DD happens to
            # share the label's name would attach a real dataset to a file whose identity
            # is not this statement's - a confident answer to a question still open.
            continue
        found = by_ddname.get(str(row["ddname"]).upper())
        if not found:
            continue
        matched += 1
        row["boundBy"] = found
        datasets = sorted({e["dataset"] for e in found if e.get("dataset")})
        if len(datasets) == 1:
            row["dataset"] = datasets[0]
            row["resolvedBy"] = "JCL DD statement: " + ", ".join(
                sorted({"{0}.{1}".format(e["job"], e["step"]) for e in found}))
            row["identity"] = "global"
            row.pop("needs", None)
        elif datasets:
            row["datasetCandidates"] = datasets
            flags.append(
                "file {0} (ddname {1}): bound to {2} different datasets across the "
                "supplied JCL - the same module runs against different data in different "
                "steps; 'boundBy' says which step uses which. Not collapsed.".format(
                    row["artifact"], row["ddname"], len(datasets)))

    out["jclBinding"] = {"job": job, "source": jcl_lineage.get("source"),
                         "steps": chosen, "basis": basis, "boundFiles": matched}
    if matched:
        out["note"] = out.get("note", "") + (
            " File rows carrying 'dataset'/'boundBy' were resolved against the supplied "
            "JCL: the ddname -> DSN binding is closed, and 'boundBy' names the step that "
            "closed it. A ddname this module sets at run time is deliberately left "
            "unbound.")
    return out


# -- the dependents view -----------------------------------------------------------
#
# The only view here whose facts do not come from the source. Everything else in this
# file is a reading of the module in hand; these rows are the estate's answer to a
# question the module cannot answer about itself, so the view says who supplied them and
# never merges them into the other two.

_DEPENDENTS_NOTE = (
    "What the ESTATE says depends on this module - the reverse of every other view here, "
    "and the half a module's own source cannot contain. Supplied by the host through "
    "--dependents-map or --dependents-resolver and reported as given: 'suppliedBy' says "
    "which door answered. Rows attach to the ENTRY POINT a dependent named, not to the "
    "module, because a call to an entry point whose name differs from the member name is "
    "exactly the case that is otherwise unresolvable. 'matchStrength' says how well the "
    "host matched the name it was asked about, and is a field rather than prose because "
    "aggregating it into a sentence loses it. A capped answer carries 'truncated' with "
    "the true 'total', so a shortened list never reads as a complete one. 'unanswered' "
    "is the honest half: an entry point the lookup does not cover, or did not reach "
    "because it failed. Nothing depending on an entry point in 'unanswered' can be "
    "concluded - absent here means nobody said, and never that nothing depends on it."
)

def build_asm_dependents(module: Module, lookup) -> Optional[dict]:
    """What depends on this module, asked once per entry point it provides.

    ``None`` when no lookup was supplied - and that is the whole point of the return
    type. An empty answer would read as "nothing in the estate depends on this module",
    which is a claim about the estate that a run nobody told anything is not entitled to
    make. The CLI writes nothing at all in that case, so a run with no door produces
    exactly the files it always did.
    """
    if lookup is None or not lookup.supplied:
        return None

    entries: List[dict] = []
    unanswered: List[dict] = []
    provides = _provides(module) or (
        [{"name": module.name, "kind": "module"}] if module.name else [])
    for provided in provides:
        name = provided["name"]
        answer = lookup(name, "program")
        if answer is None:
            # Three different absences, kept apart: the lookup broke earlier in this run,
            # or it was asked and does not cover this name.
            unanswered.append({
                "name": name,
                "reason": ("the lookup failed earlier in this run and was not asked again"
                           if lookup.disabled_reason else
                           "the lookup does not cover this name"),
            })
            continue
        row = {"name": name, "kind": provided.get("kind", "entry"),
               # camelCased and sorted by the shared serializer: four views emitting
               # matchStrength three ways is the drift the shared vocabulary prevents.
               "dependents": output_rows(answer.rows),
               "count": len(answer.rows),
               "suppliedBy": answer.door}
        if answer.truncated:
            row["truncated"] = True
            if answer.total is not None:
                row["total"] = answer.total
        entries.append(row)

    flags = []
    if lookup.disabled_reason:
        flags.append(
            "dependents lookup failed mid-run ({0}); entry points it did not reach stay "
            "unanswered - fix the lookup and re-run".format(lookup.disabled_reason))
    if lookup.map_warning:
        flags.append(
            "part of the dependents map could not be read ({0}); the entries it did read "
            "answered normally".format(lookup.map_warning))

    return {
        "format": FORMAT_DEPENDENTS,
        "formatVersion": VIEW_SCHEMA_VERSION,
        "program": module.name,
        "source": module.source_name,
        "note": _DEPENDENTS_NOTE,
        "suppliedBy": lookup.describe(),
        "entries": entries,
        "unanswered": unanswered,
        "flags": flags,
    }
