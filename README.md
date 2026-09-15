# asm-dependencies

Parse IBM mainframe assembler (HLASM) and recover what a module **provides**, what it
**calls**, what it **opens** — and, for every name the source does not actually contain,
say so rather than leaving a gap that reads like an absence.

The COBOL says what a program does. The JCL says which dataset a ddname is. Assembler sits
under both: it is where ddnames are *declared*, where the estate's utility subroutines
live, and where a module states what it can be called **as** — the half of the call graph
no other language in this family emits.

## Why it is not a fourth flavour of the same problem

An assembler module's dependency set **is not determined by its source**. Three separate
reasons, and they shape everything here:

**Conditional assembly decides which statements exist.** A `COPY` or a `LINK` can sit
inside an `AIF` branch gated on `&SYSPARM`, and `&SYSPARM` arrives as `PARM=` on the
*assemble step's JCL*:

```
&SITE    SETC  '&SYSPARM'
         AIF   ('&SITE' EQ 'PROD').PROD
         COPY  TESTCFG
         AGO   .DONE
.PROD    ANOP
         COPY  PRODCFG
.DONE    ANOP
```

Reporting both copybooks flatly claims the module depends on both, which is false for
every real assembly. Picking one is worse — a guess presented as a fact. So both are
reported **with the test that governs them**, and `--sysparm PROD` collapses the question
to a precise answer.

**The real calls are behind site macros.** Large shops wrap `LINK`/`XCTL`/`LOAD` in local
macros, so `@LINK PGM=PAYCALC` is a normal line of production assembler. A module parsed
without its macro libraries has *no calls at all*, and its manifest looks exactly like the
manifest of a module that genuinely calls nothing. Worse, you cannot scan for macro calls:
they have no marker. `SITELINK PGM=X` and `LR R15,R0` are distinguished only by whether
the operation code is in the assembler's tables — so the closure replays the parse rather
than scanning.

**Many names are computed at run time.** `LINK EPLOC=(R1)` names a register. And a
`DC CL8'PAYCALC'` that gets `MVC`'d over before the call is a **decoy**: the literal is
right there, and it is not what the field holds. A tool that reports it confidently is
worse than one that reports nothing, because nothing about the output invites a second
look.

## So every row says how it was established

| `evidence` | meaning |
|---|---|
| `literal` | the name is a token in the statement — `LINK EP=PAYCALC` |
| `assigned` | the operand names a field a `DC` initialises, and no store into it reaches the site |
| `table` | the name is one of a set of constants selected at run time — the set is known, the choice is not |
| `dynamic` | computed at run time; nothing in the source names it |

`conditional` is kept **separate** from that grading, because it answers a different
question: evidence says how well the name is known, `conditional` says whether the
statement is assembled at all. A literal name inside an undecided `AIF` branch is both
perfectly known and possibly absent.

## Install

```bash
pip install asm-dependencies
```

It depends on `mainframe-artifacts` (the estate boundary and the two-stage dependency
retrieval, shared with the COBOL, JCL and Easytrieve tools) and on nothing else. Pure
Python standard library, Python ≥ 3.9.

`mainframe-artifacts` ships from the
[mainframe-common](https://github.com/paulhowarda-bit/mainframe-common) repository (one
repo, several distributions; its `mainframe-artifacts/` subdirectory). Until it is on an
index, install it straight from that repo:

```bash
pip install "mainframe-artifacts @ git+https://github.com/paulhowarda-bit/mainframe-common#subdirectory=mainframe-artifacts"
```

**It depends on none of `cobol-xstate`, `jcl-dependencies` or `eztrieve-dependencies`.**
The four are peers that meet at plain manifest dicts. `tests/test_boundaries.py` enforces
that none of them is importable from here.

## Use

