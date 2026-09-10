"""The reverse direction: what depends on this module, joined to its entry points.

The assembler case is the one that makes this worth a contract. A call to an entry point
whose name differs from the member name is otherwise unresolvable - "nothing extracts a
program name from that kind" - even when a host index holds the answer. So the join here
is onto the ENTRY POINT, not the module, and the three answers the lookup can give
(nobody asked, asked and nothing, the lookup broke) stay three answers all the way out to
the written view.
"""

import json

from mainframe_artifacts.dependents import DependentsLookup

from _source import module

from asm_dependencies.api import analyze
from asm_dependencies.parser import parse_asm
from asm_dependencies.views import FORMAT_DEPENDENTS, build_asm_dependents

SAMPLE = module(
    ("PAYCALC", "CSECT"),
    ("", "ENTRY", "PAYINIT"),
    ("PAYINIT", "DS", "0H"),
    ("", "END", "PAYCALC"),
)

#: What a host index holds: two modules reach PAYINIT, the entry point, and neither names
#: the member PAYCALC. This is the case the item was written about.
_INDEX = {
    "PAYINIT|program": [
        {"name": "BILLRUN", "kind": "MODULE", "via": "CALL",
         "match_strength": "qualified", "detail": "BILLRUN calls PAYINIT at offset 0x1C"},
        {"name": "ACCTPOST", "kind": "MODULE", "via": "LINK",
         "match_strength": "qualified"},
    ],
    "PAYCALC|program": [],
}


def _view(mapping=None, resolver=None):
    lookup = DependentsLookup(mapping, resolver)
    return build_asm_dependents(parse_asm(SAMPLE, source_name="paycalc.asm"), lookup)


# --- the join, and the family shape --------------------------------------------------

def test_dependents_attach_to_the_entry_point_that_was_named():
    view = _view(_INDEX)
    by_name = {row["name"]: row for row in view["entries"]}
    assert sorted(by_name) == ["PAYCALC", "PAYINIT"]
    # The entry point carries the callers; the member name carries none, and says so by
    # being present with an empty list rather than by being missing.
    assert [d["name"] for d in by_name["PAYINIT"]["dependents"]] == ["ACCTPOST", "BILLRUN"]
    assert by_name["PAYINIT"]["kind"] == "entry"
    assert by_name["PAYCALC"]["dependents"] == []
    assert by_name["PAYCALC"]["count"] == 0


def test_the_view_has_the_family_keys_in_the_family_order():
    view = _view(_INDEX)
    assert list(view) == ["format", "formatVersion", "program", "source", "note",
                          "suppliedBy", "entries", "unanswered", "flags"]
    assert view["format"] == FORMAT_DEPENDENTS
    assert view["program"] == "PAYCALC"


def test_a_row_is_camelcased_and_carries_both_kinds_and_the_strength():
    row = [d for d in _view(_INDEX)["entries"]
           if d["name"] == "PAYINIT"][0]["dependents"][0]
    assert row == {"name": "ACCTPOST", "kind": "MODULE", "manifestKind": "program",
                   "via": "LINK", "matchStrength": "qualified"}


def test_rows_are_sorted_so_the_hosts_order_cannot_change_the_bytes():
    forwards = _view(_INDEX)
    backwards = _view({"PAYINIT|program": list(reversed(_INDEX["PAYINIT|program"])),
                       "PAYCALC|program": []})
    assert json.dumps(forwards) == json.dumps(backwards)


# --- the three answers, all the way out ----------------------------------------------

def test_no_lookup_supplied_is_no_view_at_all():
    parsed = parse_asm(SAMPLE, source_name="paycalc.asm")
    assert build_asm_dependents(parsed, None) is None
    assert build_asm_dependents(parsed, DependentsLookup()) is None


