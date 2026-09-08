"""The operation-code vocabulary: what is an assembler directive, what is a machine
instruction, and what is therefore probably a macro.

This exists so the parser can tell the difference between an operation it *chose* not to
model and one it has never heard of. That distinction is the whole point: an unrecognised
operation in assembler is almost always a **site macro** - shops wrap ``LINK``/``XCTL``/
``LOAD`` in local macros like ``@LINK PGM=XYZ`` - and a tool that knows only IBM macro
names finds a fraction of the real calls. So an unknown operation is a lead to chase
through the macro libraries, not a line to skip.

The machine-instruction set here is not exhaustive and does not need to be. It only has to
be good enough that ordinary arithmetic and branching is not mistaken for a macro call. An
instruction missing from it becomes a macro candidate, gets looked for in the libraries,
is not found, and is flagged - which is noisy but never wrong. The reverse error, listing
a macro name here, would make a real call silently invisible, so the rule when in doubt is
to leave a mnemonic OUT.
"""

from __future__ import annotations

from typing import FrozenSet

#: Directives that define, name or bound a control section.
SECTION_OPS = {
    "CSECT": "csect", "RSECT": "rsect", "START": "start", "COM": "com",
    "DSECT": "dsect", "DXD": "dxd",
}

#: IBM macros this package models itself. They must be listed, not left to the expander:
#: an unknown operation is treated as a site macro and looked for in the libraries, and
#: LINK/XCTL/DCB would then be fetched, missed, and flagged as holes - while their operands,
#: which are the dependencies, went unread.
IBM_MACROS = frozenset({
    # program management
    "LINK", "LINKX", "XCTL", "XCTLX", "ATTACH", "ATTACHX", "LOAD", "DELETE",
    "SYNCH", "SYNCHX", "CALL", "CEEFETCH",
    # data management
    "DCB", "DCBE", "ACB", "RPL", "EXLST", "OPEN", "CLOSE",
    # the preprocessors' language, embedded in the operand field
    "EXEC",
})

#: Directives the parser has a handler for. Anything here without a ``_do_`` method in the
#: parser is a bug - it would be silently unmodelled with no flag.
MODELLED = frozenset({
    "CSECT", "RSECT", "START", "COM", "DSECT", "DXD",
    "ENTRY", "EXTRN", "WXTRN", "ALIAS", "AMODE", "RMODE", "XATTR",
    "DC", "DS", "END", "COPY", "EQU", "CXD",
}) | IBM_MACROS

#: Directives with no bearing on what a module depends on: listing control, base-register
#: bookkeeping, alignment, and the conditional-assembly statements handled before the
#: parser ever sees them. Named rather than lumped in with machine instructions so that
#: "not modelled" stays a short, reviewable list.
IGNORED = frozenset({
    # listing and assembly control
    "TITLE", "EJECT", "SPACE", "PRINT", "PUSH", "POP", "ICTL", "ISEQ", "ACONTROL",
    "CNOP", "ORG", "LTORG", "USING", "DROP", "SYSSTATE", "SPLEVEL", "MNOTE",
    "CATTR", "EXITCTL", "ADATA", "OPSYN", "AINSERT", "AREAD", "RSECT@",
    # conditional assembly - conditional.py consumes these
    "AIF", "AGO", "ANOP", "ACTR", "SETA", "SETB", "SETC", "SETAF", "SETCF",
    "LCLA", "LCLB", "LCLC", "GBLA", "GBLB", "GBLC",
    # macro definition - macros.py consumes these
    "MACRO", "MEND", "MEXIT",
})

#: Instructions that write into storage. A store into a field a ``DC`` initialised is what
#: turns a knowable name into an unknowable one, so these are the decoy check.
STORE_OPS = frozenset({
    "MVC", "MVI", "MVCL", "MVCLE", "MVN", "MVZ", "MVO", "MVCIN", "MVCK", "MVCS", "MVCP",
    "ST", "STC", "STH", "STM", "STMY", "STCM", "STY", "STG", "STRV", "STOC",
    "XC", "NC", "OC", "TR", "TRT", "PACK", "UNPK", "ED", "EDMK",
    "ZAP", "AP", "SP", "MP", "DP", "SRP",
})

