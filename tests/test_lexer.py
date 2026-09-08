"""The column rules. Everything downstream is wrong if these are wrong."""

import pytest

from asm_dependencies.lexer import (
    COMMENT, MACRO_COMMENT, PROCESS, STATEMENT, DEFAULT_MARGINS, Margins,
    is_literal, is_register_form, keyword, literal_value, logical_lines, operands,
    read_ictl, split_operand,
)


from _source import asm, cont


def statements(text, **kw):
    stmts, flags = logical_lines(text, **kw)
    return [s for s in stmts if s.kind == STATEMENT], flags


# --- columns -----------------------------------------------------------------------

def test_the_sequence_field_is_not_part_of_the_statement():
    """Columns 73-80 hold member names and change ids. Read as operand text they become
    dependencies on whatever the shop happened to stamp there."""
    src = asm("PAYCALC", "CSECT", seq="PAYCALC1")
    stmts, flags = statements(src)
    assert stmts[0].name == "PAYCALC"
    assert stmts[0].operation == "CSECT"
    assert stmts[0].operand == ""
    assert flags == []


def test_source_past_the_identification_field_is_flagged_not_dropped():
    src = asm("", "L", "R15,=V(A)").ljust(90) + "REALCODE"
    _, flags = statements(src)
    assert len(flags) == 1
    assert "past column 71" in flags[0]
    assert "REALCODE" in flags[0]


def test_an_operand_running_past_column_71_is_cut_where_the_assembler_cuts_it():
    long_operand = "=V(" + "A" * 60 + ")"
    src = asm("", "L", "R15," + long_operand)
    stmts, _ = statements(src)
    assert len(stmts[0].operand) == 71 - 15


# --- continuation ------------------------------------------------------------------

def test_continuation_is_column_72_and_resumes_at_column_16():
    src = "\n".join([
        asm("", "ATTACH", "EP=PAYCALC,", cont=True),
        cont("PARAM=(A,B),", more=True),
        cont("VL=1"),
    ])
    stmts, flags = statements(src)
    assert len(stmts) == 1
    assert stmts[0].operand == "EP=PAYCALC,PARAM=(A,B),VL=1"
    assert stmts[0].line == 1
    assert flags == []


def test_columns_1_to_15_of_a_continuation_line_are_not_part_of_the_statement():
    """The assembler ignores the gutter. Reading it would splice the sequence area of one
    line onto the operand of another."""
    src = "\n".join([
        asm("", "ATTACH", "EP=PAYCALC,", cont=True),
        "JUNKHERE" + " " * 7 + "VL=1",
    ])
    stmts, flags = statements(src)
    assert stmts[0].operand == "EP=PAYCALC,VL=1"
    assert any("before the continue column" in f for f in flags)


def test_each_continued_line_may_carry_its_own_remarks():
    """Alternative format: operands end with a comma and remarks follow on the same line."""
    src = "\n".join([
        asm("", "ATTACH", "EP=PAYCALC,  THE PAYROLL MODULE", cont=True),
        cont("VL=1         VARIABLE LIST"),
    ])
    stmts, _ = statements(src)
    assert stmts[0].operand == "EP=PAYCALC,VL=1"
    assert "THE PAYROLL MODULE" in stmts[0].remarks
    assert "VARIABLE LIST" in stmts[0].remarks


def test_a_dangling_continuation_at_end_of_member_is_flagged():
    src = asm("", "ATTACH", "EP=PAYCALC,", cont=True)
    _, flags = statements(src)
    assert any("nothing to continue onto" in f for f in flags)


def test_a_blank_column_72_ends_the_statement():
    src = "\n".join([asm("", "L", "R15,=V(A)"), asm("", "BALR", "R14,R15")])
    stmts, _ = statements(src)
    assert [s.operation for s in stmts] == ["L", "BALR"]


# --- comments ----------------------------------------------------------------------

