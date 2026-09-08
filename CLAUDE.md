# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## What this is

Parses IBM assembler (HLASM) and extracts dependencies: the entry points a module provides,
the modules it calls or loads, the ddnames it declares, and the COPY members and macros it
is assembled from — each row carrying **how the name was established**. See README.md for
the user-facing description.

## Setup

The one dependency, `mainframe-artifacts`, ships from the **mainframe-common** repository
and is normally a sibling checkout rather than an install:

```
code/
  mainframe-common/mainframe-artifacts/    <- the dependency
  jcl-dependencies/                        <- peer (and the JCL fixture's source)
  eztrieve-dependencies/                   <- peer
  asm-dependencies/                        <- here
```

`tests/_mainframe_common.py` puts the sibling's `src` on `sys.path` when the distribution
is not installed; override the location with `MAINFRAME_COMMON_REPO`. When neither is
found, `tests/conftest.py` ignores every module except `test_sibling_distribution.py`, so
the run ends as one clean skip naming the pip command instead of a wall of collection
errors. `tools/byteproof.py` performs the same discovery.

No install is needed to run the suite — `pyproject.toml` sets `pythonpath = ["src",
"tests"]`.

## Commands

```bash
python -m pytest -q                                     # the suite
python -m pytest tests/test_conditional.py -q            # one file
python -m pytest -q -k "decoy or dispatch"               # by name

python tools/byteproof.py --check goldens/views.sha256   # byte-stability ratchet
python tools/byteproof.py --record goldens/views.sha256  # re-record (deliberately only)
python tools/refresh_fixture.py --check                  # the cross-repo JCL fixture
python tools/make_examples.py                            # regenerate examples/
python -m pyflakes src/asm_dependencies/*.py tools/*.py tests/*.py
```

Running the tool itself. `pyproject.toml`'s `pythonpath` applies to **pytest only**, so a
bare `python -m asm_dependencies` fails with `No module named` unless the package is
installed or the path is given explicitly:

```bash
# installed (also gives you the `asm-dependencies` console script)
python -m pip install -e ../mainframe-common/mainframe-artifacts -e .

# or, without installing anything - note `;` is the separator on Windows, `:` elsewhere
export PYTHONPATH="src;tests;../mainframe-common/mainframe-artifacts/src"
```

Then (`--no-fetch` because the real estate client is not installed here; a COPY member is
found automatically beside the module, `-I DIR` adds more places to look):

```bash
python -m asm_dependencies examples/paycalc.asm --outdir ./out --no-fetch --summary
python -m asm_dependencies examples/siteenv.asm --outdir ./out --no-fetch --sysparm PROD
python -m asm_dependencies examples/paycalc.asm --outdir ./out --no-fetch \
    --bind-jcl tests/fixtures/paycalc.jcl.lineage.json
python -m asm_dependencies examples/paycalc.asm --outdir ./out --jobs 1 \
    --fetcher fakes.estate:fetch_artifact          # needs tests/ on the path
```

`tests/fakes/estate.py` is the deterministic stand-in for the estate service.

**Verify from a fresh clone, not only in place.** The ratchet cannot catch a
checkout-dependent difference in the directory the goldens were recorded in. Clone the repo
to a temp dir and run `pytest` plus `byteproof --check` there before trusting a change.

## Architecture

One pipeline, each stage ignorant of the next:

```
lexer.py        physical text -> statements with fields split
                (columns 1-71, ICTL-settable; col-72 continuation resuming at col 16;
                 * and .* comments; the operand ends at the first blank outside quotes
                 and parens)
opcodes.py      the operation-code vocabulary: directive, machine instruction, or macro
conditional.py  SETA/SETB/SETC, AIF/AGO, &SYSPARM - which statements exist at all
macros.py       COPY + MACRO/MEND + library macros, via the caller's resolver
model.py        statements -> Module (sections, entries, externals, invocations, files,
                subsystem resources)
parser.py       the statement handlers - assembler directives, the IBM program- and
                data-management macros, and EXEC CICS / EXEC SQL / EXEC DLI - plus the
                second pass that decides what the A-cons meant
dataflow.py     EPLOC=FIELD -> the DC that fills it, and the store check that undoes it
classify.py     which names are IBM runtime services and must not be chased
views.py        Module -> the two JSON views + the JCL join
```

