"""The output contract - the shape the rest of the family emits.

A consumer that already reads ``jcl-dependencies`` and ``eztrieve-dependencies`` output
must be able to read this with the same code. None of that is enforced by the structure of
the source, so it is asserted here and its exact bytes are guarded by the ratchet.
"""

import json
from pathlib import Path

import pytest

from _source import module

from asm_dependencies.parser import parse_asm
from asm_dependencies.views import (
    FORMAT_ARTIFACTS, FORMAT_LINEAGE, bind_jcl_ddnames, build_asm_artifacts,
    build_asm_lineage,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

SAMPLE = module(
    ("PAYCALC", "CSECT"),
    ("", "ENTRY", "PAYINIT"),
    ("", "DC", "V(PAYVALD)"),
    ("", "LINK", "EP=PAYPOST"),
    ("CUSTDCB", "DCB", "DDNAME=CUSTMAST,DSORG=PS,MACRF=(GM)"),
    ("", "COPY", "PAYREC"),
    ("PAYINIT", "DS", "0H"),
    ("", "END", "PAYCALC"),
)


@pytest.fixture()
def artifacts():
    return build_asm_artifacts(parse_asm(SAMPLE, source_name="paycalc.asm"))


@pytest.fixture()
def lineage():
    return build_asm_lineage(parse_asm(SAMPLE, source_name="paycalc.asm"))


# --- the family shape ---------------------------------------------------------------

def test_the_artifacts_view_has_the_family_keys_in_the_family_order(artifacts):
    assert list(artifacts) == ["format", "formatVersion", "program", "source", "note",
                               "provides", "artifacts", "candidates", "excluded",
                               "flags"]


def test_the_lineage_view_starts_and_ends_the_way_the_family_does(lineage):
    keys = list(lineage)
    assert keys[:5] == ["format", "formatVersion", "program", "source", "note"]
    assert keys[-1] == "flags"


def test_the_subject_key_is_spelled_program(artifacts):
    """`mainframe_artifacts.fetch` reads `manifest.get("program") or manifest.get("job")`
    to know what NOT to fetch. Keyed anything else, the never-fetch-yourself guard holds
    "?" and the module requests ITSELF from the estate as its own dependency."""
    assert artifacts["program"] == "PAYCALC"
    assert "module" not in artifacts


def test_the_format_strings_name_this_package(artifacts, lineage):
    assert artifacts["format"] == FORMAT_ARTIFACTS == "asm-dependencies-artifacts"
    assert lineage["format"] == FORMAT_LINEAGE == "asm-dependencies-lineage"


def test_every_artifact_row_carries_the_universal_fields(artifacts):
    for row in artifacts["artifacts"]:
        assert set(row) >= {"artifact", "kind", "dependency", "identity", "touchedBy"}
        assert "resolvedBy" in row or "needs" in row


def test_dependency_is_only_ever_runtime_or_compile_time(artifacts):
    values = {r["dependency"] for r in artifacts["artifacts"]}
    assert values <= {"runtime", "compile-time"}
    # ...and the split is the one the family means by it.
    kinds = {r["kind"]: r["dependency"] for r in artifacts["artifacts"]}
    assert kinds["program"] == "runtime"
    assert kinds["file"] == "runtime"
    assert kinds["copybook"] == "compile-time"


def test_file_rows_use_the_same_io_vocabulary_as_the_easytrieve_file_row(artifacts):
    io = {r["io"] for r in artifacts["artifacts"] if r["kind"] == "file"}
    assert io <= {"read", "write", "read-write", "unknown"}


def test_rows_are_sorted_by_class_then_name(artifacts):
    order = {"program": 0, "file": 1, "copybook": 7, "macro": 8}
    keys = [(order[r["kind"]], r["artifact"]) for r in artifacts["artifacts"]]
    assert keys == sorted(keys)


def test_a_boolean_flag_is_present_or_absent_and_never_false():
    """The family writes presence-only booleans. A `false` is a different contract."""
    mod = parse_asm(module(
        ("P", "CSECT"), ("", "WXTRN", "OPTDEP"), ("", "LINK", "EPLOC=(1)"), ("", "END")))
    view = build_asm_artifacts(mod)
    for row in view["artifacts"]:
        for key, value in row.items():
            assert value is not False, "{0} wrote {1}=false".format(row["artifact"], key)
    weak = next(r for r in view["artifacts"] if r["artifact"] == "OPTDEP")
    assert weak["weak"] is True


def test_flags_are_whole_sentences_not_codes():
    mod = parse_asm(module(("P", "CSECT"), ("", "@SITEMAC", "X=1"), ("", "END")))
    view = build_asm_artifacts(mod)
    assert view["flags"]
    for flag in view["flags"]:
        assert len(flag.split()) > 6 and flag[:1].islower() is False or "line" in flag


def test_the_views_are_json_serialisable_and_byte_stable(artifacts, lineage):
    for view in (artifacts, lineage):
        first = json.dumps(view, indent=2) + "\n"
        second = json.dumps(view, indent=2) + "\n"
        assert first == second
        assert json.loads(first) == view


# --- what this package adds to the shape ---------------------------------------------

def test_evidence_is_on_every_row_whose_name_had_to_be_established():
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LINK", "EP=KNOWN"),
        ("", "LINK", "EPLOC=FIELD"),
        ("", "LINK", "EPLOC=(1)"),
        ("FIELD", "DC", "CL8'DERIVED '"),
        ("", "END"),
    ))
    rows = {r["artifact"]: r for r in build_asm_artifacts(mod)["artifacts"]}
    assert rows["KNOWN"]["evidence"] == "literal"
    assert rows["DERIVED"]["evidence"] == "assigned"
    assert all(r["evidence"] in ("literal", "assigned", "table", "dynamic")
               for r in rows.values())


