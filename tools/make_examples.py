"""Write the example members with the fields in the columns the assembler reads them from.

Generated rather than hand-spaced so column 72 means column 72 - the fixtures for a column
language are worthless if the columns are approximate.
"""
import pathlib

EX = pathlib.Path("examples")


def line(name="", op="", operand="", remark="", cont=False, seq=""):
    head = "{0}{1}".format(name.ljust(9), op)
    body = "{0} {1}".format(head, operand) if len(head) >= 15 else \
        "{0}{1}".format(head.ljust(15), operand)
    if remark:
        body = "{0}  {1}".format(body, remark)
    if not cont and not seq:
        return body.rstrip()
    return body.ljust(71)[:71] + ("X" if cont else " ") + seq


def comment(text):
    return "*" + (" " + text if text else "")


def write(name, rows):
    text = "\n".join(r if isinstance(r, str) else line(*r) for r in rows) + "\n"
    (EX / name).write_text(text, encoding="utf-8", newline="\n")
    print("wrote", EX / name, len(text), "bytes")


# --- PAYCALC: the main example -----------------------------------------------------
write("paycalc.asm", [
    comment("PAYCALC - payroll calculation driver."),
    comment("Every way this tool can learn a name, and every way it cannot."),
    comment(""),
    ("PAYCALC", "CSECT"),
    ("PAYCALC", "AMODE", "31"),
    ("PAYCALC", "RMODE", "ANY"),
    ("", "ENTRY", "PAYINIT"),
    ("", "STM", "14,12,12(13)", "SAVE THE CALLER'S REGISTERS"),
    ("", "LR", "12,15"),
    ("", "USING", "PAYCALC,12"),
    comment(""),
    comment("Record layouts and the site linkage macro both arrive by COPY."),
    ("", "COPY", "PAYREC"),
    comment(""),
    comment("DC V() is an implicit EXTRN - the strongest signal there is."),
    ("VALIDATE", "DC", "V(PAYVALD)"),
    ("", "L", "15,=V(PAYEDIT)", "THE SAME DEPENDENCY, FROM THE POOL"),
    ("", "BALR", "14,15"),
    ("", "EXTRN", "PAYAUDIT"),
    ("", "WXTRN", "PAYSTATS", "OPTIONAL - MAY BIND TO ZERO"),
    ("", "L", "15,=A(PAYAUDIT)"),
    ("", "BASR", "14,15"),
    comment(""),
    comment("A site macro. Read literally this line calls nothing at all."),
    ("", "SITELINK", "PGM=PAYPOST"),
    comment(""),
    comment("EP= names the module here on the page."),
    ("", "LINK", "EP=PAYPRNT"),
    comment(""),
    comment("EPLOC= names a FIELD. Resolvable, because nothing stores over it."),
    ("", "LINK", "EPLOC=PGMNAME"),
    comment(""),
    comment("EPLOC=(R1) names a register. This one is not in the source at all."),
    ("", "LINK", "EPLOC=(1)"),
    comment(""),
    comment("A dispatch table: the SET of targets is known, the choice is not."),
    ("", "LA", "1,PGMTAB"),
    ("", "LINK", "EPLOC=PGMTAB"),
    comment(""),
    comment("LOAD then CALL by register: one dependency, not two half-facts."),
    ("", "LOAD", "EP=PAYSORT"),
    ("", "LR", "15,0"),
    ("", "CALL", "(15),(PARMLIST),VL"),
    ("", "DELETE", "EP=PAYSORT"),
    comment(""),
    comment("XCTL transfers and never returns. Note the leading register list."),
    ("", "XCTL", "(2,12),EP=PAYNEXT"),
    comment(""),
    ("", "CLOSE", "(CUSTDCB)"),
    ("", "L", "13,4(,13)"),
    ("", "LM", "14,12,12(13)"),
    ("", "SR", "15,15"),
    ("", "BR", "14"),
    comment(""),
    ("PAYINIT", "DS", "0H", "THE SECOND ENTRY POINT"),
    ("", "OPEN", "(CUSTDCB,INPUT,RPTDCB,OUTPUT)"),
    ("", "BR", "14"),
    comment(""),
    comment("The assembler is where ddnames are DECLARED."),
    ("CUSTDCB", "DCB", "DDNAME=CUSTMAST,DSORG=PS,MACRF=(GM),EODAD=ATEOF"),
    ("RPTDCB", "DCB", "DDNAME=PAYRPT,DSORG=PS,MACRF=(PM)"),
    comment("A VSAM ACB with no DDNAME= takes the ddname from its own label."),
    ("PAYHIST", "ACB", "AM=VSAM,MACRF=(KEY,DIR,IN,OUT)"),
    comment(""),
    ("PGMNAME", "DC", "CL8'PAYCOMP'"),
    ("PGMTAB", "DC", "CL8'PAY001',CL8'PAY002',CL8'PAY003'"),
    ("PARMLIST", "DC", "A(0)"),
    ("ATEOF", "DS", "0H"),
    ("", "BR", "14"),
    ("", "LTORG"),
    ("", "END", "PAYCALC"),
])

