"""Calls, loads and ddnames - and how well each name is actually known."""

from _source import module

from asm_dependencies.model import (
    EVIDENCE_ASSIGNED, EVIDENCE_DYNAMIC, EVIDENCE_LITERAL, EVIDENCE_TABLE,
)
from asm_dependencies.parser import parse_asm


def calls(mod):
    return {c.verb: c for c in mod.invocations}


def only(mod, verb="LINK"):
    return [c for c in mod.invocations if c.verb == verb][0]


# --- how the target is established -----------------------------------------------

def test_ep_names_the_module_in_the_statement():
    mod = parse_asm(module(("P", "CSECT"), ("", "LINK", "EP=PAYCALC"), ("", "END")))
    call = only(mod)
    assert (call.target, call.evidence) == ("PAYCALC", EVIDENCE_LITERAL)


def test_eploc_naming_a_field_is_resolved_to_what_the_field_holds():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LINK", "EPLOC=PGMNAME"),
        ("PGMNAME", "DC", "CL8'PAYCALC '"),
        ("", "END"),
    ))
    call = only(mod)
    assert (call.target, call.evidence) == ("PAYCALC", EVIDENCE_ASSIGNED)
    assert "nothing in this module stores over" in call.reason


def test_a_store_into_the_field_demotes_the_literal_to_a_candidate():
    """The decoy. The DC still says PAYCALC and is no longer what the field holds - so a
    tool that reports it confidently is confidently wrong, which is worse than silent."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "MVC", "PGMNAME,SELECTED"),
        ("", "LINK", "EPLOC=PGMNAME"),
        ("PGMNAME", "DC", "CL8'PAYCALC '"),
        ("", "END"),
    ))
    call = only(mod)
    assert call.target is None
    assert call.evidence == EVIDENCE_TABLE
    assert call.candidates == ["PAYCALC"]
    assert "MVC into it" in call.reason


def test_eploc_from_a_register_names_nothing_and_says_why():
    mod = parse_asm(module(("P", "CSECT"), ("", "LINK", "EPLOC=(R1)"), ("", "END")))
    call = only(mod)
    assert (call.target, call.evidence) == (None, EVIDENCE_DYNAMIC)
    assert "from a register" in call.reason


def test_a_dispatch_table_yields_its_candidate_set():
    """The SET is recoverable even though the choice is not. A module that dispatches a
    hundred programs from a table has no literal dependencies at all."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LINK", "EPLOC=PGMTAB"),
        ("PGMTAB", "DC", "CL8'PAY001',CL8'PAY002',CL8'PAY003'"),
        ("", "END"),
    ))
    call = only(mod)
    assert call.evidence == EVIDENCE_TABLE
    assert call.candidates == ["PAY001", "PAY002", "PAY003"]
    assert call.target is None


def test_a_field_that_is_never_initialised_says_so():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LINK", "EPLOC=PGMNAME"),
        ("PGMNAME", "DS", "CL8"),
        ("", "END"),
    ))
    assert "never initialised" in only(mod).reason


# --- the verbs ---------------------------------------------------------------------

def test_xctl_is_read_past_its_leading_register_list_and_is_a_transfer():
    """`XCTL (2,12),EP=NEXTPGM` - a regex anchored on `XCTL EP=` finds nothing here, and
    XCTL does not return, so it is a different edge from LINK."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "XCTL", "(2,12),EP=NEXTPGM"), ("", "END")))
    call = only(mod, "XCTL")
    assert (call.target, call.evidence, call.transfers) == (
        "NEXTPGM", EVIDENCE_LITERAL, True)


def test_call_with_a_symbol_generates_an_external_reference():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "CALL", "PAYCALC,(PARM1),VL"), ("", "END")))
    assert only(mod, "CALL").target == "PAYCALC"
    assert [(e.name, e.via) for e in mod.externals] == [("PAYCALC", "CALL")]


def test_call_through_a_register_is_paired_with_the_load_that_fed_it():
    """LOAD EP=X / LR 15,0 / CALL (15) is one dependency, not a fact and a blank."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LOAD", "EP=CCNDRVR"),
        ("", "LR", "R15,R0"),
        ("", "CALL", "(15),(OPTIONS,DDNAMES),VL"),
        ("", "END"),
    ))
    call = only(mod, "CALL")
    assert (call.target, call.evidence) == ("CCNDRVR", EVIDENCE_ASSIGNED)
    assert "LOADs, at line" in call.reason


def test_two_loads_make_the_register_form_ambiguous_rather_than_wrong():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LOAD", "EP=DRIVER1"),
        ("", "LOAD", "EP=DRIVER2"),
        ("", "CALL", "(15)"),
        ("", "END"),
    ))
    call = only(mod, "CALL")
    assert call.target is None
    assert call.candidates == ["DRIVER1", "DRIVER2"]
    assert call.evidence == EVIDENCE_TABLE


def test_synch_takes_an_address_and_is_paired_with_the_load():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "LOAD", "EP=EXITRTN"), ("", "SYNCH", "(15)"), ("", "END")))
    assert only(mod, "SYNCH").target == "EXITRTN"