def test_a_dynamic_site_is_marked_so_the_fetch_stage_skips_it_with_a_reason():
    """`row["dynamic"]` is what `mainframe_artifacts.fetch._request_name` already keys on,
    so an unknowable target is skipped WITH a reason rather than requested and missed."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "LOAD", "EPLOC=FIELD"),
        ("", "MVC", "FIELD,SELECTED"),
        ("FIELD", "DC", "CL8'DECOY   '"),
        ("", "END"),
    ))
    view = build_asm_artifacts(mod)
    # NOT an artifact: the DC says DECOY and a store reaches the site, so it is not
    # established that this call goes there.
    assert [r["artifact"] for r in view["artifacts"]] == []
    # ...and not silently dropped either, which is the failure this package exists for.
    assert [c["candidate"] for c in view["candidates"]] == ["DECOY"]
    assert view["candidates"][0]["evidence"] == "table"
    assert "MVC into it" in view["candidates"][0]["forSite"][0]["reason"]


def test_conditional_is_separate_from_evidence():
    """Evidence says how well the name is known; conditional says whether the statement is
    assembled at all. A literal name in an undecided AIF branch is both."""
    mod = parse_asm(module(
        ("P", "CSECT"),
        ("", "AIF", "('&SYSPARM' EQ 'PROD').P"),
        ("", "LINK", "EP=TESTSUB"),
        (".P", "ANOP"),
        ("", "END"),
    ))
    row = build_asm_artifacts(mod)["artifacts"][0]
    assert row["evidence"] == "literal"
    assert row["conditional"] is True
    assert row["condition"][0]["test"] == "'&SYSPARM' EQ 'PROD'"


def test_provides_is_the_half_of_the_call_graph_no_sibling_emits(artifacts):
    assert [p["name"] for p in artifacts["provides"]] == ["PAYCALC", "PAYINIT"]
    assert artifacts["provides"][0]["kind"] == "csect"


def test_the_unresolved_inventory_lists_every_site_the_source_cannot_name(lineage):
    assert "unresolved" in lineage
    mod = parse_asm(module(("P", "CSECT"), ("", "LINK", "EPLOC=(1)"), ("", "END")))
    rows = build_asm_lineage(mod)["unresolved"]
    assert [r["site"] for r in rows] == ["LINK"]
    assert "register" in rows[0]["reason"]


# --- the JCL join ---------------------------------------------------------------------

def test_binding_against_a_real_jcl_lineage_view_closes_the_ddnames():
    """The fixture is a committed view a real jcl-dependencies run produced - this
    repository's half of a cross-repository contract, so these tests need no JCL install."""
    lineage = json.loads(
        (FIXTURES / "paycalc.jcl.lineage.json").read_text(encoding="utf-8"))
    bound = bind_jcl_ddnames(build_asm_artifacts(parse_asm(SAMPLE)), lineage)
    row = next(r for r in bound["artifacts"] if r.get("ddname") == "CUSTMAST")
    assert row["dataset"] == "PROD.CUSTOMER.MASTER"
    assert "needs" not in row
    assert bound["jclBinding"]["steps"] == ["PAYSTEP"]
    assert "EXEC PGM=" in bound["jclBinding"]["basis"]


def test_the_artifacts_view_is_refused_where_the_lineage_view_is_required():
    """Its control-card rows are keyed on DSN alone, so two members of one library collapse
    into a single row. An empty binding looks exactly like one nobody attempted."""
    bound = bind_jcl_ddnames(build_asm_artifacts(parse_asm(SAMPLE)),
                             {"format": "jcl-dependencies-artifacts"})
    assert bound["jclBinding"]["basis"] == "not attempted"
    assert any("not" in f and "format" in f for f in bound["flags"])


def test_a_ddname_the_module_sets_at_run_time_is_left_unbound():
    """Binding whatever DD shares the label's name would attach a real dataset to a file
    whose identity the module decides at run time."""
    lineage = json.loads(
        (FIXTURES / "paycalc.jcl.lineage.json").read_text(encoding="utf-8"))
    mod = parse_asm(module(
        ("PAYCALC", "CSECT"),
        ("CUSTMAST", "DCB", "DSORG=PS,MACRF=(GM)"),
        ("", "END"),
    ))
    bound = bind_jcl_ddnames(build_asm_artifacts(mod), lineage)
    row = next(r for r in bound["artifacts"] if r["kind"] == "file")
    assert "dataset" not in row
    assert row["evidence"] == "dynamic"
    assert bound["jclBinding"]["boundFiles"] == 0