# --- PAYREC: a COPY member that carries the site macro AND a record layout ---------
write("payrec.cpy", [
    comment("PAYREC - record layout plus the shop's linkage macro."),
    comment("Without this member PAYCALC has no PAYPOST dependency at all."),
    ("", "MACRO"),
    ("", "SITELINK", "&PGM=,&LIB="),
    ("", "L", "15,=V(&PGM)"),
    ("", "BALR", "14,15"),
    ("", "MEND"),
    comment(""),
    ("PAYRECD", "DSECT"),
    ("PAYEMPNO", "DS", "CL6"),
    ("PAYNAME", "DS", "CL30"),
    ("PAYGROSS", "DS", "PL4"),
    ("PAYRECL", "EQU", "*-PAYRECD"),
])

# --- SITEENV: the conditional-assembly example ------------------------------------
write("siteenv.asm", [
    comment("SITEENV - what this depends on is decided by the ASSEMBLE STEP."),
    comment("Run it twice: once plain, once with --sysparm PROD."),
    ("SITEENV", "CSECT"),
    ("", "STM", "14,12,12(13)"),
    ("&SITE", "SETC", "'&SYSPARM'"),
    ("", "AIF", "('&SITE' EQ 'PROD').PROD"),
    ("", "COPY", "TESTCFG"),
    ("", "L", "15,=V(TESTLOG)"),
    ("", "AGO", ".DONE"),
    (".PROD", "ANOP"),
    ("", "COPY", "PRODCFG"),
    ("", "L", "15,=V(PRODLOG)"),
    (".DONE", "ANOP"),
    ("", "BALR", "14,15"),
    ("", "LM", "14,12,12(13)"),
    ("", "BR", "14"),
    ("", "END", "SITEENV"),
])

write("prodcfg.cpy", [
    comment("PRODCFG - the production estate's bindings."),
    ("PRODDCB", "DCB", "DDNAME=PRODMAST,DSORG=PS,MACRF=(GM)"),
    ("", "DC", "V(PRODAUTH)"),
])

write("testcfg.cpy", [
    comment("TESTCFG - the test estate's bindings."),
    ("TESTDCB", "DCB", "DDNAME=TESTMAST,DSORG=PS,MACRF=(GM)"),
    ("", "DC", "V(TESTAUTH)"),
])

# --- DISPATCH: a module whose every call is dynamic --------------------------------
write("dispatch.asm", [
    comment("DISPATCH - table-driven. Read literally it depends on NOTHING,"),
    comment("which is the failure mode the unresolved inventory exists for."),
    ("DISPATCH", "CSECT"),
    ("", "STM", "14,12,12(13)"),
    ("", "LR", "12,15"),
    ("", "USING", "DISPATCH,12"),
    ("", "L", "3,0(,1)", "THE CALLER'S SELECTOR"),
    ("", "SLL", "3,3"),
    ("", "LA", "1,PGMTAB(3)"),
    ("", "LINK", "EPLOC=(1)", "TARGET COMPUTED AT RUN TIME"),
    comment(""),
    comment("A decoy: the DC below says PAYCALC and is not what the field holds."),
    ("", "MVC", "SELECTED,0(1)"),
    ("", "LINK", "EPLOC=SELECTED"),
    ("", "LM", "14,12,12(13)"),
    ("", "BR", "14"),
    ("PGMTAB", "DC", "CL8'DSPRTN01',CL8'DSPRTN02',CL8'DSPRTN03'"),
    ("SELECTED", "DC", "CL8'PAYCALC '"),
    ("", "END", "DISPATCH"),
])

# --- CUSTINQ: a CICS + Db2 transaction module -------------------------------------
write("custinq.asm", [
    comment("CUSTINQ - a CICS customer inquiry, with static and dynamic SQL."),
    comment("EXEC CICS and EXEC SQL are not assembler: they are a preprocessor's"),
    comment("language in the operand field, blank-separated and with no remarks."),
    ("CUSTINQ", "CSECT"),
    ("", "DFHEIENT", "CODEREG=(3),DATAREG=(13),EIBREG=(11)"),
    comment(""),
    comment("Quotes are the evidence: a literal name vs a data area."),
    ("", "EXEC", "CICS RECEIVE MAP('CUSTM01') MAPSET('CUSTSET')"),
    comment(""),
    ("", "EXEC", "SQL SELECT CUSTNAME,BALANCE"),
    ("", "EXEC", "SQL INTO :WSNAME,:WSBAL FROM PRODDB.CUSTOMER"),
    comment(""),
    ("", "EXEC", "CICS READ FILE('CUSTMAST') INTO(CUSTREC) RIDFLD(WSKEY)"),
    ("", "EXEC", "CICS WRITEQ TS QUEUE('CUSTLOG') FROM(CUSTREC)"),
    comment(""),
    comment("A literal target, and one whose name a run-time value decides."),
    ("", "EXEC", "CICS LINK PROGRAM('CUSTVAL') COMMAREA(CUSTREC)"),
    ("", "EXEC", "CICS ASSIGN PROGRAM(WSPGM)"),
    ("", "EXEC", "CICS XCTL PROGRAM(WSPGM)"),
    comment(""),
    ("", "EXEC", "CICS SEND MAP('CUSTM02')"),
    ("", "EXEC", "CICS RETURN TRANSID('CINQ')"),
    comment(""),
    ("WSPGM", "DS", "CL8"),
    ("WSKEY", "DS", "CL6"),
    ("CUSTREC", "DS", "CL80"),
    ("", "END", "CUSTINQ"),
])
