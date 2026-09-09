"""Db2 SYNONYM/ALIAS knowledge reaches the `db2-table` rows.

Upstream ledger batch 10, item 34. `db2-table` is a first-class manifest kind in this
package, and `views.py`'s identity note had clearly thought about qualification - but a
synonym is the adjacent case and got no mention, so a consumer reading these rows
reasonably concluded the name each holds is a table. On a real estate this is not rare:
about one table name in five an assembler module reaches is a SYNONYM, not a table.

The other three front-ends already take this knowledge as input through the same two
shared doors, and a family where one member's `db2-table` rows mean something different
from the rest is exactly what `_merge_db2_io`'s docstring says it is trying to avoid.
"""

from pathlib import Path

from asm_dependencies.api import analyze
from asm_dependencies.views import build_asm_artifacts
from asm_dependencies.parser import parse_asm

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
#: Its EXEC SQL names PRODDB.CUSTOMER.
SOURCE = (EXAMPLES / "custinq.asm").read_text()


def _tables(manifest: dict) -> dict:
    return {r["artifact"]: r for r in manifest["artifacts"] if r["kind"] == "db2-table"}


def _analysis(**kw):
    return analyze(SOURCE, source_name="custinq.asm", retrieve=False, **kw)


def test_without_the_doors_every_table_is_reported_as_written():
    """The default must not change. No map, no resolver, no baseTable - and that is
    honest rather than incomplete: nothing has told this package otherwise."""
    row = _tables(_analysis().artifacts())["PRODDB.CUSTOMER"]
    assert "baseTable" not in row and "resolvedVia" not in row


def test_a_synonym_map_resolves_the_base_table_and_keeps_the_written_name():
    """The name as written stays the artifact.

    A departure from the ledger, which asked for the resolved table AS the artifact.
    Following that literally would have made an asm `db2-table` row mean something
    different from a JCL or Easytrieve one, which is the divergence item 34 exists to
    close - both of those keep the written name and add `baseTable`.
    """
    a = _analysis(synonyms={"PRODDB.CUSTOMER": "PRODDB.T_BASE_TABLE"})
    row = _tables(a.artifacts())["PRODDB.CUSTOMER"]
    assert row["artifact"] == "PRODDB.CUSTOMER"
    assert row["baseTable"] == "PRODDB.T_BASE_TABLE"
    assert row["resolvedVia"] == "synonym map"


def test_a_host_resolver_is_the_other_door_and_says_so():
    a = _analysis(synonym_resolver=lambda n: "PRODDB.T_BASE_TABLE"
                  if n == "PRODDB.CUSTOMER" else None)
    row = _tables(a.artifacts())["PRODDB.CUSTOMER"]
    assert row["baseTable"] == "PRODDB.T_BASE_TABLE"
    assert row["resolvedVia"] == "catalog resolver"


def test_a_name_that_is_not_a_synonym_is_left_alone():
    """Not-a-synonym and no-knowledge must stay distinguishable from each other only by
    what the caller supplied, never by inventing a base."""
    a = _analysis(synonyms={"SOMETHING.ELSE": "PRODDB.T_OTHER"})
    row = _tables(a.artifacts())["PRODDB.CUSTOMER"]
    assert "baseTable" not in row


def test_a_resolver_that_raises_is_a_flagged_failure_not_a_not_a_synonym():
    """A failed lookup is never silently read as 'this is a table'."""
    def boom(name):
        raise RuntimeError("catalog unreachable")

    a = _analysis(synonym_resolver=boom)
    manifest = a.artifacts()
    assert "baseTable" not in _tables(manifest)["PRODDB.CUSTOMER"]
    assert any("synonym resolver failed" in f for f in manifest["flags"]), manifest["flags"]


def test_the_identity_note_stops_calling_a_synonym_a_table():
    """The cheaper half of the ask, done as well as the parameter: a consumer reading the
    note must not conclude the name it holds is always a table."""
    row = _tables(build_asm_artifacts(
        parse_asm(SOURCE, source_name="custinq.asm")))["PRODDB.CUSTOMER"]
    assert "SYNONYM" in row["needs"] and "baseTable" in row["needs"]
