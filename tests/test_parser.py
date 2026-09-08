"""What a module provides, and what it needs the binder to resolve."""

from _source import module

from asm_dependencies.model import VIA_EXTRN, VIA_QCON, VIA_VCON, VIA_WXTRN
from asm_dependencies.parser import default_program_name, parse_asm


def externals(mod):
    return {(e.name, e.via, e.weak) for e in mod.externals}


def names(mod):
    return {e.name for e in mod.externals}


# --- identity and the provides index ------------------------------------------------

def test_the_identity_is_the_first_control_section_not_the_file_name():
    """Unlike Easytrieve, assembler HAS a real identity. Falling back to the member name
    when a CSECT names the module would break the join to the JCL step that runs it."""
    mod = parse_asm(module(("PAYCALC", "CSECT"), ("", "END")), source_name="member.asm")
    assert mod.name == "PAYCALC"


def test_the_member_name_is_the_identity_only_when_no_section_names_one():
    mod = parse_asm(module(("", "DS", "CL8")), source_name="frag.asm")
    assert mod.name == "FRAG"


def test_default_program_name_strips_up_to_two_extensions():
    assert default_program_name("dir/PAYCALC.asm") == "PAYCALC"
    assert default_program_name("PAYCALC.asm.txt") == "PAYCALC"


def test_provides_is_the_union_of_section_names_and_entry_operands():
    """A CSECT name is an entry point without needing an ENTRY statement, so the provides
    index - the half of the call graph no sibling emits - is the union of the two."""
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("", "ENTRY", "PAYINIT,PAYTERM"),
        ("WORKAREA", "DSECT"),
        ("", "END"),
    ))
    assert mod.provides() == ["PAYCALC", "PAYINIT", "PAYTERM"]


def test_a_dsect_is_not_externally_visible():
    mod = parse_asm(module(("CUSTREC", "DSECT"), ("", "DS", "CL80"), ("", "END")))
    assert mod.provides() == []


def test_a_repeated_csect_resumes_rather_than_defining_a_second_one():
    """`CSECT` with a name already seen continues that section. Counting occurrences
    would multiply every control section by however often the source switched back."""
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("WORK", "DSECT"),
        ("PAYCALC", "CSECT"),
        ("", "END"),
    ))
    assert [s.name for s in mod.sections] == ["PAYCALC", "WORK"]


def test_a_sequence_symbol_in_the_name_field_starts_the_unnamed_section():
    mod = parse_asm(module((".RETRY", "CSECT"), ("", "END")), source_name="x.asm")
    assert mod.provides() == []
    assert mod.sections[0].name == ""


def test_amode_and_rmode_attach_to_their_section():
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("PAYCALC", "AMODE", "31"),
        ("PAYCALC", "RMODE", "ANY"),
        ("", "END"),
    ))
    assert (mod.sections[0].amode, mod.sections[0].rmode) == ("31", "ANY")


# --- address constants --------------------------------------------------------------

def test_a_v_type_constant_is_an_implicit_extrn():
    """`DC V(X)` declares X external with no EXTRN statement. It is the highest-confidence
    dependency signal assembler has."""
    mod = parse_asm(module(("PAYCALC", "CSECT"), ("VSUB", "DC", "V(SUBRTN)"), ("", "END")))
    assert externals(mod) == {("SUBRTN", VIA_VCON, False)}


def test_a_v_con_written_as_a_literal_is_the_same_dependency():
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("", "L", "R15,=V(SUBRTN)"),
        ("", "BALR", "R14,R15"),
        ("", "END"),
    ))
    ref = mod.externals[0]
    assert (ref.name, ref.via, ref.literal_pool) == ("SUBRTN", VIA_VCON, True)


def test_one_v_con_may_name_several_symbols():
    mod = parse_asm(module(("P", "CSECT"), ("", "DC", "V(SORT,MERGE,CALC)"), ("", "END")))
    assert names(mod) == {"SORT", "MERGE", "CALC"}


def test_an_a_type_constant_is_not_external_unless_the_symbol_is_declared():
    """`=A(X)` to a local label is an address, not a dependency. Treating every A-con as
    external would make every branch table in the estate a list of missing modules."""
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("", "L", "R15,=A(LOCAL)"),
        ("LOCAL", "DS", "F"),
        ("", "END"),
    ))
    assert externals(mod) == set()


def test_an_a_type_constant_becomes_external_when_an_extrn_declares_it():
    """The EXTRN may come after the use, which is why this is decided in a second pass."""
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("", "L", "R15,=A(SUBRTN)"),
        ("", "EXTRN", "SUBRTN"),
        ("", "END"),
    ))
    assert ("SUBRTN", VIA_EXTRN, False) in externals(mod)