```bash
asm-dependencies paycalc.asm                    # 2 views + both retrieval reports -> ./out
asm-dependencies paycalc.asm --target artifacts # just the manifest
asm-dependencies paycalc.asm --summary          # + a human scoreboard on stderr

# Decide the conditional assembly the way the assemble step would
asm-dependencies paycalc.asm --sysparm PROD

# Close the ddnames against the JCL that runs it
asm-dependencies paycalc.asm --bind-jcl out/paycalc.jcl.lineage.json

# Gather where the estate is reachable, model where it is not
asm-dependencies paycalc.asm --gather-only ./bundle
asm-dependencies paycalc.asm --from-bundle ./bundle    # no network at all
```

As a library:

```python
from asm_dependencies.api import analyze

m = analyze(open("paycalc.asm").read(), source_name="paycalc.asm", retrieve=False)
m.artifacts()          # what it provides, what it needs, and the evidence for each
m.lineage()            # every site in source order, plus the unresolved inventory
m.bind(jcl_lineage)    # close the ddname -> dataset join from a JCL model
```

## What it extracts

**Provided** — `CSECT`/`START`/`RSECT` names ∪ `ENTRY` operands, with `AMODE`/`RMODE` and
any `ALIAS` rewrite. This is the `provides` index, and it is what answers "who calls
PAYCALC" once a graph loader has both halves. With `ALIAS` the name in the source is *not*
the name another module binds to, so an unrewritten index joins to nothing.

**Depended on** — as `artifacts` rows:

| what | from | kind | dependency |
|---|---|---|---|
| static-link targets | `DC V(X)`, `=V(X)`, `EXTRN`, `WXTRN`, `CALL SYM` | `program` | runtime |
| dynamic load targets | `LINK`/`XCTL`/`ATTACH`/`LOAD`/`DELETE`/`CEEFETCH` | `program` | runtime |
| ddnames | `DCB DDNAME=`, `ACB`, `OPEN`, `DCB=`/`TASKLIB=` | `file` | runtime |
| CICS programs | `EXEC CICS LINK`/`XCTL`/`LOAD PROGRAM()` | `program` | runtime |
| BMS mapsets | `EXEC CICS SEND`/`RECEIVE MAP()`, `MAPSET()` | `terminal-map` | runtime |
| transactions | `EXEC CICS START`/`RETURN TRANSID()` | `cics-transaction` | runtime |
| CICS files and queues | `FILE()`, `QUEUE()`/`QNAME()` on the TS/TD verbs | `file`, `queue` | runtime |
| Db2 tables | `EXEC SQL` `FROM`/`INSERT INTO`/`UPDATE`/`DELETE FROM`/`DECLARE ... TABLE` | `db2-table` | runtime |
| copy members | `COPY`, `EXEC SQL INCLUDE` | `copybook` | compile-time |
| macros | any operation code the assembler would resolve from a library | `macro` | compile-time |

`EXEC CICS` and `EXEC SQL` are not assembler — they are a preprocessor's language sitting
in the operand field, and two assembler rules do not hold for them: their operands are
separated by **blanks** rather than commas, and they have **no remarks field**, so
everything to the right margin is part of the command. Split the assembler's way, an
`EXEC CICS LINK PROGRAM('PAYCALC')` yields the operand `CICS` and throws the command away
as a remark. A CICS command also continues at **column 2**, not the assembler's column 16,
and it has no `END-EXEC` for the loss to show up against.

CICS states the confidence rule in its own syntax, so the quoting *is* the evidence:
`PROGRAM('PAYCALC')` is a literal, `PROGRAM(PGMNAME)` is a data area whose contents are
decided at run time. `EXEC CICS ASSIGN` names no dependency at all — every one of its
options is a receiver — and a `PROGRAM()` built from what it returns is the canonical
unresolvable CICS dispatch, so the module says where that happened.

**IMS is deliberately partial, and says so.** `EXEC DLI SCHD PSB()` is the one place a PSB
name appears in the source. Under `ASMTDLI` the PCBs are positional (`L R2,0(R1)`), so the
module cannot know which database PCB 2 is — that needs the PSBGEN, and the manifest says
so rather than guessing. `psb` and `segment` rows go in `excluded` with their reason, since
neither kind is registered in `mainframe_artifacts.fetch._KIND_TYPE` yet.

