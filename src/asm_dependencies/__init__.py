"""Parse IBM mainframe assembler (HLASM) and recover what a module depends on.

The COBOL says what a program does and the JCL says which dataset a ddname is. Assembler
sits under both: it is where ddnames are *declared* (``DCB DDNAME=``), where the estate's
utility subroutines live, and where a module states what it can be called **as**
(``CSECT``, ``ENTRY``) - the half of the call graph no other language in the family emits.

What makes it a different problem from its siblings is that an assembler module's
dependency set is **not determined by its source**. ``COPY`` and macro calls can sit inside
``AIF`` branches gated on ``&SYSPARM``, which arrives as ``PARM=`` on the assemble step;
site macros hide the real ``LINK``/``XCTL``/``LOAD``; and ``EPLOC=(R1)`` names a register
rather than a program. So every row says how it was established, and every site whose
answer is decided at run time is reported rather than dropped.

Peers, not layers: this package imports nothing from ``cobol_xstate``, ``jcl_dependencies``
or ``eztrieve_dependencies``. They meet at plain manifest dicts, and
``tests/test_boundaries.py`` enforces it.
"""

#: The logger root every module in this package writes under. The CLI hands it to
#: configure_logging alongside the core one - a root nobody configures propagates to
#: the root logger, which would end -qq's silence.
PACKAGE_LOGGER = "asm_dependencies"

#: The name this distribution publishes under. It names both views' ``format`` and, passed
#: down to ``mainframe_artifacts``, the two shared retrieval reports - which hardcoded
#: "cobol-xstate" for every front-end until upstream ledger batch 10, item 30c.
PRODUCER = "asm-dependencies"

#: Bumped when a published view's shape changes in a way a consumer must notice. Additive
#: keys do NOT bump it; a removed or re-meaning key does.
#:
#: Starts at 3, not 1, and family-wide: cobol-xstate's lineage view had been publishing
#: ``formatVersion: 2`` on its own since conditions moved into interned pools, so 1 would
#: have taken a published number BACKWARDS - the exact silent shape change this key exists
#: to prevent. One number across the family keeps a consumer's rule the same everywhere.
VIEW_SCHEMA_VERSION = 3

from .lexer import DEFAULT_MARGINS, Margins, logical_lines
from .macros import MacroExpander
from .model import (
    EVIDENCE_ASSIGNED, EVIDENCE_DYNAMIC, EVIDENCE_LITERAL, EVIDENCE_TABLE, Module,
)
from .parser import default_program_name, parse_asm

#: The version of the plain-dict contract ``bind_jcl_ddnames`` reads. The JCL side is a
#: peer, so nothing is imported across - this integer is what a skewed pair is caught by,
#: because an unbound manifest looks exactly like one nobody tried to bind.
JCL_BINDING_API_VERSION = 1

__all__ = [
    "PACKAGE_LOGGER", "DEFAULT_MARGINS", "Margins", "logical_lines",
    "MacroExpander", "Module", "parse_asm", "default_program_name",
    "EVIDENCE_LITERAL", "EVIDENCE_ASSIGNED", "EVIDENCE_TABLE", "EVIDENCE_DYNAMIC",
    "JCL_BINDING_API_VERSION",
]
