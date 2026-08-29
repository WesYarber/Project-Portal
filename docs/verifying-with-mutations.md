# Verifying a change by deleting the fix

This project's standard proof that a test really owns a fix is a **delete-the-fix
sweep**: take each line you added, delete it, run the whole suite, and check that
exactly the test which should own that line is the one that fails. A fix nothing
fails on is a fix nothing is holding in place, and the next refactor removes it.

It works. It has also broken itself in three different ways, all of them silent,
and this file is the list — because a sweep that lies to you is worse than no
sweep at all: it ends with a green suite and a defect in the tree.

## First: does this change earn a sweep at all?

This file is about doing a sweep correctly. **Whether to do one is decided
elsewhere** — by the `## How much to verify this run` section of your own
prompt, which carries the install's `verification_depth` setting (see
`app/verifydepth.py`). On the default, Wes's answer of 2026-08-07 applies: sweep
only when *logic* changed. A docs edit, a label, a constant or a rename does not
earn one, and a run that sweeps anyway is spending about 60% of its cost proving
something nobody doubted.

So read that section before you read the rest of this one.

## 1. A killed sweep leaves its mutation applied

The naive harness is

```python
path.write_text(mutated)
subprocess.run([..., "pytest", "tests/", ...])
path.write_text(original)     # <-- only reached if the process survives
```

Twelve mutations times a 90-second suite is eighteen minutes, which is longer
than an agent's foreground command timeout. So on 2026-07-29 the sweep was
**killed mid-mutation** and the restore never ran. One line — `kept.reverse()`
— stayed deleted in the working tree.

The next sweep then captured its baseline from the already-broken file:

```python
orig = {p: p.read_text() for p in (PB, TD, AR)}
```

Its own version of that mutation reported `SKIP (pattern missing)`, because the
line it meant to delete was already gone. Every *other* mutation restored the
file to the broken baseline. And the defect shipped through a completely green
3202-test suite.

**Fixes, all three needed:**

1. **Restore however you die.** `atexit.register(restore_all)` plus `SIGTERM`
   and `SIGINT` handlers that call `sys.exit` — a bare handler that returns does
   not run `atexit`.
2. **Run it detached.** `setsid nohup … < /dev/null &` with `python -u`, and
   wait on a marker the script prints at the end (`SWEEP COMPLETE`), not on the
   process. Block-buffered stdout is why an 18-minute sweep looks hung for its
   whole life.
3. **Re-run the plain suite between a killed sweep and the next one.** Cheap,
   and it is the only check that catches this.

## 2. A broad failure is evidence about the baseline, not about the test

The signal was there and was easy to misread. One test failed on **eleven of
twelve mutations**, including mutations in a module it has nothing to do with —
a `promptbudget` test failing when the mutation was in `todos.py`. The tempting
reading is "that test is flaky, ignore it".

> **A test that fails under mutations it has nothing to do with is not flaky.
> It is failing on the baseline, and your baseline is broken.**

A sweep means nothing unless the unmutated tree is green. Check that first, and
read a suspiciously broad failure as news about the tree.

## 3. A fixture must never size itself off the constant under mutation

The test written to close one of the gaps did this:

```python
"text": "x" * (todos.DONE_TAIL_BYTES * 3)     # 3 GB under the mutation
```

Under the mutation that raises `DONE_TAIL_BYTES` to `10**9` this allocates a
3 GB string. pytest dies inside the run's 7.5 GiB memory cgroup before emitting
a single `FAILED` line — so the harness sees no failures and reports the
mutation as **uncaught**, which is the exact opposite of the truth.

Two rules out of one bug:

- A fixture states its own size. Give the function under test an explicit
  parameter (`_done_tail(rows, budget=...)`) rather than reading the module
  constant into the fixture.
- **A mutation harness must not read "no FAILED lines" as "no failures".**
  Check the return code and the fact that pytest emitted a summary line at all;
  a crashed run is a *skipped* data point, not a passing one.

## 4. Mutate the wiring, not only the logic

Both genuine gaps the sweep found were the same shape — a pure function is easy
to test and a **call site** is easy to forget:

- `todos._done_tail`'s "one item always survives" guard was covered only through
  `prompt_section`, where `db.add_todo`'s 500-character truncation made the
  guard unreachable. The test was vacuous and deleting the guard broke nothing.
- Nothing owned the line wiring the budget into `build_prompt`. Replacing
  `_budget_bytes("prompt_answered_kb", 6)` with a billion at the call site
  failed no test at all.

So put a mutation on the argument, the default and the call, not just on the
body.