`api.py` wires prefetch → parse → views → fetch; `cli.py` is a thin front end over it.
Nothing below `views.py` knows about JSON, and nothing above `parser.py` decides evidence.

### Invariants that span files

**Four peer packages, no imports between them.** `cobol_xstate` says what a program does,
`jcl_dependencies` says which dataset a ddname is, `eztrieve_dependencies` says which bytes
become which, and this says what a module calls and what it can be called *as*. They share
only `mainframe_artifacts` and meet at plain dicts. `tests/test_boundaries.py` runs child
interpreters with those packages blocked *and* greps every module's import lines — and
`test_the_module_inventory_is_complete` fails if a new module is added without being listed,
because the parametrize list is hand-maintained and an unlisted module is silently
unchecked.

**The resolver is the sole route to an external member.** `prefetch.py` closes over a
module's COPY members and macros by *replaying the parse* under a recording resolver until
it stops asking. That works only while `macros.MacroExpander._resolve` is the one call that
reaches outside. Memoizing resolution inside the expander, short-circuiting when the
resolver returns `None`, or adding a second resolution path **silently shortens the
closure** — no error, just a module that reads as though it had fewer dependencies than it
has. A site macro routinely carries every call the module makes, so a short closure
produces an empty manifest that still looks finished.

**COPY members close before macros do.** `prefetch_asm` runs its first rounds with
`resolve_macros=False`. The order is load-bearing: a COPY member routinely carries the
shop's linkage macros, so asking for `SITELINK` while `COPY PAYREC` is still unresolved
gets a not-found for a macro that PAYREC defines inline one round later — and that
not-found is permanent, because a member is only ever asked for once.

**Definitions come out before conditional assembly runs.** `MacroExpander._walk` calls
`_definitions_out` and only then `conditional.evaluate`. An `AIF` inside a macro body is
decided when the macro is *invoked*, with its parameters bound; evaluating it at definition
time decides it against an empty environment and drops the branch the call actually selects.

**`None` propagates through the conditional-assembly evaluator.** An unknown operand makes
the whole test unknown. Guessing one decides a branch, and a wrongly decided branch produces
no flag — just a manifest quietly missing everything on the other side. The two deliberate
exceptions are the short-circuits: `0 AND unknown` is 0 and `1 OR unknown` is 1.

**Evidence and `conditional` answer different questions.** Evidence says how well the name
is known (`literal` / `assigned` / `table` / `dynamic`); `conditional` says whether the
statement is assembled at all. Folding them into one vocabulary loses the case that occurs
most: a perfectly known name in a branch that may not exist.

**A store demotes a literal.** `dataflow._fields` is the decoy check. `PGMNAME DC
CL8'PAYCALC'` plus `MVC PGMNAME,SELECTED` means the constant is not what the field holds at
the call. Reporting `PAYCALC` there is not merely incomplete — it is confidently wrong,
which is worse, because nothing about the output invites a second look.

**An EXEC command is lexed by different rules, and the lexer owns them.** `EXEC CICS` and
`EXEC SQL` are a preprocessor's language in the operand field: blank-separated operands, no
remarks field, and (for CICS) a continue column of 2 rather than 16. `lexer.is_exec` and
`_exec_operand` are where that lives, so the parser never has to know about columns. Split
the assembler's way, an `EXEC CICS LINK PROGRAM('X')` has the operand `CICS` and the rest is
a remark.

**A CICS argument's quoting is its evidence.** IBM's own rule - literal names in quotes,
data areas without - so `PROGRAM('PAYCALC')` is `literal` and `PROGRAM(PGMNAME)` is
`dynamic`. Do not "improve" this by resolving the data area from a `DC`: what a CICS
COMMAREA-fed field holds is not decidable from this module.

**`terminal-map` and `cics-transaction` carry no `io`.** Same as `program`, `proc` and
`macro` elsewhere in the family: SEND and RECEIVE describe the terminal conversation, not a
direction of access to the load module. Giving them one makes a consumer's `io` rule mean
two different things. `db2-table` uses the JCL side's `read+write` and every other kind uses
the Easytrieve `file` row's `read-write` - two spellings of one idea in one output family,
kept per KIND so a consumer's rule is the same wherever a row of that kind came from.

