"""EXEC CICS, EXEC SQL and DL/I - the preprocessors' language in the operand field."""

from _source import asm, cont, module

from asm_dependencies.model import EVIDENCE_DYNAMIC, EVIDENCE_LITERAL
from asm_dependencies.parser import parse_asm
from asm_dependencies.views import build_asm_artifacts


def resources(mod):
    return {(r.kind, r.name): r for r in mod.resources}


def rows(mod):
    return {(r["kind"], r["artifact"]): r for r in build_asm_artifacts(mod)["artifacts"]}


# --- the lexing an EXEC command needs -------------------------------------------------

def test_an_exec_command_has_no_remarks_field():
    """Split the assembler's way, the operand is `CICS` and the command becomes a remark."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXEC", "CICS LINK PROGRAM('PAYCALC')"), ("", "END")))
    assert [c.target for c in mod.invocations] == ["PAYCALC"]


def test_a_cics_command_continues_at_column_two_not_sixteen():
    """CICS's rule, not the assembler's. Read from column 16, the first operand of every
    continuation written in the ordinary CICS style is silently dropped - and there is no
    END-EXEC for the loss to show up against."""
    src = "\n".join([
        asm("", "EXEC", "CICS LINK PROGRAM('PAYCALC')", cont=True),
        "   COMMAREA(WRKAREA)".ljust(71) + "X",
        "               LENGTH(=H'250')",
    ])
    mod = parse_asm(src)
    assert [c.target for c in mod.invocations] == ["PAYCALC"]


def test_exec_sql_continues_at_the_assembler_continue_column():
    src = "\n".join([
        asm("", "EXEC", "SQL SELECT CUSTNO", cont=True),
        cont("INTO :WSCUSTNO", more=True),
        cont("FROM PRODDB.CUSTOMER"),
    ])
    mod = parse_asm(src)
    assert ("db2-table", "PRODDB.CUSTOMER") in resources(mod)


# --- CICS ------------------------------------------------------------------------------

def test_link_and_xctl_name_the_program_they_reach():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "CICS LINK PROGRAM('PAYCALC') COMMAREA(WRK)"),
        ("", "EXEC", "CICS XCTL PROGRAM('PAYNEXT')"),
        ("", "END"),
    ))
    calls = {c.target: c for c in mod.invocations}
    assert set(calls) == {"PAYCALC", "PAYNEXT"}
    assert calls["PAYNEXT"].transfers is True
    assert calls["PAYCALC"].evidence == EVIDENCE_LITERAL


def test_quotes_are_the_evidence():
    """IBM states it in the command reference: use quotes for literal names, omit them for
    data area variables. So the quoting IS how well the name is known."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "CICS LINK PROGRAM(PGMNAME)"),
        ("", "END"),
    ))
    call = mod.invocations[0]
    assert (call.target, call.evidence) == (None, EVIDENCE_DYNAMIC)
    assert "data area" in call.reason


def test_send_map_without_a_mapset_defaults_the_mapset_to_the_map_name():
    """IBM: "the name given in the MAP option is assumed to be that of the mapset". The
    mapset is the load module, so reading only MAPSET() finds nothing for the common form."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXEC", "CICS SEND MAP('CUSTM01') ERASE"), ("", "END")))
    ref = resources(mod)[("terminal-map", "CUSTM01")]
    assert "no MAPSET was given" in ref.reason


def test_an_explicit_mapset_is_used_when_it_is_given():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "CICS SEND MAP('CUSTM01') MAPSET('CUSTSET')"),
        ("", "END"),
    ))
    assert ("terminal-map", "CUSTSET") in resources(mod)


def test_transactions_files_and_queues_each_get_their_kind():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "CICS START TRANSID('PAY1') INTERVAL(0)"),
        ("", "EXEC", "CICS READ FILE('CUSTMAST') INTO(REC) RIDFLD(KEY)"),
        ("", "EXEC", "CICS WRITEQ TS QUEUE('PAYQUEUE') FROM(REC)"),
        ("", "EXEC", "CICS RETURN TRANSID('PAY2')"),
        ("", "END"),
    ))
    kinds = {(r.kind, r.name) for r in mod.resources}
    assert kinds == {("cics-transaction", "PAY1"), ("cics-transaction", "PAY2"),
                     ("file", "CUSTMAST"), ("queue", "PAYQUEUE")}


def test_the_direction_comes_from_the_command():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "CICS READ FILE('CUSTMAST') INTO(REC)"),
        ("", "EXEC", "CICS REWRITE FILE('CUSTMAST') FROM(REC)"),
        ("", "EXEC", "CICS READQ TS QUEUE('LOGQ') INTO(REC)"),
        ("", "END"),
    ))
    view = rows(mod)
    assert view[("file", "CUSTMAST")]["io"] == "read-write"
    assert view[("queue", "LOGQ")]["io"] == "read"


def test_a_remote_target_says_it_is_in_another_region():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "CICS LINK PROGRAM('REMOTE') SYSID('CICB')"),
        ("", "END"),
    ))
    assert any("ANOTHER CICS region" in n for n in mod.notes)


def test_assign_names_no_dependency_and_says_why():
    """Every ASSIGN option is a receiver. A PROGRAM() built from one is the canonical
    unresolvable CICS dispatch, and it is worth saying so where it happens."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXEC", "CICS ASSIGN PROGRAM(WSPGM)"), ("", "END")))
    assert mod.resources == []
    assert mod.invocations == []
    assert any("names no dependency" in n for n in mod.notes)