`DC V(NAME)` is an **implicit `EXTRN`** — the highest-confidence dependency signal
assembler has. `=A(NAME)` is *not* external unless the symbol is separately declared,
which is why external resolution is a second pass: the `EXTRN` can come after the use.
`WXTRN` is a distinct edge, not a weaker one — the binder does not search libraries for it
and an unresolved one is not a broken build. `XCTL` is a transfer, not a call.

**Candidates** — names a site *could* reach, kept out of `artifacts` and reported in their
own list. A dispatch table's entries are not proven dependencies; listing them among the
artifacts would overclaim, and listing them nowhere would give a module that dispatches a
hundred programs an empty manifest.

**Declared storage** — the lineage view's `storage` lists every labelled `DC`/`DS`, in
source order, as `{field, line, operand}` plus `section`, `values` (the character
constants a `DC` initialises it with) and `inMember` (the `COPY` member or macro it was
assembled from) when there are any. An unlabelled `DS` is padding and is not listed. There
is no offset or length: nothing here computes duplication factors, type-implied lengths,
alignment or `ORG`, so `operand` is the declaration as written — which is also why a
`PAYINIT DS 0H` entry label is listed as `0H` rather than silently told apart from storage.

## Honest limits, all in `flags` rather than guessed

* **Columns 1–71**, continuation in 72 resuming at 16. A non-sequence tail past the margin
  is reported, not silently dropped. `ICTL` moves the margins and **does not propagate**
  into `COPY` members or library macros, so copied text is always lexed with the defaults.
* **A comment whose prose reaches column 72 swallows the next line.** That is what the
  assembler does — and when the swallowed line was a statement, it is flagged, because
  nothing else in the output would ever show it.
* **A sequence symbol in the name field means no name.** `.RETRY CSECT` starts the
  *unnamed* control section; it does not name one `.RETRY`.
* **An unrecognised operation is a macro**, which is literally how HLASM resolves an
  operation code. One that cannot be retrieved is flagged naming what may be behind it.
* **A backward `AIF`** — a conditional-assembly loop — is flagged rather than unrolled.
* **`SYNCH` and `CALL (15)` name no module.** They are paired with the `LOAD` in the same
  control section when exactly one is a candidate, and reported as ambiguous when several
  are. `DELETE` corroborates a name a dynamic `LOAD` hid, as a candidate and never a target.
* **`MF=L` / `MF=(E,list)`** are two halves of one dependency, joined. A list form is a
  data definition and calls nothing; an execute form transfers control and names nothing.