def test_a_comment_is_passed_through_not_dropped():
    """The CICS translator leaves the original EXEC CICS command as a comment beside the
    DFHECALL it generated, so a lexer that drops comments cannot read translated source."""
    src = "*        EXEC CICS SEND MAP('CUSTM01')"
    stmts, _ = logical_lines(src)
    assert len(stmts) == 1
    assert stmts[0].kind == COMMENT
    assert "EXEC CICS SEND MAP('CUSTM01')" in stmts[0].operand


def test_a_macro_comment_is_distinguished_from_an_ordinary_one():
    stmts, _ = logical_lines(".*       NOT GENERATED\n*        GENERATED")
    assert [s.kind for s in stmts] == [MACRO_COMMENT, COMMENT]


def test_a_continued_comment_swallows_the_following_line():
    """The one comment rule that changes what the NEXT statement is: a comment with a
    non-blank column 72 continues, so the line after it is not a statement at all."""
    src = "\n".join([
        "*        THIS COMMENT CONTINUES".ljust(71) + "X",
        asm("", "L", "R15,=V(SWALLOWED)"),
        asm("", "BALR", "R14,R15"),
    ])
    stmts, _ = logical_lines(src)
    assert stmts[0].kind == COMMENT
    assert "SWALLOWED" in stmts[0].operand
    assert [s.operation for s in stmts if s.kind == STATEMENT] == ["BALR"]


def test_a_comment_never_reports_a_tail_past_the_margin():
    """A comment runs to the end of the line whatever the margin is - flagging its prose
    would emit one flag per line for every commented member in the estate."""
    _, flags = logical_lines("*" + " ORDINARY PROSE THAT IS LONG " * 4)
    assert flags == []


# --- ICTL --------------------------------------------------------------------------

def test_ictl_moves_the_margins():
    """An operand that would be cut at column 71 survives whole when ICTL widens the end
    column - and no longer reports a tail, because there is no longer a tail."""
    long_operand = "R15,=V(" + "A" * 55 + ")"          # ends at column 78
    src = "\n".join([asm("", "ICTL", "1,80"), asm("", "L", long_operand)])

    margins = read_ictl(src.splitlines(), [])
    assert margins.end == 80
    assert margins.continuation_allowed is False

    wide, wide_flags = statements(src, margins=margins)
    assert wide[1].operand == long_operand
    assert wide_flags == []

    narrow, narrow_flags = statements(src, margins=DEFAULT_MARGINS)
    assert narrow[1].operand != long_operand
    assert any("past column 71" in f for f in narrow_flags)


def test_a_malformed_ictl_keeps_the_defaults_and_says_so():
    flags = []
    assert read_ictl([asm("", "ICTL", "9,9,9")], flags) == DEFAULT_MARGINS
    assert any("outside the ranges" in f for f in flags)


def test_ictl_margins_are_not_inherited_by_copied_text():
    """ICTL does not propagate into COPY members or library macros - they always use
    1/71/16. The expander must be able to lex copied text with the defaults."""
    member = asm("", "L", "R15,=V(A)", seq="MEMBER01")
    stmts, flags = statements(member, margins=DEFAULT_MARGINS)
    assert stmts[0].operand == "R15,=V(A)"
    assert flags == []


def test_margins_can_be_supplied_explicitly():
    src = "  " + asm("", "L", "R15,=V(A)")[:69]
    stmts, _ = statements(src, margins=Margins(begin=3, end=71, cont=18))
    assert stmts[0].operation == "L"


# --- fields ------------------------------------------------------------------------

def test_a_sequence_symbol_in_the_name_field_means_no_name():
    """`.RETRY CSECT` starts the UNNAMED control section. Keeping the sequence symbol in
    the name field would invent a control section called `.RETRY`."""
    stmts, _ = statements(asm(".RETRY", "CSECT"))
    assert stmts[0].name == ""
    assert stmts[0].sequence_symbol == ".RETRY"


