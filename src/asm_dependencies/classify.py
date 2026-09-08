"""Which names are IBM runtime services rather than application modules.

The retrieval stage must not go looking for source that does not exist. ``GETMAIN`` is a
supervisor service, ``DSNHLI`` is the Db2 language interface, ``ASMTDLI`` is the DL/I call
interface and ``DFHECALL`` is what the CICS translator generates - none of them is a member
in anybody's source library, and asking the estate for them produces a report full of
not-founds that say nothing.

So they are classified, not chased. ``mainframe_artifacts.categories.CATEGORY_IBM`` is
already in that package's ``NON_FETCHABLE`` set, so a row carrying it is skipped by stage 2
with a reason rather than requested and missed.

This is the *classifier*, and it lives here rather than in the shared package on purpose:
knowing that ``ASMTDLI`` is a DL/I entry point is assembler domain knowledge, while the
string ``"ibm-runtime"`` is shared vocabulary. Same split as the COBOL side.

The table doubles as a capability detector. A module that references ``DSNHLI`` talks to
Db2; one that references ``ASMTDLI`` talks to IMS. That is worth reporting even though
neither is a dependency to retrieve.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: Subsystem -> the entry points and modules that mean "this module talks to it".
_SUBSYSTEMS: Dict[str, Tuple[str, ...]] = {
    "Db2": (
        "DSNHLI", "DSNHLI2", "DSNALI", "DSNELI", "DSNRLI", "DSNCLI", "DSNULI",
        "DSNWLI", "DSNWLI2", "DSNTIAR", "DSNTIAC",
    ),
    "IMS": ("ASMTDLI", "CBLTDLI", "PLITDLI", "AIBTDLI", "CEETDLI", "DFSLI000"),
    "CICS": ("DFHECALL", "DFHEIENT", "DFHEIRET", "DFHEISTG", "DFHEIEND", "DFHEIGBL",
             "DFHEIBLK", "DFHREGS", "DFHEIPLR", "DFHAID", "DFHBMSCA"),
    "MQ": ("MQCONN", "MQCONNX", "MQOPEN", "MQCLOSE", "MQPUT", "MQPUT1", "MQGET",
           "MQDISC", "MQBEGIN", "MQCMIT", "MQBACK", "MQINQ", "MQSET", "MQSUB",
           "CSQBSTUB", "CSQCSTUB", "CSQQSTUB", "CSQASTUB"),
    "Language Environment": ("CEEENTRY", "CEETERM", "CEE3ABD", "CEEHDLR", "CEEMSG",
                             "CEECBLDY"),
    "TSO": ("IKJEFTSR", "IKJEFT01", "IKJEFT1A", "IKJEFT1B", "IKJTSOEV", "IRXJCL",
            "IRXEXEC"),
    "utility": ("IDCAMS", "IEBGENER", "IEBCOPY", "IEHLIST", "IEHPROGM", "IEFBR14",
                "SORT", "ICEMAN", "IERRCO00", "ICETOOL", "ADRDSSU"),
    # The SYS1.MACLIB macros every module uses and none of them owns. Named because a
    # module assembled without its macro libraries flags every one of these as a missing
    # dependency, and on real source that is most of the flags - drowning the site macros,
    # which are the ones that actually hide the shop's calls.
    "MVS": (
        # supervisor and task services
        "WTO", "WTOR", "WTL", "GETMAIN", "FREEMAIN", "STORAGE", "ABEND", "SNAP", "SNAPX",
        "TIME", "STIMER", "STIMERM", "TTIMER", "WAIT", "POST", "EVENTS", "ENQ", "DEQ",
        "RESERVE", "SPIE", "ESPIE", "ESTAE", "ESTAEX", "SETRP", "MODESET", "TESTAUTH",
        "SYSSTATE", "SPLEVEL", "DETACH", "IDENTIFY", "SAVE", "RETURN", "DELETE",
        # data management
        "GET", "PUT", "READ", "WRITE", "CHECK", "NOTE", "POINT", "BLDL", "FIND", "STOW",
        "RDJFCB", "DEVTYPE", "TRKCALC", "DYNALLOC", "GENCB", "MODCB", "SHOWCB", "TESTCB",
        "ENDREQ", "ERASE", "FREEPOOL", "BSP", "CNTRL",
        # register and mapping conveniences
        "YREGS", "EQUREGS", "IHASAVER", "DCBD", "IHADCB", "IHADCBE", "IEFZB4D0",
        "IEFJFCBN", "CVT", "IHAPSA", "IKJTCB", "IHASDWA",
    ),
}

#: Prefixes that are IBM's by convention. Narrow on purpose: ``DFH`` is CICS and ``DSN`` is
#: Db2 estate-wide, but a shop's own modules can begin with almost anything else, so a
#: broader guess would classify application source as runtime and stop retrieving it.
_PREFIXES: Dict[str, str] = {
    "DFH": "CICS",
    "CEE": "Language Environment",
    "IGZ": "Language Environment",
    "DSNT": "Db2",
    "DFS": "IMS",
}

_INDEX: Dict[str, str] = {}
for _subsystem, _names in _SUBSYSTEMS.items():
    for _name in _names:
        _INDEX[_name] = _subsystem


def subsystem(name: str) -> Optional[str]:
    """The IBM subsystem ``name`` belongs to, or ``None`` if it is not one of theirs.

    ``None`` is the honest default. A module this tool cannot place is an application
    module until the estate says otherwise - and the fetch stage's probe is what says
    otherwise, by retrieving it as COBOL or as assembler.
    """
    key = (name or "").upper()
    if key in _INDEX:
        return _INDEX[key]
    for prefix, owner in _PREFIXES.items():
        if key.startswith(prefix):
            return owner
    return None


def is_runtime(name: str) -> bool:
    return subsystem(name) is not None


def reason(name: str) -> str:
    """Why a row is not retrievable, in words the fetch report can print verbatim."""
    return ("provided by {0} - an IBM runtime entry point, so there is no application "
            "source to retrieve".format(subsystem(name)))
