"""Resolving the names that are in the module but not in the statement.

Without this pass, every ``EPLOC=``, ``DE=``, ``MF=(E,...)`` and ``SYNCH`` site is
``dynamic``, and on real assembler that is most of them - which makes the tool technically
honest and practically useless. With it, the common shape resolves::

             LINK  EPLOC=PGMNAME
    PGMNAME  DC    CL8'PAYCALC '

The reason this is a separate pass and not a parser rule is the **decoy**. Add one line::

             MVC   PGMNAME,SELECTED
             LINK  EPLOC=PGMNAME
    PGMNAME  DC    CL8'PAYCALC '

and the literal is still right there, still says ``PAYCALC``, and is no longer what the
field holds when the call is made. A tool that reports ``PAYCALC`` here is not merely
incomplete - it is confidently wrong, which is worse, because nothing about the output
invites a second look. So a store into a field demotes it back to ``dynamic`` with a
reason that names the store.

Everything here upgrades or downgrades ``evidence``. Nothing here invents a target: where
the source does not decide, the site stays unresolved and says why.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .model import (
    EVIDENCE_ASSIGNED, EVIDENCE_DYNAMIC, EVIDENCE_TABLE, Module,
)

#: Verbs whose register form is the second half of a LOAD, rather than a call to nothing.
_PAIRED_WITH_LOAD = ("SYNCH", "SYNCHX", "CALL")


def resolve(module: Module) -> None:
    """Upgrade what the module itself decides; leave the rest unresolved and explained."""
    data = {d.label: d for d in module.data}
    stores: Dict[str, List] = {}
    for store in module.stores:
        stores.setdefault(store.target, []).append(store)

    _fields(module, data, stores)
    _parameter_lists(module, stores)
    _pair_with_load(module)
    _libraries(module)
    _corroborate_with_delete(module)


def _name(value: str) -> Optional[str]:
    """An eight-byte name field, trimmed. Blank means the field says nothing."""
    trimmed = (value or "").strip()
    return trimmed.upper() if trimmed else None


def _fields(module: Module, data: Dict[str, object], stores: Dict[str, List]) -> None:
    for call in module.invocations:
        if call.via_field is None or call.target is not None:
            continue
        definition = data.get(call.via_field)
        written = stores.get(call.via_field, [])

        if definition is None:
            call.reason = (
                "{0} is not a labelled constant in this module, so what it holds at the "
                "call is not in this source".format(call.via_field))
            continue

        if written:
            # The field is written before use somewhere in the module. Even when every
            # store is itself a literal, WHICH one reaches this site is control flow this
            # pass deliberately does not simulate - so the set is reported, not a choice.
            literals = [_name(s.literal) for s in written if s.literal]
            candidates = [v for v in [_name(v) for v in definition.values] + literals if v]
            call.candidates = sorted(set(candidates))
            call.evidence = EVIDENCE_TABLE if call.candidates else EVIDENCE_DYNAMIC
            call.reason = (
                "{0} is initialised to {1!r} but {2} into it at line {3}, so the constant "
                "is not necessarily what the field holds here - the names listed are "
                "candidates, not the target".format(
                    call.via_field,
                    (definition.values or [""])[0],
                    ", ".join(sorted({s.operation for s in written})),
                    ", ".join(str(s.line) for s in written[:3])))
            continue

        values = [v for v in (_name(v) for v in definition.values) if v]
        if len(values) == 1:
            call.target = values[0]
            call.evidence = EVIDENCE_ASSIGNED
            call.reason = ("the name is in {0}, which a DC at line {1} initialises and "
                           "nothing in this module stores over".format(
                               call.via_field, definition.line))
        elif len(values) > 1:
            call.candidates = sorted(set(values))
            call.evidence = EVIDENCE_TABLE
            call.reason = (
                "{0} is a table of {1} names selected at run time - the set is known, the "
                "choice is not".format(call.via_field, len(values)))
        else:
            call.reason = ("{0} is reserved but never initialised in this module, so its "
                           "contents come from outside it".format(call.via_field))


def _parameter_lists(module: Module, stores: Dict[str, List]) -> None:
    """Join ``MF=(E,LINKLST)`` to the ``LINK ...,MF=L`` that named the module.

    They can be a whole control section apart. The list form is a data definition and
    calls nothing; the execute form transfers control and names nothing. Reported
    separately, one is a phantom call and the other a dependency with no site.
    """
    defined = {call.list_label: call for call in module.invocations
               if call.list_form and call.list_label}
    for call in module.invocations:
        if call.list_form or call.target is not None or not call.list_label:
            continue
        source = defined.get(call.list_label)
        if source is None:
            call.reason = call.reason or (
                "the parameter list {0} is not defined by a list form in this module, so "
                "the module it names is not in this source".format(call.list_label))
            continue
        if stores.get(call.list_label):
            call.reason = (
                "the parameter list {0} is written into before this execute form, so the "
                "name the list form set is not necessarily the one used here".format(
                    call.list_label))
            if source.target:
                call.candidates = sorted({source.target})
                call.evidence = EVIDENCE_TABLE
            continue
        if source.target:
            call.target = source.target
            call.evidence = EVIDENCE_ASSIGNED
            call.reason = ("the name is on the list form at line {0}; this execute form "
                           "is where control is transferred".format(source.line))
        elif source.candidates:
            call.candidates = list(source.candidates)
            call.evidence = EVIDENCE_TABLE
            call.reason = source.reason


def _pair_with_load(module: Module) -> None:
    """``LOAD EP=X`` / ``LR 15,0`` / ``CALL (15)`` is one dependency, not two half-facts.

    The register form names no module. What it reaches is whatever the preceding ``LOAD``
    put in the register - so when exactly one candidate ``LOAD`` precedes it in the same
    control section, the pair is stated. When several do, the choice is control flow this
    pass does not simulate, and the set is reported instead.
    """
    for index, call in enumerate(module.invocations):
        if call.verb not in _PAIRED_WITH_LOAD or call.target is not None:
            continue
        if call.verb == "CALL" and call.via_field is None and not call.reason:
            continue
        preceding = [c for c in module.invocations[:index]
                     if c.verb in ("LOAD", "CEEFETCH") and c.section == call.section]
        targets = sorted({c.target for c in preceding if c.target})
        if len(targets) == 1:
            call.target = targets[0]
            call.evidence = EVIDENCE_ASSIGNED
            call.reason = ("the register form names no module; it reaches the one this "
                           "control section LOADs, at line {0}".format(
                               next(c.line for c in preceding if c.target == targets[0])))
        elif len(targets) > 1:
            call.candidates = targets
            call.evidence = EVIDENCE_TABLE
            call.reason = ("the register form names no module, and this control section "
                           "LOADs {0} of them - which one reaches this site is control "
                           "flow, not text".format(len(targets)))


def _libraries(module: Module) -> None:
    """``DCB=MYLIB`` on a LINK names the DCB, and the DCB carries the ddname.

    The library is a real dependency and a different one from the module: it says *where*
    the module is fetched from, which for a site that loads from its own STEPLIB is the
    difference between a resolvable name and an unresolvable one.
    """
    ddnames = {f.label: f.ddname for f in module.files if f.label and f.ddname}
    for call in module.invocations:
        if call.library and call.library in ddnames:
            call.library = ddnames[call.library]


def _corroborate_with_delete(module: Module) -> None:
    """``DELETE`` must name what ``LOAD`` named, from the same task.

    So a literal ``DELETE EP=PAYCALC`` beside a dynamic ``LOAD EPLOC=(R1)`` is independent
    evidence of a name the LOAD hid. It is offered as a candidate and never as the target:
    a section may load several modules and delete only one.
    """
    deleted: Dict[Optional[str], List[str]] = {}
    for call in module.invocations:
        if call.verb == "DELETE" and call.target:
            deleted.setdefault(call.section, []).append(call.target)
    for call in module.invocations:
        if call.verb != "LOAD" or call.target is not None:
            continue
        names = sorted(set(deleted.get(call.section, [])))
        if not names:
            continue
        call.candidates = sorted(set(call.candidates) | set(names))
        call.evidence = _at_least_table(call.evidence)
        call.reason = "{0}; a DELETE in the same control section names {1}, which a " \
                      "LOAD in that section must have loaded".format(
                          call.reason or "the target is computed at run time",
                          ", ".join(names))


def _at_least_table(evidence: str) -> str:
    return EVIDENCE_TABLE if evidence == EVIDENCE_DYNAMIC else evidence