def test_enq_identifies_its_resource_by_address_and_is_flagged():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXEC", "CICS ENQ RESOURCE(LOCKAREA) LENGTH(8)"),
        ("", "END")))
    assert any("not by a name" in f for f in mod.flags)


def test_an_unmodelled_cics_command_is_flagged_rather_than_skipped():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXEC", "CICS SPOOLWRITE TOKEN(TK)"), ("", "END")))
    assert any("SPOOLWRITE is not modelled" in f for f in mod.flags)


# --- Db2 -------------------------------------------------------------------------------

def test_static_sql_names_its_tables_and_which_way_the_data_moves():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "SQL SELECT A,B INTO :X,:Y FROM PRODDB.CUSTOMER WHERE K = :Z"),
        ("", "EXEC", "SQL INSERT INTO PRODDB.ORDERS VALUES (:A,:B)"),
        ("", "EXEC", "SQL UPDATE PRODDB.CUSTOMER SET BAL = :B"),
        ("", "EXEC", "SQL DELETE FROM PRODDB.AUDIT WHERE K = :Z"),
        ("", "END"),
    ))
    view = rows(mod)
    assert view[("db2-table", "PRODDB.ORDERS")]["io"] == "write"
    assert view[("db2-table", "PRODDB.AUDIT")]["io"] == "write"
    # Read by the SELECT and written by the UPDATE: the JCL side's spelling, kept per kind.
    assert view[("db2-table", "PRODDB.CUSTOMER")]["io"] == "read+write"


def test_a_declared_cursor_names_its_table():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "SQL DECLARE C1 CURSOR FOR SELECT A FROM PRODDB.LEDGER"),
        ("", "END"),
    ))
    assert ("db2-table", "PRODDB.LEDGER") in resources(mod)


def test_dynamic_sql_hides_the_table_and_the_module_says_so():
    """The statement text is in a host variable. The table list is INCOMPLETE, and a
    manifest that does not say so is claiming a completeness it does not have."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "SQL PREPARE S1 FROM :STMTBUF"),
        ("", "END"),
    ))
    assert any("builds SQL at run time" in f and "INCOMPLETE" in f for f in mod.flags)


def test_an_sql_include_is_a_copybook_but_sqlca_is_not():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "SQL INCLUDE SQLCA"),
        ("", "EXEC", "SQL INCLUDE CUSTDCL"),
        ("", "END"),
    ))
    assert [m.name for m in mod.members] == ["CUSTDCL"]


def test_the_db2_language_interface_is_not_chased_as_application_source():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "L", "R15,=V(DSNHLI)"), ("", "END")))
    view = build_asm_artifacts(mod)
    assert view["artifacts"] == []
    assert "Db2" in view["excluded"][0]["reason"]


# --- IMS --------------------------------------------------------------------------------

def test_a_dli_call_is_recognised_and_its_interface_is_not_chased():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "CALL", "ASMTDLI,(GUFUNC,DBPCB,IOAREA,SSANAME),VL"),
        ("", "END"),
    ))
    view = build_asm_artifacts(mod)
    assert view["artifacts"] == []
    assert "IMS" in view["excluded"][0]["reason"]


def test_exec_dli_schd_is_the_one_place_a_psb_name_is_in_the_source():
    """Under ASMTDLI the PCBs are positional and the module cannot know which database PCB
    2 is. EXEC DLI SCHD is the exception, and it is worth taking."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXEC", "DLI SCHD PSB('PAYPSB')"), ("", "END")))
    assert ("psb", "PAYPSB") in resources(mod)
    view = build_asm_artifacts(mod)
    excluded = {e["name"]: e["reason"] for e in view["excluded"]}
    assert "PSBGEN" in excluded["PAYPSB"]


def test_an_ims_segment_is_reported_as_not_retrievable_rather_than_as_an_artifact():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "EXEC", "DLI GU SEGMENT('CUSTOMER') INTO(IOAREA)"),
        ("", "END"),
    ))
    view = build_asm_artifacts(mod)
    assert view["artifacts"] == []
    excluded = {e["name"]: e["reason"] for e in view["excluded"]}
    assert "not a retrievable member" in excluded["CUSTOMER"]
