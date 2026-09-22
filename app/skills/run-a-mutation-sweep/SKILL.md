---
name: run-a-mutation-sweep
description: Use when checking whether a test suite actually catches regressions by mutating the code under test — the sweep edits files in place, so run it in this exact order or it leaves damage that reads as an ordinary bug in your own work.
---

# Running a mutation sweep without wrecking the tree

A mutation sweep proves a suite has teeth: mutate a line, run the tests, expect red. It works
by **editing files in place**, which is why an interrupted one is the most expensive mistake
in this repo — it has bitten repeatedly. The mutation is left applied, and every symptom after
that reads as a bug in code you just wrote.

## Do not edit the tree at all, if you can help it

The safest sweep never writes a file in the checkout: export the tracked files to a scratch
directory, mutate THAT, and run the suite there. An interrupted sweep then leaves nothing
behind, `git status` cannot go dirty, a dev server cannot pick a mutation up, and the whole
"Recovering" section below stops applying. Copy tracked files only, so a big `data/` does
not fill the tmpfs:

```python
names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                       check=True).stdout
tar = subprocess.Popen(["tar", "--null", "-T", "-", "-cf", "-"], cwd=ROOT,
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
untar = subprocess.Popen(["tar", "-xf", "-"], cwd=dest, stdin=tar.stdout)
tar.stdout.close(); tar.stdin.write(names); tar.stdin.close()
untar.wait(); tar.wait()
```

`tar` reads the files off disk, so this exports the WORKING TREE of every tracked file -
including the change you are sweeping, uncommitted. The rest of this page is for the sweeps
that genuinely must edit in place.

## Restoring a file restores its bytes, not its clock

**Writing the original text back rewrites the mtime even though not one byte changed.** After
an in-place sweep every file it touched looks freshly edited to anything reading mtimes, and
a 67-mutation sweep reads from outside as 67 urgent deploys that never happened. On
2026-09-21 three projects on this estate - secret-shopper-helper, mtg-proxy-forge and
cork-engraving-modeler - each answered an UNDEPLOYED drift finding within six minutes of each
other, having measured their own running code, found it current, and traced the finding to
exactly this.

So **capture the clock with the text, and restore it last** - after every write to that file,
because restoring the content is itself what moved the mtime:

```python
stamps = {path: os.stat(path).st_mtime_ns for path in paths}   # BEFORE mutating
try:
    ...mutate, run the suite, write the original text back...
finally:
    path.write_text(original)
    shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)
    os.utime(path, ns=(stamps[path], stamps[path]))          # LAST
```

Two things about the shape:

- **Pair the capture and the restore in one helper** that reads text and clock together and
  writes text and clock together. A second argument to `restore()` is something every call
  site can forget; a helper that cannot be called without the stamps is something none of
  them can.
- **Put it in a `finally`.** That also fixes the older problem that a sweep killed by a
  timeout leaves its mutation in the working tree.

The portal ships that helper at `deploy/sweeplib.py` (`capture(paths)` / `restore(originals)`),
held down by `tests/test_sweeplib.py` - which also asserts, over every sweep script in
`deploy/`, that it either works on an export or goes through the helper. The test that reads
the write-back off the syntax tree is the one worth copying: a sweep author does not have to
remember the rule if the suite remembers it for them.

## Order of operations

1. **Commit first.** Let the sweep own a clean tree. If anything goes wrong the recovery is
   `git checkout .`, which is only safe if there is nothing else uncommitted.
2. **Keep the safety fence OUTSIDE the code under test.** A guard that the sweep can mutate
   is not a guard.
3. **Run it in the FOREGROUND.** `setsid nohup ... & disown` does not save it: an agent run's
   cgroup kills background children on a normal exit, and a sweep killed mid-mutation leaves
   that mutation in the tree for the next `git add -A` to commit outright. If the sweep is
   long enough to hit the 10-minute tool timeout, split it into batches that each fit rather
   than backgrounding it.
4. **Do not import the sweep script "just to syntax-check it"** — importing runs it.
5. **Start no dev server while it runs.** The server loads whatever mutation was applied at
   that moment and keeps serving it until restarted. If a served page misbehaves during a
   sweep, compare the served bytes against the same function called in-process before
   believing the page.
6. **Check `git diff` when it finishes**, every time, before doing anything else.

## Reading the results

- **Before trusting a sweep's score, check that every anchor still occurs exactly once in
  the file it mutates** - a SKIP is counted as a survivor and reads as a number rather than
  as a broken sweep. A sweep anchors on a literal copied out of the code under test, so
  ordinary refactoring rots it, often the very cleanup the sweep's own fix made possible.
  Measured across 26 sweeps elsewhere on the estate, 14 of 696 anchors had come loose,
  including the one mutation proving a weekly watch notified at all. An anchor found
  *twice* is just as broken: `text.replace(find, repl, 1)` silently mutates the first
  match, so the sweep reaches a confident verdict about the wrong line. Have each sweep
  expose an `anchors()` returning `[(file, exact string)]` and one test ask them all on
  every test run, rather than waiting for the next hand-run sweep months later. The portal's
  own is `tests/test_sweep_anchors.py`.
- A test run that HANGS usually means a mutation turned a loop condition into an infinite
  loop, not that your code is wrong.
- **Sweep the new assertions too, not only the code they guard.** An assertion whose fixtures
  all satisfy it trivially survives every mutation and looks like real coverage.
- To validate a specific regression check, delete the fix it guards and watch it fail. If it
  still passes, the check is decorative.

## Recovering from an interrupted sweep

`git status` showing unexpected edits after a sweep is a mutation, not work in progress.
`git checkout -- <file>` it. If a tree keeps going dirty on its own, a stray sweep from a
timed-out earlier run is still going: find it in `ps -eo pid,cmd` and kill it by pid.