def test_a_name_the_index_does_not_cover_is_unanswered_not_empty():
    view = _view({"PAYINIT|program": _INDEX["PAYINIT|program"]})
    assert [row["name"] for row in view["entries"]] == ["PAYINIT"]
    # PAYCALC is NOT reported as having no dependents: nobody said anything about it.
    assert view["unanswered"] == [
        {"name": "PAYCALC", "reason": "the lookup does not cover this name"}]


def test_a_lookup_that_breaks_leaves_the_rest_unanswered_and_flagged():
    calls = []

    def boom(name, kind=None):
        calls.append(name)
        raise RuntimeError("index unreachable")

    view = _view(resolver=boom)
    assert view["entries"] == []
    assert [row["name"] for row in view["unanswered"]] == ["PAYCALC", "PAYINIT"]
    assert view["unanswered"][1]["reason"].startswith("the lookup failed earlier")
    assert calls == ["PAYCALC"]                       # asked once, then disabled
    assert "index unreachable" in view["flags"][0]
    assert "fix the lookup and re-run" in view["flags"][0]


def test_a_reported_fan_out_cap_reaches_the_view():
    def capped(name, kind=None):
        if name != "PAYINIT":
            return None
        return {"rows": _INDEX["PAYINIT|program"], "truncated": True, "total": 9182}

    row = [r for r in _view(resolver=capped)["entries"] if r["name"] == "PAYINIT"][0]
    assert (row["truncated"], row["total"], row["count"]) == (True, 9182, 2)


def test_the_view_says_which_door_answered():
    assert _view(_INDEX)["suppliedBy"].startswith("map (")
    assert _view(resolver=lambda name, kind=None: [])["suppliedBy"] == "resolver"


# --- through analyze() ---------------------------------------------------------------

def test_analyze_without_the_parameter_is_the_run_it_always_was():
    analysis = analyze(SAMPLE, source_name="paycalc.asm", retrieve=False)
    assert analysis.dependents_lookup is None
    assert analysis.dependents() is None


def test_analyze_asks_the_lookup_and_memoises_the_view():
    calls = []

    def index(name, kind=None):
        calls.append((name, kind))
        return _INDEX.get("{0}|program".format(name))

    analysis = analyze(SAMPLE, source_name="paycalc.asm", retrieve=False,
                       dependents_resolver=index)
    view = analysis.dependents()
    assert view is not None
    assert analysis.dependents() is view              # memoised, not re-asked
    assert sorted(calls) == [("PAYCALC", "program"), ("PAYINIT", "program")]


def test_the_map_alone_is_a_door():
    analysis = analyze(SAMPLE, source_name="paycalc.asm", retrieve=False,
                       dependents=_INDEX)
    entries = analysis.dependents()["entries"]
    assert [row["name"] for row in entries] == ["PAYCALC", "PAYINIT"]


# --- the bundle ----------------------------------------------------------------------

def test_a_gathered_bundle_replays_the_reverse_direction(tmp_path):
    from mainframe_artifacts.bundle import open_bundle

    from asm_dependencies.api import gather

    def index(name, kind=None):
        return _INDEX.get("{0}|program".format(name))

    live = analyze(SAMPLE, source_name="paycalc.asm", retrieve=False,
                   dependents_resolver=index).dependents()
    root = tmp_path / "bundle"
    gather(SAMPLE, source_name="paycalc.asm", dest=str(root), dependents_resolver=index)

    bundle = open_bundle(root)
    assert bundle.has_dependents()
    # No lookup passed at all: the bundle supplies it, exactly as it supplies members.
    replayed = analyze(bundle.source(), source_name="paycalc.asm",
                       bundle=bundle).dependents()
    assert replayed == live


def test_a_bundle_gathered_without_a_lookup_replays_no_view(tmp_path):
    from mainframe_artifacts.bundle import open_bundle

    from asm_dependencies.api import gather

    root = tmp_path / "bundle"
    gather(SAMPLE, source_name="paycalc.asm", dest=str(root))
    bundle = open_bundle(root)
    assert bundle.has_dependents() is False
    assert analyze(bundle.source(), source_name="paycalc.asm",
                   bundle=bundle).dependents() is None
