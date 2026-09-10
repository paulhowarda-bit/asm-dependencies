"""The command line, the retrieval closure against a fake estate, and the contract check."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fakes.estate import fetch_artifact

from asm_dependencies.cli import run
from asm_dependencies.parser import parse_asm
from asm_dependencies.prefetch import prefetch_asm
from asm_dependencies.views import build_asm_artifacts

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
TOOLS = REPO / "tools"


def written(out_dir):
    return sorted(p.name for p in Path(out_dir).iterdir() if p.suffix == ".json")


# --- the command line ---------------------------------------------------------------

def test_a_default_run_writes_both_views_and_both_retrieval_reports(tmp_path):
    rc = run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q",
              "--no-fetch"])
    assert rc == 0
    assert written(tmp_path) == ["paycalc.asm.artifacts.json", "paycalc.asm.fetch.json",
                                 "paycalc.asm.lineage.json", "paycalc.asm.prefetch.json"]


def test_a_default_run_writes_no_dependents_view(tmp_path):
    """Nobody was asked, so nothing is said - not even an empty answer."""
    run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q", "--no-fetch"])
    assert "paycalc.asm.dependents.json" not in written(tmp_path)


def test_a_dependents_map_is_written_as_its_own_view(tmp_path):
    dep = tmp_path / "dependents.json"
    dep.write_text(json.dumps({
        "PAYCALC|program": [{"name": "BILLRUN", "kind": "MODULE", "via": "CALL",
                             "match_strength": "qualified"}]}), encoding="utf-8")
    rc = run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q",
              "--no-fetch", "--dependents-map", str(dep)])
    assert rc == 0
    view = json.loads((tmp_path / "paycalc.asm.dependents.json").read_text(
        encoding="utf-8"))
    assert view["format"] == "asm-dependencies-dependents"
    named = [row for row in view["entries"] if row["name"] == "PAYCALC"]
    assert [d["name"] for d in named[0]["dependents"]] == ["BILLRUN"]
    # ...and the other two views are untouched by it.
    assert "dependents" not in json.loads(
        (tmp_path / "paycalc.asm.artifacts.json").read_text(encoding="utf-8"))


def test_a_dependents_map_that_will_not_open_is_an_operator_error(tmp_path):
    rc = run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q",
              "--no-fetch", "--dependents-map", str(tmp_path / "missing.json")])
    assert rc == 2


def test_target_narrows_what_is_written(tmp_path):
    run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q", "--no-fetch",
         "--target", "artifacts"])
    assert "paycalc.asm.lineage.json" not in written(tmp_path)


def test_bind_jcl_forces_the_artifacts_view_and_closes_the_ddnames(tmp_path):
    rc = run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q",
              "--no-fetch", "--target", "lineage",
              "--bind-jcl", str(REPO / "tests" / "fixtures" / "paycalc.jcl.lineage.json")])
    assert rc == 0
    view = json.loads((tmp_path / "paycalc.asm.artifacts.json").read_text())
    row = next(r for r in view["artifacts"] if r.get("ddname") == "CUSTMAST")
    assert row["dataset"] == "PROD.CUSTOMER.MASTER"


def test_sysparm_collapses_the_conditional_branches(tmp_path):
    run([str(EXAMPLES / "siteenv.asm"), "--outdir", str(tmp_path), "-q", "--no-fetch"])
    both = json.loads((tmp_path / "siteenv.asm.artifacts.json").read_text())
    run([str(EXAMPLES / "siteenv.asm"), "--outdir", str(tmp_path), "-q", "--no-fetch",
         "--sysparm", "PROD"])
    prod = json.loads((tmp_path / "siteenv.asm.artifacts.json").read_text())

    assert {r["artifact"] for r in both["artifacts"]} > \
           {r["artifact"] for r in prod["artifacts"]}
    assert all(r.get("conditional") for r in both["artifacts"])
    assert not any(r.get("conditional") for r in prod["artifacts"])
    assert "TESTAUTH" not in {r["artifact"] for r in prod["artifacts"]}


def test_a_missing_source_is_an_operator_error_not_a_crash(tmp_path):
    assert run([str(tmp_path / "nope.asm"), "--outdir", str(tmp_path), "-q"]) == 2


def test_gather_only_and_from_bundle_cannot_both_apply(tmp_path):
    assert run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path), "-q",
                "--gather-only", str(tmp_path / "b"),
                "--from-bundle", str(tmp_path / "b")]) == 2


def test_a_bundle_round_trip_needs_no_estate_at_all(tmp_path):
    """Gather where the estate is reachable, model where it is not - and the offline run
    is the same code with a different fetcher, not a second code path."""
    bundle = tmp_path / "bundle"
    rc = run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path / "g"), "-q",
              "--gather-only", str(bundle),
              "--fetcher", "fakes.estate:fetch_artifact"])
    assert rc == 0
    assert (bundle / "estate-bundle.json").is_file()

    rc = run([str(EXAMPLES / "paycalc.asm"), "--outdir", str(tmp_path / "o"), "-q",
              "--from-bundle", str(bundle)])
    assert rc == 0
    assert written(tmp_path / "o") == ["paycalc.asm.artifacts.json",
                                       "paycalc.asm.fetch.json",
                                       "paycalc.asm.lineage.json",
                                       "paycalc.asm.prefetch.json"]


def test_stdin_is_accepted(tmp_path, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO((EXAMPLES / "paycalc.asm").read_text()))
    assert run(["-", "--outdir", str(tmp_path), "-q", "--no-fetch"]) == 0
    assert "PAYCALC.asm.artifacts.json" in written(tmp_path)


def test_a_non_assembler_member_warns_but_still_runs(tmp_path, caplog):
    jcl = tmp_path / "job.jcl"
    jcl.write_text("//PAYRUN   JOB (ACCT),'X'\n//S1  EXEC PGM=IEFBR14\n")
    assert run([str(jcl), "--outdir", str(tmp_path), "-q", "--no-fetch"]) == 0


# --- the retrieval closure ------------------------------------------------------------

def test_the_closure_follows_a_macro_that_invokes_a_macro():
    """SITEIO's call to SITELINK is invisible until SITEIO's own text is in hand, so this
    needs a second round - the thing a lexical scan cannot do."""
    source = (
        "IOMOD    CSECT\n"
        "         SITEIO DD=CUSTMAST\n"
        "         END   IOMOD\n"
    )
    pre = prefetch_asm(source, fetch_artifact, source_name="iomod.asm", jobs=1)
    rows = {r["member"]: r for r in pre.report()["members"]}
    assert {"SITEIO", "SITELINK", "CUSTREC"} <= set(rows)

    module = parse_asm(source, resolver=pre.resolver(), source_name="iomod.asm")
    names = {r["artifact"] for r in build_asm_artifacts(module)["artifacts"]}
    # IOINIT is named only inside SITELINK, which is named only inside SITEIO.
    assert "IOINIT" in names
    assert "CUSTMAST" in names


def test_a_failed_request_is_not_reported_as_an_absent_member():
    source = "M        CSECT\n         BOOM\n         END\n"
    pre = prefetch_asm(source, fetch_artifact, source_name="m.asm", jobs=1)
    row = next(r for r in pre.report()["members"] if r["member"] == "BOOM")
    assert row["status"] == "error"


def test_a_copy_member_beside_the_module_is_found_without_an_estate():
    source = (EXAMPLES / "paycalc.asm").read_text()
    pre = prefetch_asm(source, None, paths=[str(EXAMPLES)], source_name="paycalc.asm",
                       jobs=1)
    row = next(r for r in pre.report()["members"] if r["member"] == "PAYREC")
    assert row["status"] == "local"


# --- the cross-repository contract ------------------------------------------------------

def test_the_committed_jcl_fixture_still_has_the_shape_the_binder_reads():
    """An unbound manifest looks EXACTLY like one nobody tried to bind, so drift here is
    silent unless something asserts it."""
    proc = subprocess.run([sys.executable, str(TOOLS / "refresh_fixture.py"), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_examples_are_reproducible_from_their_generator(tmp_path):
    """The fixtures for a column language are worthless if the columns are approximate, so
    they are generated - and a hand-edit that drifts from the generator is a red test."""
    before = {p.name: p.read_bytes() for p in EXAMPLES.iterdir()}
    proc = subprocess.run([sys.executable, str(TOOLS / "make_examples.py")],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    after = {p.name: p.read_bytes() for p in EXAMPLES.iterdir()}
    assert before == after


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.asm")))
def test_no_example_line_reaches_the_continuation_column(path):
    """A comment whose prose reaches column 72 swallows the next statement. It is what the
    assembler does, and it is not what any of these examples means."""
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        assert not (len(line) > 71 and line[71:72].strip()), \
            "{0}:{1} reaches the continuation column".format(path.name, number)
