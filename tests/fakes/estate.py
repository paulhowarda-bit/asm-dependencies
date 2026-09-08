"""A deterministic stand-in for the estate's artifact service (mf-fetch).

The real default client is ``mf_fetch:fetch_artifact`` - an external library that talks to
a mainframe share. Nothing here can reach it, so without a stand-in the retrieval reports
are untestable and the byte-stability ratchet could not cover them.

Answers from a fixed table, so a run is reproducible on any machine with no network. The
members cover what the assembler closure actually exercises:

* a **COPY member carrying a DSECT** whose fields exist in no other file;
* a **site macro that wraps LINK**, which is the whole reason this stage exists - without
  it the module that calls through it has no dependencies at all;
* a **macro that invokes another macro**, so the closure needs a second round to find it;
* a name the estate **does not have**;
* ``BOOM``, a name whose **request fails** - which is not the same fact as absence and
  must never be reported as one.
"""

from __future__ import annotations


def _asm(*rows):
    """Rows as ``(name, op, operand)``, laid out in the assembler's columns."""
    out = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
            continue
        name, op, operand = (list(row) + ["", "", ""])[:3]
        head = "{0}{1}".format(name.ljust(9), op)
        line = "{0} {1}".format(head, operand) if len(head) >= 15 else \
            "{0}{1}".format(head.ljust(15), operand)
        out.append(line.rstrip())
    return "\n".join(out) + "\n"


#: The record layout SITEDCB depends on. Its fields exist in no other member, so a parse
#: without it leaves the module with no storage definition at all.
CUSTREC = _asm(
    "* CUSTREC - the customer master record.",
    ("CUSTRECD", "DSECT"),
    ("CUSTNO", "DS", "CL6"),
    ("CUSTNAME", "DS", "CL30"),
    ("CUSTBAL", "DS", "PL8"),
    ("CUSTRECL", "EQU", "*-CUSTRECD"),
)

#: The shop's linkage macro. THIS is the member that matters: a module calling through
#: `@LINK` has, read literally, no dependencies at all.
SITELINK = _asm(
    "* SITELINK - the shop's standard linkage macro.",
    ("", "MACRO"),
    ("", "SITELINK", "&PGM=,&LIB="),
    ("", "L", "15,=V(&PGM)"),
    ("", "BALR", "14,15"),
    ("", "MEND"),
)

#: A macro that invokes another macro. The inner invocation is invisible until this
#: member's text is in hand, which is what forces a second round of the closure.
SITEIO = _asm(
    "* SITEIO - opens the standard files, and links through SITELINK.",
    ("", "MACRO"),
    ("", "SITEIO", "&DD="),
    ("", "COPY", "CUSTREC"),
    ("IOFILE", "DCB", "DDNAME=&DD,DSORG=PS,MACRF=(GM)"),
    ("", "SITELINK", "PGM=IOINIT"),
    ("", "MEND"),
)

TABLE = {
    "CUSTREC": CUSTREC,
    "SITELINK": SITELINK,
    "SITEIO": SITEIO,
}


class EstateRequestFailed(RuntimeError):
    """The request itself failed - credentials, connectivity, a service fault."""


def fetch_artifact(name, type=None, copy=None):        # noqa: A002 - the wire keyword
    """The mf-fetch calling convention: ``f(name, type=..., copy=...)``."""
    key = str(name).strip().strip("'\"").upper()
    if "(" in key:                                     # SRC.LIB(CUSTREC) -> the member
        key = key.split("(", 1)[1].rstrip(")")
    if key == "BOOM":
        raise EstateRequestFailed("estate share unreachable (simulated)")
    text = TABLE.get(key)
    if text is None:
        return {"artifact_name": key, "found": False}
    return {"artifact_name": key, "found": True, "text": text,
            "detected_type": "macro",
            "source_location": "PROD.ASMMAC({0})".format(key)}