_ARITHMETIC = """
A AD ADR AE AER AFI AG AGF AGFI AGFR AGHI AGHIK AGR AGRK AH AHI AHIK AHY AL ALC ALCG
ALCGR ALCR ALFI ALG ALGF ALGFI ALGFR ALGHSIK ALGR ALGRK ALR ALRK ALSI ALSIH ALY AP AR
ARK AU AUR AW AWR AXR AY BAKR BAL BALR BAS BASR BASSM BC BCR BCT BCTG BCTGR BCTR BRAS
BRASL BRC BRCL BRCT BRCTG BRXH BRXLE BSM BXH BXLE C CD CDR CDS CDSG CDSY CE CER CFI CG
CGF CGFI CGFR CGH CGHI CGR CGRJ CGRL CH CHI CHY CIJ CL CLC CLCL CLCLE CLFI CLG CLGF
CLGFI CLGR CLGRJ CLI CLIJ CLM CLR CLRJ CLRL CLST CLY CP CPYA CR CRJ CRL CS CSG CSY CVB
CVBG CVD CVDG CY D DD DDR DE DER DP DR DSG DSGF DSGR E ED EDMK EX EXRL FLOGR IC ICM ICMH
ICMY IIHF IIHH IIHL IILF IILH IILL IPM L LA LAE LAM LARL LAY LB LBR LCDR LCER LCGR LCR
LD LDR LE LER LG LGB LGBR LGF LGFI LGFR LGH LGHI LGHR LGR LH LHI LHR LHY LLC LLCR LLGC
LLGCR LLGF LLGFR LLGH LLGHR LLGT LLGTR LLH LLHR LLIHF LLIHH LLIHL LLILF LLILH LLILL LM
LMG LMH LMY LNDR LNER LNGR LNR LPDR LPER LPGR LPR LPSW LPSWE LR LRA LRAG LRL LRV LRVG
LRVGR LRVH LRVR LT LTDR LTER LTG LTGF LTGR LTR LY M MC MD MDR ME MER MG MGHI MH MHI MHY
ML MLG MLGR MLR MP MR MS MSFI MSG MSGF MSGFI MSGFR MSGR MSR MSY MVCDK MVCSK MVPG MVST MXD
MXDR MXR N NC NG NGR NGRK NI NIHF NIHH NIHL NILF NILH NILL NOP NOPR NR NRK NY O OC OG OGR
OGRK OI OIHF OIHH OIHL OILF OILH OILL OR ORK OY PALB PC PLO POPCNT PR PT PTLB RISBG RLL
RLLG S SAC SACF SAM24 SAM31 SAM64 SAR SD SDR SE SER SG SGF SGFR SGR SGRK SH SHI SHY SL
SLA SLAG SLB SLBG SLBGR SLBR SLDA SLDL SLFI SLG SLGF SLGFI SLGFR SLGR SLGRK SLL SLLG
SLR SLRK SLY SP SPKA SPM SQD SQDR SQE SQER SR SRA SRAG SRDA SRDL SRK SRL SRLG SRP SRST
SSAR SSK SSM ST STAM STAP STC STCK STCKE STCKF STCM STCMH STCMY STCTL STCY STD STE STFLE
STG STH STHY STIDP STM STMG STMH STMY STNSM STOSM STPQ STPT STRAG STRV STRVG STRVH STURA
STY SU SUR SVC SW SWR SXR SY TAM TAR TB TBEGIN TEND TM TMH TMHH TMHL TML TMLH TMLL TMY
TP TPROT TR TRACE TRAP2 TRAP4 TRE TROO TROT TRT TRTO TRTR TRTT TS UNPK UNPKA UNPKU UPT X
XC XG XGR XGRK XI XIHF XILF XR XRK XY ZAP
"""

#: Extended branch mnemonics. They are ordinary ``BC``/``BRC`` masks, and they are how
#: nearly all assembler branching is actually written.
_BRANCHES = """
B BE BH BL BM BNE BNH BNL BNM BNO BNP BNZ BO BP BZ BR BER BHR BLR BMR BNER BNHR BNLR
BNMR BNOR BNPR BNZR BOR BPR BZR J JE JH JL JM JNE JNH JNL JNM JNO JNP JNZ JO JP JZ
JAS JASL JC JCT JXH JXLE JLE JLH JLL JLM JLNE JLNH JLNL JLNM JLNO JLNP JLNZ JLO JLP
JLU JLZ JLNOP
"""

#: Machine and extended mnemonics. Used only to decide that an operation is NOT a macro.
#: STORE_OPS is folded in deliberately: leaving it out made ``MVC`` an unknown operation,
#: so the expander went looking for a macro called MVC, and every store in the estate came
#: back as an unresolved dependency.
MACHINE: FrozenSet[str] = (frozenset(_ARITHMETIC.split())
                           | frozenset(_BRANCHES.split()) | STORE_OPS)

#: Everything the parser recognises without going looking for a macro definition.
KNOWN: FrozenSet[str] = MODELLED | IGNORED | MACHINE


def is_known(operation: str) -> bool:
    return operation.upper() in KNOWN


def is_store(operation: str) -> bool:
    return operation.upper() in STORE_OPS
