"""Which statements exist at all - and saying so when the source cannot decide."""

from _source import module

from asm_dependencies.conditional import Environment, evaluate_test
from asm_dependencies.parser import parse_asm

# The shape the research turned up: which copybook is read, and therefore which V-cons and
# DCBs exist, is decided by SYSPARM= on the assemble step's JCL - not by this source.
SITE_SWITCH = module(
    ("P", "CSECT"),
    ("&SITE", "SETC", "'&SYSPARM'"),
    ("", "AIF", "('&SITE' EQ 'PROD').PROD"),
    ("", "DC", "V(TESTSUB)"),
    ("", "AGO", ".DONE"),
    (".PROD", "ANOP"),
    ("", "DC", "V(PRODSUB)"),
    (".DONE", "ANOP"),
    ("", "END"),
)


def refs(mod):
    return {e.name: e for e in mod.externals}


def test_an_undecidable_branch_keeps_both_arms_and_says_what_governs_them():
    """Reporting both flatly claims the module depends on both, which is false for every
    real assembly. Picking one is a guess presented as a fact. So: both, with the test."""
    mod = parse_asm(SITE_SWITCH)
    assert set(refs(mod)) == {"TESTSUB", "PRODSUB"}
    for ref in mod.externals:
        assert ref.conditions and ref.conditions[0].decided is False
    assert "&SITE" in refs(mod)["TESTSUB"].conditions[0].test


def test_supplying_sysparm_collapses_it_to_one_answer():
    mod = parse_asm(SITE_SWITCH, sysparm="PROD")
    assert set(refs(mod)) == {"PRODSUB"}
    assert refs(mod)["PRODSUB"].conditions == []


def test_the_other_value_selects_the_other_arm():
    mod = parse_asm(SITE_SWITCH, sysparm="TEST")
    assert set(refs(mod)) == {"TESTSUB"}


def test_a_decidable_test_needs_no_sysparm_at_all():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("&MODE", "SETC", "'DB2'"),
        ("", "AIF", "('&MODE' EQ 'DB2').USEDB2"),
        ("", "DC", "V(IMSSUB)"),
        (".USEDB2", "ANOP"),
        ("", "DC", "V(DB2SUB)"),
        ("", "END"),
    ))
    assert set(refs(mod)) == {"DB2SUB"}


def test_a_conditional_copy_is_reported_with_its_condition():
    """A COPY under an undecided AIF is a conditional dependency, not an absent one and
    not an unconditional one."""
    lib = {"PRODPARM": module(("", "DC", "V(PRODIO)")),
           "TESTPARM": module(("", "DC", "V(TESTIO)"))}
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "AIF", "('&SYSPARM' EQ 'PROD').PROD"),
        ("", "COPY", "TESTPARM"),
        ("", "AGO", ".DONE"),
        (".PROD", "ANOP"),
        ("", "COPY", "PRODPARM"),
        (".DONE", "ANOP"),
        ("", "END"),
    ), resolver=lambda n: lib.get(n.upper()))
    assert {m.name for m in mod.members} == {"TESTPARM", "PRODPARM"}
    assert all(m.conditions for m in mod.members)
    # The condition travels through the COPY onto what the member declares.
    assert all(e.conditions for e in mod.externals)


def test_a_backward_branch_is_flagged_rather_than_unrolled():
    mod = parse_asm(module(
        ("P", "CSECT"),
        (".TOP", "ANOP"),
        ("", "DC", "V(SUB)"),
        ("", "AIF", "(&I LT 4).TOP"),
        ("", "END"),
    ))
    assert any("branches backwards" in f for f in mod.flags)


def test_a_branch_to_a_label_that_does_not_exist_is_flagged():
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "AIF", "('&X' EQ 'Y').NOWHERE"), ("", "END")))
    assert any("not a sequence symbol" in f for f in mod.flags)


def test_conditional_assembly_statements_do_not_reach_the_manifest():
    mod = parse_asm(module(
        ("P", "CSECT"), ("&A", "SETA", "1"), ("", "ANOP"), ("", "END")))
    assert mod.members == []
    assert mod.flags == []


# --- the evaluator ---------------------------------------------------------------

def test_an_unknown_operand_makes_the_whole_test_unknown():
    """None propagates deliberately. Guessing an operand decides a branch, and a wrongly
    decided branch produces no flag - just a manifest quietly missing the other side."""
    assert evaluate_test("'&SYSPARM' EQ 'PROD'", Environment()) is None


def test_a_false_conjunct_decides_the_test_even_when_the_other_is_unknown():
    env = Environment()
    env.set("&DEBUG", 0)
    assert evaluate_test("(&DEBUG EQ 1) AND ('&SYSPARM' EQ 'TEST')", env) is False
    assert evaluate_test("(&DEBUG EQ 1) OR ('&SYSPARM' EQ 'TEST')", env) is None


def test_a_true_disjunct_decides_the_test_even_when_the_other_is_unknown():
    env = Environment()
    env.set("&DEBUG", 1)
    assert evaluate_test("(&DEBUG EQ 1) OR ('&SYSPARM' EQ 'TEST')", env) is True


def test_substring_notation_is_evaluated():
    env = Environment(sysparm="PRODEAST")
    assert evaluate_test("'&SYSPARM'(1,4) EQ 'PROD'", env) is True


def test_arithmetic_and_relations_are_evaluated():
    env = Environment()
    env.set("&N", 3)
    assert evaluate_test("&N LT 4", env) is True
    assert evaluate_test("&N GE 4", env) is False
    assert evaluate_test("NOT (&N EQ 3)", env) is False