* **Not modelled, and it says so:** register-level dataflow (assembler gives base and
  displacement, so field flow means modelling the machine), binder-side resolution (a
  `V(NAME)` says a symbol is needed, not which library supplies it — that is the link
  step's `SYSLIB`), and `SETAF`/`SETCF`, `AREAD` and `AINSERT`, which reach outside the
  conditional-assembly language entirely.

## The reverse direction, when a host can supply it

Everything above is what this module *names*. The opposite question — *what depends on
this module* — cannot be answered from its source at all: which modules call one of its
entry points is a fact about the estate, held in an index only the host can read. So it
arrives through a door rather than being derived, exactly the way Db2 synonym knowledge
does:

```bash
asm-dependencies paycalc.asm --dependents-map dependents.json
asm-dependencies paycalc.asm --dependents-resolver mycatalog:who_calls
```

The map is a JSON object keyed `"NAME|KIND"` whose values are lists of dependents rows;
the resolver is a callable asked at the point of need. Supply either and a third view,
`<module>.asm.dependents.json`, is written; supply neither and **nothing is written**,
because "nobody told us" and "nothing depends on it" are different answers and only one
of them is a claim about the estate.

Rows attach to the **entry point** a dependent named rather than to the module, which is
the case that matters here: a call to an entry point whose name differs from the member
name is otherwise unresolvable even when the index holds the answer. An entry point the
lookup does not cover, or did not reach because it failed, is listed under `unanswered`
with the reason — never as an empty list of dependents. A capped answer carries
`truncated` with the true `total`, so a shortened list never reads as a complete one.

A `--gather-only` run records what the lookup answered into the estate bundle, and
`--from-bundle` replays it, so the reverse direction is reproducible off the network like
everything else here.

## The one place it meets the JCL tool

`bind_jcl_ddnames(manifest, jcl_lineage)` resolves this module's ddnames against a JCL
job's `ddBindings`. It takes a plain **dict** and returns one, so this package imports
nothing from the JCL side.

Finding *which* step is simpler here than on the Easytrieve side, and worth stating: an
Easytrieve step runs `PGM=EZTPA00` — the interpreter — so the step's program name cannot
identify which program runs. An assembler module **is** the load module, so `EXEC PGM=`
names it directly. The match is against the whole `provides` index rather than the
module's own name, because a load module is often bound under an `ENTRY` name or an
`ALIAS`. A ddname this module sets at run time is deliberately left unbound.

`tests/fixtures/paycalc.jcl.lineage.json` is a committed JCL lineage view — a real one,
produced by running `jcl-dependencies` over `examples/paycalc.jcl` — so these tests need
no JCL install. `tools/refresh_fixture.py --check` asserts the shape the binder reads and
is run by the suite, because a drifted contract does not fail loudly: an unbound manifest
looks exactly like one nobody tried to bind.

`JCL_BINDING_API_VERSION` in `asm_dependencies/__init__.py` is the contract version.

## Validated against real source

The examples in `examples/` are written for this repository, so they only prove the tool
does what it was built to do. It has also been run over two public corpora of real
assembler — 106 members, no crashes:

* **IBM's IFOX (Assembler XF) source**, ten production modules of card-image assembler
  from [moshix/IFOX](https://github.com/moshix/IFOX). It is the ideal adversarial case,
  because IFOX declares its own structure through site macros: `JCSECT (X0A00)` makes the
  control section and `JENTRY (X0A01=START)` makes the entry points, with the definitions
  in a `COPY JCOMMON` that is not in the repository. So the tool correctly reports
  `provides: []` — and now says *why*, rather than leaving an empty list that reads as
  "this module exposes nothing". It finds all eight of IFOX's dynamic phase `LOAD`s and
  reports each one unresolved, naming the member the phase name lives in.
* **John Ehrman's teaching programs** from
  [adelosa/learnasm370](https://github.com/adelosa/learnasm370), 96 members.

That run found five real defects, all now regression-tested: `MEXIT` was truncating a
macro body statically rather than ending *generation* (which discarded the `AIF` target
after it), a macro's own `LCLA`/`SETC` variables were reported as unsupplied parameters, a
sequence symbol on `MEND` was dropped so every `AIF (...).EXIT` looked like a branch to
nowhere, IBM's own `SYS1.MACLIB` macros were being chased through the estate alongside the
shop's, and a macro-generated control section produced a silently empty `provides`.

## Development

```bash
# mainframe-artifacts comes from a sibling mainframe-common checkout (or the git+ line above)
python -m pip install -e ../mainframe-common/mainframe-artifacts -e .
python -m pytest -q
python tools/byteproof.py --check goldens/views.sha256   # byte-stability ratchet
python tools/refresh_fixture.py --check                  # the JCL contract fixture
python tools/make_examples.py                            # regenerate examples/
```

From a bare dual-checkout — mainframe-common beside this repo, nothing installed — the
suite and the ratchet find `../mainframe-common/mainframe-artifacts` automatically
(override with `MAINFRAME_COMMON_REPO`); without either, the suite ends as one clean skip
naming the exact pip command.

Output is byte-stable and deterministic: a refactor that should not change output must
produce identical bytes, and a green test run does not prove that — the order of a
candidate list, the presence of a reason and the key order of a row are all output, and
none of them is asserted anywhere. The ratchet hashes every view of every example under
two `PYTHONHASHSEED` values and under both `&SYSPARM` settings. Re-record only when an
output change is intended and reviewed.