## 5. `pgrep -f` and `pkill -f` match the shell that runs them

`until ! pgrep -f mutsweep.py; do sleep 15; done` **never exits**: `pgrep -f`
matches the full command line, and the waiting shell's own command line contains
the pattern, so it forever finds itself.

The same self-match kills you from the other end — `pkill -f 'port 8791'` run
from a shell whose command line contains that string kills the shell, which
surfaces as exit code 144 and a command that "did nothing". That one is recorded
in `docs/looking-at-the-ui.md` too, because the throwaway-portal teardown is
where you meet it; it is the same trap and it applies to both tools.

**Wait on a file marker, not on a process name**, and put a teardown pattern in
a script file rather than in the command line that runs it.

## 6. Parse the FAILED line before believing a MISSED

The harness decides "caught" by looking for the owning test's name among
pytest's `FAILED tests/...::test_x` lines. On 2026-08-04 a sweep reported its
first two mutations MISSED while showing "2 failures" right beside the verdict
— the parser did `line.split(" ")[0].replace("FAILED ", "")`, and the first
space-separated token of a FAILED line is the word `FAILED` itself, so the
owner name was compared against the string "FAILED" forever.

This is §3's lie in the other direction: there the harness read a crash as
"uncaught", here it read a catch as a miss. Both waste a kill-and-rerun of an
half-hour sweep. The tell is the same both times: **the verdict and the raw
failure count disagreeing.** A MISSED with a nonzero failure count deserves a
look at the failed-test names before anything else — and a harness change
(the parse, the verdict logic, the marker) deserves one mutation's worth of
dry run before the other twelve.

### 6a. `ERROR` is not `FAILED`, and a teardown assertion is always `ERROR`

Same lesson, met again on 2026-08-29 and worth its own line because the parse
looked correct. The harness matched `^FAILED (\S+)`, which is right as far as it
goes — but a check that runs in a **teardown hook** rather than in a test body
is reported by pytest as

```
ERROR tests/test_module_state.py::test_a_run_id_keyed_registry_is_filled_here
```

never as FAILED. The module-state invariant is enforced from
`pytest_runtest_teardown`, precisely so the *polluting* test is the one named,
so every mutation it catches is invisible to a FAILED-only parser. The sweep
reported MISSED while its own run had printed "2 errors", and a whole extra pass
went into rewriting tests that were already working.

Match both:

```python
FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
```

The §6 tell applies unchanged: **a MISSED whose run shows a nonzero failure or
error count is a parser bug until proven otherwise.** Applying the mutation by
hand and reading the output took under a minute and settled it.

Also from that sweep, on the throwaway-portal side: `pkill -f 'port 8791'`
killed the invoking shell *even though the pattern lived in a script file*,
because the `printf` that wrote the script sat in the same command line that
ran it. Write the script in one command, run it in the next.

## 7. A full-suite sweep fills tmpfs, and the run's own cgroup pays for it

On 2026-08-16 a ten-mutation sweep died after six with every shell command
failing as

```
/bin/bash: line 1: pwd: write error: Disk quota exceeded
```

which reads like a broken machine and is not. `/tmp` on this box is a **9.5 GB
tmpfs**, its pages are charged to the *allocating* cgroup, and every agent run
is a memory-capped scope. Each full-suite pass leaves its `tmp_path` trees
behind — six passes had put **1.8 GB across `/tmp/pytest-of-wes`** — so a sweep
slowly starves the run executing it. Neither total disk nor inodes was anywhere
near exhausted (80% and 73%), which is exactly why the error misleads.

Three things follow:

- **Budget the sweep in disk, not just minutes.** `du -sh /tmp/pytest-of-wes`
  before and between passes; `rm -rf` it when the sweep ends. Ten full-suite
  mutations is ~3 GB of tmpfs you are renting from your own memory cap.
- **Prefer the owning test FILES for the re-sweep.** The question a re-run asks
  is "does the test written to own this line fail without it", and the two
  files that hold it answer that in two seconds instead of 150. Keep the
  whole-suite pass for the first round, where unexpected breakage is the point.
- **Kill it with SIGTERM, never SIGKILL.** §1's `atexit` fence only runs if the
  process gets to exit — and then verify with `diff` against the copy aside
  rather than trusting the handler ran.

Related, from the same run: **`--timeout=` is not available here.**
pytest-timeout is not installed, so the flag makes pytest exit rc=4 having
emitted no summary line at all — which a §3-compliant harness dutifully reports
as SKIP on every single mutation. One dry-run mutation caught it before the
other nine were wasted, which is §6's rule earning its keep a second time. Time
box with the subprocess timeout instead.