def test_the_list_and_execute_forms_are_joined():
    """The name is on the MF=L statement and the transfer is on the MF=E one, a whole
    control section apart. Read separately, one is a phantom call and the other a
    dependency with no site."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LINK", "SF=(E,LINKLST)"),
        ("LINKLST", "LINK", "EP=PAYCALC,SF=L"),
        ("", "END"),
    ))
    execute = [c for c in mod.invocations if not c.list_form][0]
    assert (execute.target, execute.evidence) == ("PAYCALC", EVIDENCE_ASSIGNED)
    assert "list form at line" in execute.reason


def test_an_execute_form_whose_list_is_overwritten_is_only_a_candidate():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "MVC", "LINKLST,NEWPARMS"),
        ("", "LINK", "SF=(E,LINKLST)"),
        ("LINKLST", "LINK", "EP=PAYCALC,SF=L"),
        ("", "END"),
    ))
    execute = [c for c in mod.invocations if not c.list_form][0]
    assert execute.target is None
    assert execute.candidates == ["PAYCALC"]


def test_delete_corroborates_a_name_a_dynamic_load_hid():
    """DELETE must name what LOAD named, from the same task - so it is independent
    evidence. Offered as a candidate, never as the target: a section may load several
    modules and delete only one."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LOAD", "EPLOC=(R1)"),
        ("", "DELETE", "EP=PAYCALC"),
        ("", "END"),
    ))
    load = only(mod, "LOAD")
    assert load.target is None
    assert load.candidates == ["PAYCALC"]
    assert "must have loaded" in load.reason


def test_the_library_operand_resolves_to_the_ddname_it_names():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LINK", "EP=PAYCALC,DCB=MYLIB"),
        ("MYLIB", "DCB", "DDNAME=PGMLIB,DSORG=PO,MACRF=(R)"),
        ("", "END"),
    ))
    assert only(mod).library == "PGMLIB"


# --- ddnames ------------------------------------------------------------------------

def test_a_dcb_declares_a_ddname():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("CUSTDCB", "DCB", "DDNAME=CUSTMAST,DSORG=PS,MACRF=(GM),EODAD=EOF"),
        ("", "END"),
    ))
    ref = mod.files[0]
    assert (ref.ddname, ref.io, ref.evidence) == ("CUSTMAST", "read", EVIDENCE_LITERAL)
    assert ref.exits == {"EODAD": "EOF"}


def test_a_dcb_with_no_ddname_is_dynamic_because_it_is_moved_in_before_open():
    mod = parse_asm(module(
        ("P", "CSECT"), ("MYDCB", "DCB", "DSORG=PS,MACRF=(GM)"), ("", "END")))
    ref = mod.files[0]
    assert ref.evidence == EVIDENCE_DYNAMIC
    assert "moved in before OPEN" in ref.reason


def test_a_vsam_acb_with_no_ddname_takes_the_ddname_from_its_label():
    """Scanning only for DDNAME= misses the ddname of every ACB written this way."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("CUSTFILE", "ACB", "AM=VSAM,MACRF=(KEY,DIR,IN)"), ("", "END")))
    ref = mod.files[0]
    assert (ref.ddname, ref.evidence, ref.io) == ("CUSTFILE", EVIDENCE_LITERAL, "read")
    assert "the label on the ACB statement" in ref.reason


def test_a_vtam_acb_is_not_a_file():
    """Two entirely different macros share the name ACB. Reading a VTAM one as VSAM
    invents a file that does not exist."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("MYACB", "ACB", "AM=VTAM,APPLID=MYAPPL"), ("", "END")))
    assert mod.files == []
    assert any("VTAM application control block" in n for n in mod.notes)


def test_open_supplies_the_direction_the_dcb_did_not():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("OUTDCB", "DCB", "DDNAME=REPORT,DSORG=PS"),
        ("", "OPEN", "(OUTDCB,OUTPUT)"),
        ("", "END"),
    ))
    assert mod.files[0].io == "write"
    assert mod.files[0].opened is True


def test_open_through_a_register_says_which_file_is_unknown():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("MYDCB", "DCB", "DDNAME=CUSTMAST,DSORG=PS"),
        ("", "OPEN", "((R5),INPUT)"),
        ("", "END"),
    ))
    assert any("address in a register" in f for f in mod.flags)


def test_macrf_gives_direction_for_read_and_write():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("IN", "DCB", "DDNAME=INFILE,MACRF=(GM)"),
        ("OUT", "DCB", "DDNAME=OUTFILE,MACRF=(PM)"),
        ("UPD", "ACB", "DDNAME=VSAMFILE,MACRF=(KEY,DIR,IN,OUT)"),
        ("", "END"),
    ))
    assert [f.io for f in mod.files] == ["read", "write", "read-write"]


# --- IBM runtime --------------------------------------------------------------------

def test_an_ibm_runtime_entry_point_is_not_chased_as_application_source():
    from asm_dependencies.views import build_asm_artifacts
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "L", "R15,=V(DSNHLI)"),
        ("", "CALL", "ASMTDLI,(FUNC,PCB),VL"),
        ("", "DC", "V(PAYCALC)"),
        ("", "END"),
    ))
    view = build_asm_artifacts(mod)
    assert [r["artifact"] for r in view["artifacts"]] == ["PAYCALC"]
    excluded = {r["name"]: r["reason"] for r in view["excluded"]}
    assert "Db2" in excluded["DSNHLI"]
    assert "IMS" in excluded["ASMTDLI"]
