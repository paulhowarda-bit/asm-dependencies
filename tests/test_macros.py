"""COPY members and macros - the text a module is assembled from but does not contain.

The tests that matter most here are the site-macro ones. A module whose calls are all
behind `@LINK PGM=X` has, read literally, no dependencies at all - and a manifest that
says so looks exactly like a manifest for a module that genuinely has none.
"""

from _source import module

from asm_dependencies.macros import MacroExpander
from asm_dependencies.parser import parse_asm


class Library:
    """A macro/copy library that counts what it was asked for."""

    def __init__(self, **members):
        self.members = {k.upper(): v for k, v in members.items()}
        self.asked = []

    def __call__(self, name):
        self.asked.append(name.upper())
        if name.upper() == "BOOM":
            raise RuntimeError("estate share unreachable (simulated)")
        return self.members.get(name.upper())


def names(mod):
    return {e.name for e in mod.externals}


def members(mod):
    return {(m.name, m.kind, m.resolved) for m in mod.members}


# --- COPY ----------------------------------------------------------------------------

def test_a_copy_member_contributes_its_dependencies():
    lib = Library(CUSTDCB=module(
        ("CUSTFILE", "DC", "V(IOROUTIN)"),
        ("PGMNAME", "DC", "CL8'PAYCALC '"),
    ))
    mod = parse_asm(module(("P", "CSECT"), ("", "COPY", "CUSTDCB"), ("", "END")),
                    resolver=lib)
    assert names(mod) == {"IOROUTIN"}
    assert ("CUSTDCB", "copybook", True) in members(mod)
    assert [d.label for d in mod.data] == ["CUSTFILE", "PGMNAME"]


def test_a_copied_statement_remembers_which_member_it_came_from():
    lib = Library(CUSTDCB=module(("", "DC", "V(IOROUTIN)")))
    mod = parse_asm(module(("P", "CSECT"), ("", "COPY", "CUSTDCB"), ("", "END")),
                    resolver=lib)
    assert mod.externals[0].origin == "CUSTDCB"
    assert mod.externals[0].depth == 1


def test_an_unresolved_copy_says_what_is_missing_rather_than_nothing():
    lib = Library()
    mod = parse_asm(module(("P", "CSECT"), ("", "COPY", "MISSING"), ("", "END")),
                    resolver=lib)
    assert ("MISSING", "copybook", False) in members(mod)
    assert any("MISSING was not resolved" in f for f in mod.flags)


def test_a_failed_request_is_not_reported_as_an_absent_member():
    """Raising means the request failed - credentials, connectivity. Returning None means
    the estate was asked and had nothing. Reporting the first as the second makes an
    unreachable share read as an empty one, with full confidence."""
    lib = Library()
    mod = parse_asm(module(("P", "CSECT"), ("", "COPY", "BOOM"), ("", "END")),
                    resolver=lib)
    assert any("could not be retrieved" in f for f in mod.flags)


def test_a_computed_copy_member_name_is_flagged_not_guessed():
    """`COPY &MEM` builds its member name from conditional assembly, typically &SYSPARM.
    Which member is copied is decided by the assemble step's PARM=, not by this source."""
    lib = Library()
    mod = parse_asm(module(("P", "CSECT"), ("", "COPY", "&MEM"), ("", "END")),
                    resolver=lib)
    use = [m for m in mod.members if m.computed]
    assert use and use[0].resolved is False
    assert lib.asked == []
    assert any("builds its member name by substitution" in f for f in mod.flags)


def test_a_copy_that_copies_itself_is_stopped_and_says_so():
    lib = Library(LOOPY=module(("", "COPY", "LOOPY")))
    mod = parse_asm(module(("P", "CSECT"), ("", "COPY", "LOOPY"), ("", "END")),
                    resolver=lib)
    assert any("already being expanded" in f for f in mod.flags)


def test_copied_text_is_lexed_with_the_default_margins():
    """ICTL does not propagate into COPY members, so one assembly can carry two column
    geometries. Lexing the member with the container's margins mis-reads every line."""
    lib = Library(WIDE=module(("", "DC", "V(FOUND)", ), ))
    src = module(("", "ICTL", "1,80"), ("P", "CSECT"), ("", "COPY", "WIDE"), ("", "END"))
    mod = parse_asm(src, resolver=lib)
    assert names(mod) == {"FOUND"}


# --- site macros: the reason this stage exists ----------------------------------------

SITE_LINK = module(
    ("", "MACRO"),
    ("", "@LINK", "&PGM="),
    ("", "L", "R15,=V(&PGM)"),
    ("", "BALR", "R14,R15"),
    ("", "MEND"),
)