## 8. A MISSED can mean the mutation did nothing, not that the test is weak

The sweep for `app/inert.py` on 2026-08-16 reported `regex: a literal ends a
value (prev)` MISSED across three passes. Two rewrites of the owning test later,
the mutation was still walking through it — because the mutation was a **no-op**:

```python
_VALUE_ENDED = ")"      # the real value
_VALUE_ENDED = "/"      # "broken" - and identical in behavior
```

The constant's only job is to be a character that `_REGEX_PRECEDING_CHARS` does
**not** contain, and `/` is not in that set either. Both values mean "a slash
here divides". There was nothing for any test to catch. Changing the mutation to
`"="`, which *is* in the set, caught it on the first try.

So a stubborn MISSED has three explanations, not one, and they want checking in
this order because that is how cheap they are:

1. **The mutation does not change behavior.** Verify it directly — `exec` the
   mutated source and print the function's output beside the real one — before
   touching the test. Two minutes, and it ends the question.
2. **The named owner is the wrong test.** §6's failure count already tells you:
   a MISSED reporting other failures means the line is held, just not where you
   said. Three of this sweep's twelve first-pass misses were only that.
3. **The test really is vacuous.** The commonest cause, and the one the previous
   day's run named: a test written in the same breath as the fix tends to assert
   something *downstream* of it that differs either way. Nine of twelve here.

The tell for (1) versus (3) is that a vacuous test can be made to fail by
sharpening it, and a no-op mutation cannot be made to fail by anything. If two
honest rewrites of the test have not moved the verdict, stop rewriting the test
and go read what the mutation actually does.

A related habit worth keeping from the same sweep: when a line survives every
attempt to make it observable, that is evidence about the *line*. The regex
flag-consuming loop in `_end_of_regex` could not be caught by any test because
its presence changed no output anywhere — so it was deleted rather than
documented as an exception, which is this file's opening premise applied to
itself.

## 9. `ERROR` is not `FAILED`, and a crashed harness reads as an escape

2026-08-28, and it is §3 and §6 arriving together through a door neither names.

A sweep over the seamless-note change reported two escapes that were nothing of
the kind. The mutations made a note post urlencoded instead of multipart, so the
body handed to the stub `fetch` stopped being a FormData and became a plain
string. One scene in `tests/js/inplace_submit.mjs` called `.get()` on it. bun
threw, the **module-scoped** `ran` fixture that shells out to bun raised, and
pytest reported that as

```
ERROR tests/test_inplace_submit.py::test_a_note_posts_as_multipart...
```

never as `FAILED`. The harness parsed only `^FAILED (\S+)`, found nothing, and
filed a mutation whose tests catch it perfectly as uncaught.

Two fixes, and both are needed, because they fail in opposite directions:

1. **Parse `ERROR` as well as `FAILED`.** Any suite whose tests sit behind a
   session- or module-scoped fixture can report a real catch this way, and the
   more expensive the fixture the more likely it is to be scoped that broadly.
2. **A scene must not assume the fixed behavior.** The whole job of a scene is
   to observe the broken tree, so anything it reads from the code under test has
   to be read defensively — here, a helper that pulls a field out of a body
   without caring whether it is a FormData or a urlencoded string. A scene that
   throws takes every *other* mutation's verdict down with it.

The tell is the same one §6 gives, worn differently: **a MISSED whose pytest run
did not actually run the tests.** Check that the owning test executed at all
before believing it passed. "0 failed" and "never ran" print almost identically.

### And a mutation can land somewhere other than where you aimed it

From the same sweep. `MORPH_KEEP` is built by string concatenation:

```js
var MORPH_KEEP = ".draft-note, .ctx-menu, #pull-refresh, #img-lightbox, " +
  "#sel-actions, .quote-chip, .rec-row, .attach-row-item";
```

so the pattern `".rec-row, .attach-row-item"` — with its leading quote — **does
not occur in it**. The opening quote is back at `"#sel-actions`. The pattern
matched a `querySelectorAll` elsewhere in the file instead, and was caught, by
that call site's test rather than the declared one.

`src.count(find) == 1` does not save you here: the pattern was unique, it was
just somewhere else. A `WRONG OWNER` verdict is therefore worth reading as
"where did this actually land?" before it is read as "which test is weak?" —
anchor a pattern on something structural (a closing quote and semicolon, a line
ending) rather than on a substring that could sit inside a longer literal.
