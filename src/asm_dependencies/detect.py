"""Is this member assembler?

Kept in this package rather than in ``mainframe_artifacts`` on purpose: knowing that
``CSECT`` in the operation field of a fixed-column line means an assembler control section
is assembler domain knowledge, and the shared package holds vocabulary, not judgement.

The answer is advisory. It is printed as a warning and never stops a run - a fragment, a
copy member or a macro is perfectly good input and none of them looks like a complete
module.
"""

from __future__ import annotations

import os
import re
from typing import Optional, Tuple

#: Operation codes that mean assembler and essentially nothing else. Deliberately short:
#: a longer list would start matching COBOL and JCL words and turn the check into noise.
_MARKERS = re.compile(
    r"^.{0,15}\s(CSECT|DSECT|RSECT|START|EXTRN|WXTRN|ENTRY|AMODE|RMODE|USING|DROP|LTORG"
    r"|MACRO|MEND|AIF|AGO|ANOP|ICTL)\s", re.I | re.M)

#: ``DC``/``DS`` with a type letter, and the V-con that is this language's signature.
_CONSTANTS = re.compile(r"^.{0,15}\s(DC|DS)\s+\d*[ABCDEFHLPQRSVXYZ]", re.I | re.M)
_VCON = re.compile(r"=?V\([A-Z@#$_][A-Z0-9@#$_]*\)", re.I)

_EXTENSIONS = (".asm", ".mlc", ".hlasm", ".alc", ".bal", ".mac", ".s")

#: Things that mean it is one of the SIBLING languages, so the answer is a confident no
#: rather than an unsure one.
_NOT_ASM = re.compile(
    r"^(//[A-Z0-9#@$]{0,8}\s+(JOB|EXEC|DD|PROC)\b"      # JCL
    r"|\s*IDENTIFICATION\s+DIVISION"                     # COBOL
    r"|\s*PROGRAM-ID\b"
    r"|\s*FILE\s+[A-Z0-9#@$-]+\s+(FB|VB|F|V|VS)\b)",     # Easytrieve
    re.I | re.M)


def classify(source_name: str, source: str) -> Tuple[bool, Optional[str]]:
    """``(looks like assembler, why not)``.

    ``why not`` is prose for a warning, and is ``None`` when the answer is yes. A member
    that is merely inconclusive - a short macro, a copy member of pure ``DS`` statements -
    gets a "cannot tell" reason rather than a denial, because being wrong in the confident
    direction would talk a user out of a run that would have worked.
    """
    other = _NOT_ASM.search(source or "")
    if other:
        return False, ("this looks like {0} rather than assembler - its first "
                       "structural line is {1!r}".format(
                           _which(other.group(0)), other.group(0).strip()[:40]))
    if _MARKERS.search(source or ""):
        return True, None
    if _CONSTANTS.search(source or "") or _VCON.search(source or ""):
        return True, None
    if os.path.splitext(source_name or "")[1].lower() in _EXTENSIONS:
        return True, None
    return False, ("no CSECT, DSECT, EXTRN, macro or DC/DS statement was found in the "
                   "assembler statement columns, so this may not be assembler - or may be "
                   "a fragment, which is fine")


def _which(text: str) -> str:
    head = text.strip().upper()
    if head.startswith("//"):
        return "JCL"
    if head.startswith("FILE"):
        return "Easytrieve"
    return "COBOL"


def looks_like_asm(source_name: str, source: str) -> bool:
    return classify(source_name, source)[0]