def test_a_blank_name_field_is_empty_not_the_operation():
    stmts, _ = statements(asm("", "CSECT"))
    assert stmts[0].name == ""
    assert stmts[0].operation == "CSECT"


def test_a_variable_symbol_anywhere_is_visible():
    stmts, _ = statements(asm("", "LINK", "EP=&PGM"))
    assert stmts[0].has_variable_symbol is True
    stmts, _ = statements(asm("", "LINK", "EP=PAYCALC"))
    assert stmts[0].has_variable_symbol is False


def test_process_statements_are_read_in_the_header_and_are_comments_after_it():
    src = "\n".join([
        "*PROCESS RENT,GOFF",
        asm("PAYCALC", "CSECT"),
        "*PROCESS TOO,LATE",
    ])
    stmts, _ = logical_lines(src)
    assert stmts[0].kind == PROCESS
    assert stmts[0].operand == "RENT,GOFF"
    assert stmts[2].kind == COMMENT


# --- operand splitting -------------------------------------------------------------

@pytest.mark.parametrize("text,operand,remarks", [
    ("A,B  MOVE THE FIELD", "A,B", "MOVE THE FIELD"),
    ("C'THIS IS NOT A COMMENT'", "C'THIS IS NOT A COMMENT'", ""),
    ("C'A,B'  A LITERAL COMMA", "C'A,B'", "A LITERAL COMMA"),
    ("PARAM=(A, B),VL=1  NOTE", "PARAM=(A, B),VL=1", "NOTE"),
    ("C'IT''S'  EMBEDDED QUOTE", "C'IT''S'", "EMBEDDED QUOTE"),
])
def test_the_operand_ends_at_the_first_blank_outside_quotes_and_parens(
        text, operand, remarks):
    assert split_operand(text) == (operand, remarks)


@pytest.mark.parametrize("text,parts", [
    ("EP=PAYCALC,PARAM=(A,B),VL=1", ["EP=PAYCALC", "PARAM=(A,B)", "VL=1"]),
    ("C'A,B',C'C'", ["C'A,B'", "C'C'"]),
    ("(2,12),EP=NEXTPGM", ["(2,12)", "EP=NEXTPGM"]),
    ("", []),
    ("SINGLE", ["SINGLE"]),
])
def test_operands_split_on_top_level_commas_only(text, parts):
    assert operands(text) == parts


@pytest.mark.parametrize("part,key,value", [
    ("EP=PAYCALC", "EP", "PAYCALC"),
    ("DDNAME=CUSTMAST", "DDNAME", "CUSTMAST"),
    ("=V(PAYCALC)", None, "=V(PAYCALC)"),
    ("(2,12)", None, "(2,12)"),
    ("MF=(E,LINKLST)", "MF", "(E,LINKLST)"),
])
def test_a_keyword_operand_splits_only_on_a_top_level_equals(part, key, value):
    assert keyword(part) == (key, value)


@pytest.mark.parametrize("value,is_reg", [
    ("(1)", True), ("(R1)", True), ("(15)", True),
    ("PGMNAME", False), ("(2,12)", False), ("()", False),
])
def test_a_parenthesised_register_is_recognised(value, is_reg):
    assert is_register_form(value) is is_reg


@pytest.mark.parametrize("token,value", [
    ("C'PAYCALC'", "PAYCALC"),
    ("CL8'PAYCALC '", "PAYCALC "),
    ("'PAYCALC'", "PAYCALC"),
    ("X'0C'", "0C"),
    ("C'IT''S'", "IT'S"),
])
def test_typed_and_quoted_literals_read_as_their_contents(token, value):
    assert is_literal(token) is True
    assert literal_value(token) == value


def test_a_bare_symbol_is_not_a_literal():
    assert is_literal("PAYCALC") is False
    assert literal_value("PAYCALC") == "PAYCALC"