def test_wxtrn_is_a_weak_edge_not_a_missing_one():
    """The binder does not search libraries for a WXTRN, and leaving it unresolved is not
    a broken build - so it is a different edge, not a lesser one."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "EXTRN", "HARDDEP"), ("", "WXTRN", "OPTDEP"), ("", "END")))
    assert externals(mod) == {("HARDDEP", VIA_EXTRN, False), ("OPTDEP", VIA_WXTRN, True)}


def test_a_q_con_is_recorded_as_a_layout_reference_not_a_call():
    mod = parse_asm(module(("P", "CSECT"), ("", "DC", "Q(MYDXD)"), ("", "END")))
    ref = mod.externals[0]
    assert (ref.name, ref.via, ref.is_layout) == ("MYDXD", VIA_QCON, True)


def test_a_v_con_naming_a_section_this_module_defines_is_not_an_external_dependency():
    """The binder resolves it inside the module. Reporting it would make every module that
    takes the address of its own entry point look like it depends on itself."""
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("", "DC", "V(PAYCALC)"),
        ("", "END"),
    ))
    assert externals(mod) == set()
    assert any("resolved internally" in n for n in mod.notes)


def test_alias_rewrites_the_name_the_binder_sees():
    """With ALIAS the name in the source is not the name another module binds to, so an
    unrewritten provides index joins to nothing."""
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("PAYCALC", "ALIAS", "C'payroll.calculate'"),
        ("", "END"),
    ))
    assert mod.provides() == ["payroll.calculate"]
    assert any("ALIAS renames" in n for n in mod.notes)


# --- data and stores -----------------------------------------------------------------

def test_a_labelled_character_constant_keeps_its_value():
    mod = parse_asm(module(("P", "CSECT"), ("PGMNAME", "DC", "CL8'PAYCALC '"), ("", "END")))
    assert mod.data[0].label == "PGMNAME"
    assert mod.data[0].value == "PAYCALC "


def test_a_table_of_constants_keeps_every_entry():
    """A dispatch table's candidate set is recoverable even when the choice is not."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("PGMTAB", "DC", "CL8'PAY001',CL8'PAY002',CL8'PAY003'"),
        ("", "END"),
    ))
    assert mod.data[0].values == ["PAY001  ", "PAY002  ", "PAY003  "]
    assert mod.data[0].value is None


def test_a_store_into_a_labelled_field_is_recorded():
    """The decoy check: a DC that is overwritten before use does not say what the field
    holds at the call, and a tool that reports it confidently is confidently wrong."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "MVC", "PGMNAME,SELECTED"),
        ("PGMNAME", "DC", "CL8'PAYCALC '"),
        ("", "END"),
    ))
    assert [(s.target, s.operation) for s in mod.stores] == [("PGMNAME", "MVC")]


def test_a_store_from_a_literal_keeps_the_value_it_sets():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "MVC", "PGMNAME,=CL8'OTHER'"), ("", "END")))
    assert mod.stores[0].literal == "OTHER"


def test_a_store_through_a_register_names_no_label():
    mod = parse_asm(module(("P", "CSECT"), ("", "MVC", "0(8,R5),SELECTED"), ("", "END")))
    assert mod.stores == []


# --- honesty --------------------------------------------------------------------------

def test_an_unknown_operation_is_reported_as_an_unresolved_macro():
    """In HLASM an unrecognised operation IS a macro call. Skipping it would hide every
    call a site macro wraps - which is how most large shops write assembler."""
    mod = parse_asm(module(("P", "CSECT"), ("", "@LINK", "PGM=PAYCALC"), ("", "END")))
    assert [(m.name, m.kind, m.resolved) for m in mod.members] == [
        ("@LINK", "macro", False)]
    assert any("@LINK" in f and "NOT in this manifest" in f for f in mod.flags)


def test_a_machine_instruction_is_not_mistaken_for_a_macro():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "STM", "R14,R12,12(R13)"),
        ("", "LR", "R12,R15"),
        ("", "BRAS", "R14,LOCAL"),
        ("", "END"),
    ))
    assert mod.members == []
    assert mod.flags == []


def test_the_end_operand_is_the_modules_entry_point():
    mod = parse_asm(module(("PAYCALC", "CSECT"), ("", "END", "PAYCALC")))
    assert mod.end_label == "PAYCALC"


def test_one_unparseable_statement_does_not_lose_the_rest():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "DC", "V(GOOD1)"),
        ("", "EXTRN", "((("),
        ("", "DC", "V(GOOD2)"),
        ("", "END"),
    ))
    assert names(mod) == {"GOOD1", "GOOD2"}
    assert any("(((" in f for f in mod.flags)