def test_a_site_macro_wrapping_link_yields_the_call_it_hides():
    """The whole point of expanding macros rather than scanning for IBM macro names."""
    lib = Library(**{"@LINK": SITE_LINK})
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("", "@LINK", "PGM=PAYSUB1"),
        ("", "@LINK", "PGM=PAYSUB2"),
        ("", "END"),
    ), resolver=lib)
    assert names(mod) == {"PAYSUB1", "PAYSUB2"}
    assert ("@LINK", "macro", True) in members(mod)
    assert mod.flags == []


def test_an_inline_macro_definition_needs_no_library():
    mod = parse_asm(SITE_LINK + "\n" + module(
        ("PAYCALC", "CSECT"), ("", "@LINK", "PGM=PAYSUB1"), ("", "END")))
    assert names(mod) == {"PAYSUB1"}


def test_a_macro_that_invokes_a_macro_needs_a_second_round():
    """The inner invocation only becomes visible once the outer macro's text is in hand,
    which is why the closure is discovered by replaying the parse, not by scanning."""
    lib = Library(**{
        "@LINK": SITE_LINK,
        "@STDCALL": module(
            ("", "MACRO"),
            ("", "@STDCALL", "&PGM="),
            ("", "@LINK", "PGM=&PGM"),
            ("", "MEND"),
        ),
    })
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"), ("", "@STDCALL", "PGM=DEEPSUB"), ("", "END")), resolver=lib)
    assert names(mod) == {"DEEPSUB"}
    assert "@LINK" in lib.asked and "@STDCALL" in lib.asked


def test_positional_macro_parameters_substitute_in_order():
    lib = Library(**{"@CALL2": module(
        ("", "MACRO"),
        ("", "@CALL2", "&FIRST,&SECOND"),
        ("", "DC", "V(&FIRST)"),
        ("", "DC", "V(&SECOND)"),
        ("", "MEND"),
    )})
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "@CALL2", "ALPHA,BETA"), ("", "END")), resolver=lib)
    assert names(mod) == {"ALPHA", "BETA"}


def test_a_keyword_default_is_used_when_the_call_omits_it():
    lib = Library(**{"@DFLT": module(
        ("", "MACRO"),
        ("", "@DFLT", "&PGM=STDSUB"),
        ("", "DC", "V(&PGM)"),
        ("", "MEND"),
    )})
    mod = parse_asm(module(("P", "CSECT"), ("", "@DFLT"), ("", "END")), resolver=lib)
    assert names(mod) == {"STDSUB"}


def test_an_unsupplied_parameter_stays_visible_and_is_never_blanked():
    """A DCB generated with DDNAME='' is a file binding that is WRONG rather than absent,
    and the wrong one is the one nobody notices."""
    lib = Library(**{"@NEEDS": module(
        ("", "MACRO"),
        ("", "@NEEDS", "&PGM"),
        ("", "DC", "V(&PGM)"),
        ("", "MEND"),
    )})
    mod = parse_asm(module(("P", "CSECT"), ("", "@NEEDS"), ("", "END")), resolver=lib)
    assert names(mod) == set()
    assert any("&PGM was not supplied" in f for f in mod.flags)


def test_a_macro_member_that_defines_nothing_is_reported_rather_than_ignored():
    lib = Library(**{"@EMPTY": module(("", "DS", "0H"))})
    mod = parse_asm(module(("P", "CSECT"), ("", "@EMPTY"), ("", "END")), resolver=lib)
    assert any("contains no MACRO/MEND definition" in f for f in mod.flags)


def test_an_unclosed_macro_definition_is_flagged():
    src = module(("", "MACRO"), ("", "@BROKEN", "&A"), ("", "DC", "V(&A)"))
    mod = parse_asm(src)
    assert any("not closed by MEND" in f for f in mod.flags)


# --- the invariant prefetch depends on ------------------------------------------------

def test_the_resolver_is_asked_on_every_encounter_and_never_memoized():
    """prefetch closes the member set by REPLAYING the parse under a recording resolver
    until it stops asking for members it has not got. Answering the second ask from a
    cache would make the replay silent, and the closure short - with no error, just a
    module that reads as though it had fewer dependencies than it has."""
    lib = Library(SHARED=module(("", "DC", "V(IOROUTIN)")))
    parse_asm(module(
        ("P", "CSECT"),
        ("", "COPY", "SHARED"),
        ("", "COPY", "SHARED"),
        ("", "END"),
    ), resolver=lib)
    assert lib.asked == ["SHARED", "SHARED"]