**An unresolved IBM macro is a narrower gap than an unresolved site macro.** `DFHEIENT`
generates CICS prologue code; it does not hide the shop's calls. `macros.py` asks
`classify.subsystem` before wording the flag, because "any CALL it expands to is missing"
would send a reader looking for a dependency that was never there.

**An IBM macro and a site macro are different kinds of gap.** `classify` knows the
SYS1.MACLIB names (`WTO`, `GETMAIN`, `PUT`, `RETURN`, `YREGS`, …) as well as the CICS/Db2/
IMS entry points. They are excluded from the manifest and get the narrower flag, because a
module assembled without its macro libraries otherwise reports 400 missing IBM macros that
bury the handful of SITE macros - and those are the ones actually hiding the shop's calls.
Measured on IBM's IFOX source: 78 of the flags in ten modules were SYS1.MACLIB names.

**An empty `provides` needs an explanation.** A module with no visible control section AND
unresolved macros almost certainly declares its structure through one of them - IFOX uses
`JCSECT (X0A00)` and `JENTRY (X0A01=START)`. `_report_hidden_structure` says so, and names
the `END` operand when there is one, because the provides index is half the estate's call
graph and a module silently contributing nothing to it is a hole in the graph rather than
a gap in one manifest.

**Candidates are not artifacts.** A dispatch table's entries go in the manifest's
`candidates` list, never in `artifacts`. Putting them in `artifacts` overclaims; leaving
them out entirely gives a table-driven module an empty manifest, which is indistinguishable
from a module that genuinely calls nothing.

**The subject key is spelled `program`.** `mainframe_artifacts.fetch` reads
`manifest.get("program") or manifest.get("job")` to know what not to fetch. Keyed anything
else — `module`, say — the guard holds `"?"` and the module requests *itself* from the
estate as its own dependency.

## Output is the contract

Every byte of both views is output. Key order, list order, the presence of a reason, the
wording of a flag — none of it is asserted by the test suite, so `tools/byteproof.py` is
what actually guards it. It hashes every view of every example under two `PYTHONHASHSEED`
values **and** under both `&SYSPARM` settings, because conditional assembly makes those two
runs genuinely different output and a change that collapsed them would otherwise be
invisible. Re-record goldens only when a change is intended and reviewed.

`.gitattributes` pinning `eol=lf` is load-bearing for this, not cosmetic: a member resolved
from the local search path is read as bytes and its length goes into the retrieval report,
so a CRLF checkout shifts every one of those counts.

`examples/` is **generated** by `tools/make_examples.py`, and `test_cli.py` fails if the
committed files drift from it. The fixtures for a column language are worthless if the
columns are approximate, and hand-spacing them to column 72 does not survive editing.

## Honesty discipline

Nothing is guessed. Anything unresolved is surfaced rather than smoothed over — unresolved
macros and COPY members, computed member names, undecided `AIF` branches, register-form
operands, overwritten name fields, backward conditional branches, text past the margin, a
comment that swallowed a statement. When adding a feature, the question to answer is "what
does this tool *not* know here, and does the output say so?" A flag naming a gap is worth
more than a plausible value.

New IBM macros go in `opcodes.IBM_MACROS` **and** get a `_Parser._do_<name>` handler.
Listing one without a handler makes it silently unmodelled with no flag; leaving one out
entirely makes the expander chase it through the macro libraries as if it were a site macro.

New machine mnemonics go in `opcodes._ARITHMETIC` or `_BRANCHES`. The rule when in doubt is
to leave a mnemonic **out**: an unknown operation becomes a macro candidate, is looked for,
is not found, and is flagged — noisy but never wrong. The reverse error, listing a *macro*
name as a machine instruction, makes a real call silently invisible.

Adding a new artifact `kind` to the manifest requires a matching entry in
`mainframe_artifacts.fetch._KIND_TYPE` (and `artifact_service.EXT_FOR_TYPE`) in the
mainframe-common repo — otherwise stage 2 reports it `skipped: no known retrieval type`,
which is false for anything stage 1 already retrieved. The kinds used here (`program`,
`file`, `copybook`, `macro`) are all already registered, as are `CATEGORY_ASM` and
`EXT_FOR_TYPE["asm"]`.