def test_the_binder_takes_a_plain_dict_and_does_not_mutate_it():
    lineage = json.loads(
        (FIXTURES / "paycalc.jcl.lineage.json").read_text(encoding="utf-8"))
    manifest = build_asm_artifacts(parse_asm(SAMPLE))
    before = json.dumps(manifest, sort_keys=True)
    bind_jcl_ddnames(manifest, lineage)
    assert json.dumps(manifest, sort_keys=True) == before


def test_the_manifest_conforms_to_the_written_core():
    """Upstream ledger batch 10, item 31: the shared row vocabulary is written down in
    mainframe-artifacts, so this package checks itself against the contract."""
    from mainframe_artifacts.manifest import validate_manifest
    from asm_dependencies.parser import parse_asm
    from asm_dependencies.views import build_asm_artifacts
    from pathlib import Path

    examples = Path(__file__).resolve().parents[1] / "examples"
    for path in sorted(examples.glob("*.asm")):
        module = parse_asm(path.read_text(), source_name=path.name)
        assert validate_manifest(build_asm_artifacts(module)) == [], path.name


# --- storage: the labelled DC/DS items the module declares -------------------------

def _storage(*rows, resolver=None):
    return build_asm_lineage(parse_asm(module(*rows), resolver=resolver))["storage"]


def test_a_csect_publishes_each_labelled_storage_item_with_its_section():
    rows = _storage(("PAYCALC", "CSECT"), ("WORKA", "DS", "CL8"), ("WORKB", "DS", "F"),
                    ("", "END"))
    assert rows == [
        {"field": "WORKA", "line": 2, "operand": "CL8", "section": "PAYCALC"},
        {"field": "WORKB", "line": 3, "operand": "F", "section": "PAYCALC"},
    ]


def test_a_dsect_publishes_its_items_under_the_dsect():
    """A mapping DSECT is where a caller's parameter block is described."""
    rows = _storage(("PAYCALC", "CSECT"), ("PARMS", "DSECT"), ("PARMKEY", "DS", "CL6"),
                    ("PARMAMT", "DS", "PL5"), ("", "END"))
    assert [(r["field"], r["section"], r["operand"]) for r in rows] == [
        ("PARMKEY", "PARMS", "CL6"), ("PARMAMT", "PARMS", "PL5")]


def test_an_unlabelled_ds_is_not_an_addressable_item_and_is_not_published():
    rows = _storage(("P", "CSECT"), ("", "DS", "CL4"), ("KEEP", "DS", "CL2"), ("", "END"))
    assert [r["field"] for r in rows] == ["KEEP"]


def test_a_table_of_constants_publishes_every_value():
    rows = _storage(("P", "CSECT"), ("PGMTAB", "DC", "CL8'PAY001',CL8'PAY002'"),
                    ("", "END"))
    assert rows[0]["values"] == ["PAY001  ", "PAY002  "]


def test_a_storage_item_without_a_value_carries_no_values_key():
    """Presence-only, like the family's other optional keys: a DS initialises nothing."""
    rows = _storage(("P", "CSECT"), ("WORK", "DS", "CL8"), ("", "END"))
    assert "values" not in rows[0] and "inMember" not in rows[0]


def test_a_copied_storage_item_names_the_member_it_came_from():
    """The same `inMember` the section rows and every touch carry: the item is declared
    in the member, which is the identity a second module COPYing it shares."""
    lib = {"PAYPARM": module(("PARMKEY", "DS", "CL6"))}
    rows = _storage(("P", "CSECT"), ("", "COPY", "PAYPARM"), ("", "END"),
                    resolver=lambda name: lib.get(name.upper()))
    assert rows == [{"field": "PARMKEY", "line": 1, "operand": "CL6", "section": "P",
                     "inMember": "PAYPARM"}]


def test_a_zero_length_label_is_published_as_written_not_reinterpreted():
    """`PAYINIT DS 0H` is an entry point's label, not storage. The row says `0H` and
    nothing more: deciding which zero-length labels are code would be a guess."""
    rows = _storage(("P", "CSECT"), ("", "ENTRY", "PAYINIT"), ("PAYINIT", "DS", "0H"),
                    ("", "END"))
    assert rows == [{"field": "PAYINIT", "line": 3, "operand": "0H", "section": "P"}]


def test_a_module_with_no_labelled_storage_publishes_an_empty_list():
    """Every list in this view is present when empty - the parser always looked, so an
    empty list is the answer "none declared", not "not asked"."""
    assert _storage(("P", "CSECT"), ("", "LINK", "EP=X"), ("", "END")) == []
    assert "storage" in build_asm_lineage(parse_asm(module(("P", "CSECT"), ("", "END"))))