def test_expansion_is_bounded_and_says_when_it_stopped():
    deep = {"M{0}".format(i): module(
        ("", "COPY", "M{0}".format(i + 1))) for i in range(40)}
    lib = Library(**deep)
    expander = MacroExpander(lib, max_depth=3)
    from asm_dependencies.lexer import logical_lines
    stmts, _ = logical_lines(module(("", "COPY", "M0")))
    expander.expand(stmts)
    assert any("nested deeper than 3 levels" in f for f in expander.flags)


# --- regressions found by running against real source --------------------------------
#
# All three came out of IBM's own IFOX (Assembler XF) source, whose JPHASE macro has this
# shape: locals declared with LCLA/LCLC, an AIF whose target sits AFTER a MEXIT, and the
# closing MEND carrying a sequence symbol. Between them they produced 45 wrong flags in a
# single member.

JPHASE = module(
    ("", "MACRO"),
    ("", "JPHASE", "&PHASE=,&PHSUFF="),
    ("", "LCLA", "&A"),
    ("", "LCLC", "&C"),
    ("", "AIF", "(K'&PHSUFF EQ 0).NX10"),
    ("&C", "SETC", "'&PHSUFF'"),
    ("", "DC", "V(SUFFIXED)"),
    ("", "MEXIT"),
    (".NX10", "ANOP"),
    ("", "DC", "V(PLAINCASE)"),
    (".EXIT", "MEND"),
)


def test_mexit_does_not_truncate_the_macro_body():
    """MEXIT ends GENERATION, and only when conditional assembly reaches it. Treating it
    as a static end of the body threw away the `.NX10` branch - which is the branch the
    AIF above it selects, so the common path generated nothing at all."""
    mod = parse_asm(JPHASE + "\n" + module(
        ("P", "CSECT"), ("", "JPHASE", "PHASE=X0A"), ("", "END")))
    assert "PLAINCASE" in names(mod)


def test_a_macros_own_lcl_and_set_variables_are_not_unsupplied_parameters():
    """&A and &C are conditional-assembly locals the body declares, not parameters the
    caller forgot. Flagging them buried the real unsupplied-parameter flag under one per
    local per invocation."""
    mod = parse_asm(JPHASE + "\n" + module(
        ("P", "CSECT"), ("", "JPHASE", "PHASE=X0A"), ("", "END")))
    assert not [f for f in mod.flags if "&A was not supplied" in f]
    assert not [f for f in mod.flags if "&C was not supplied" in f]


def test_a_sequence_symbol_on_mend_is_a_real_branch_target():
    """`.EXIT MEND` - dropping the label made every `AIF (...).EXIT` in the body report as
    a branch to a label that does not exist."""
    mod = parse_asm(module(
        ("", "MACRO"),
        ("", "GUARDED", "&OPT="),
        ("", "AIF", "('&OPT' EQ 'NONE').EXIT"),
        ("", "DC", "V(OPTIONAL)"),
        (".EXIT", "MEND"),
    ) + "\n" + module(("P", "CSECT"), ("", "GUARDED", "OPT=YES"), ("", "END")))
    assert not [f for f in mod.flags if "not a sequence symbol" in f]
    assert "OPTIONAL" in names(mod)


def test_a_module_whose_control_section_is_macro_generated_says_so():
    """IFOX declares its control section as `JCSECT (X0A00)` - a site macro. Without the
    macro library the module genuinely provides nothing this tool can see, and an empty
    'provides' reads as 'exposes no entry point' rather than 'the names are hidden'."""
    mod = parse_asm(module(
        ("", "JCSECT", "(X0A00)"),
        ("START", "SAVE", "(14,12)"),
        ("", "END", "START"),
    ))
    assert mod.provides() == []
    flag = next(f for f in mod.flags if "declares no control section" in f)
    assert "JCSECT" in flag
    assert "END statement names START" in flag


def test_an_ibm_macro_is_excluded_rather_than_chased_through_the_estate():
    """SYS1.MACLIB is not the shop's source. Chasing GETMAIN and WTO fills the retrieval
    report with not-founds that bury the SITE macros - the ones hiding real calls."""
    from asm_dependencies.views import build_asm_artifacts
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "GETMAIN", "R,LV=256"),
        ("", "WTO", "'STARTED'"),
        ("", "@SITELINK", "PGM=PAYCALC"),
        ("", "END"),
    ))
    view = build_asm_artifacts(mod)
    assert [r["artifact"] for r in view["artifacts"]] == ["@SITELINK"]
    assert {e["name"] for e in view["excluded"]} == {"GETMAIN", "WTO"}
    # ...and the flag for an IBM macro is the narrower one.
    assert any("generates MVS runtime code" in f for f in mod.flags)
    assert any("@SITELINK" in f and "NOT in this manifest" in f for f in mod.flags)
