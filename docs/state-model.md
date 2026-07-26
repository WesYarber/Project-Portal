# Redesigning project states

Status: **implemented** - Wes approved this on 2026-07-22 ("For the changes to
project statuses, I like what you have proposed") and the whole design below
shipped in one pass: schema + migration (`db._migrate_status_to_stage`), worker,
report compat mapping, dashboard, picker, Telegram/nl, and the test sweep. Two
deliberate deviations from the text below: a `review`-stage project holding an
open question stays on the Review shelf (that shelf already means "your turn"),
and scheduling skips a blocked/asking project only when it also has no open
agent todos (the one-line refinement the doc itself anticipated). His follow-up
asks landed with it: two-button idea entry (backlog vs plan-now) and
note-triggered reactivation (`worker.reactivate_on_note`).

## Why

Wes, 2026-07-22 04:50: *"I really don't like how we are doing project states as
of now, and I want to revisit and improve these in the future."* He did not
spell out the dislike, but the record does. The same night produced two notes
(04:50, 06:02) about paused projects landing on the wrong dashboard shelf, and
both bugs came from the same root: the stored status does not say what Wes
means by it, so every consumer has to re-interpret it - and each interpretation
site is a place to get it wrong.

## What we have today

One stored `status` column with eight values, plus three patch-columns that
exist only because the eight values conflate independent facts:

| stored | what it actually means | patched by |
|---|---|---|
| `inbox` | not started (Wes calls it "backlog"; the UI already lies about the name via `STATUS_BADGES`) | |
| `planning` | agent works on it, but may not write code | duplicates `build_approved = 0` |
| `building` | agent works on it and may write code | duplicates `build_approved = 1` |
| `needs_input` | active, but an agent question is open | `resume_status` remembers where to go back to; `db.resolve_question` restores it when the last question is answered |
| `waiting_user` | EITHER "Wes paused it" OR "agent is blocked on Wes" OR "plan ready, awaiting build OK" | `paused_by_user` disambiguates the first from the other two |
| `review` | agent thinks it is done; Wes should look | |
| `done` / `abandoned` | finished | |

Everything the status drives, as of `9214756`:

- **Scheduling** - `worker._pick_project` schedules `{inbox, planning, building}`;
  `STATUS_TASK` maps them to triage/plan/build prompts (`app/worker.py:21-25,137`).
- **Dashboard shelf** - `db.project_shelf` (`app/db.py:754-770`) re-interprets:
  running run → top, `waiting_user`/`needs_input` → Paused, `inbox` → Backlog,
  the rest split into Building/Review in `main.dashboard`. This function is the
  third rewrite of that mapping; the first two are the bugs Wes reported.
- **Question urgency** - `db.shelved_project_ids` (`app/db.py:772-782`) decides
  whose open questions count on the nav badge, again by re-interpreting status
  plus `paused_by_user`.
- **The build gate** - `worker` converts an agent's `new_status: building` into
  `waiting_user` + a journal plea when `build_approved` is 0 (`app/worker.py:728-745`),
  overloading `waiting_user` a third way.
- **Auto-resume** - answering/dismissing the last open question while in
  `needs_input` restores `resume_status` (`app/db.py:1214-1264`), state-mirroring
  that exists only because "has an open question" was promoted into the enum.
- **The picker** - `USER_STATUS_CHOICES` hides `planning`/`needs_input` from Wes
  (agent-only values), and `status_choices` has to splice the current value back
  in so the dropdown doesn't misrepresent (`app/config.py:197-238`).

The smells, compactly: two of the eight values duplicate a flag that already
exists (`build_approved`); one value mirrors a count that already exists (open
questions) and needs a return-address column; one value means three different
things and needs a who-set-it column; and the stored names have already drifted
from the names Wes sees (`inbox`→"backlog", `waiting_user`→"paused").

## The proposal

Split the one overloaded enum into its independent facts. **Stored, user-owned:**

- **`stage`** - the lifecycle, exactly the five things Wes ever chooses, exactly
  the five dashboard shelves: `backlog | active | review | done | abandoned`.
  Stored name = displayed name, no badge-translation table.
- **`paused`** (timestamp or NULL) - orthogonal to stage, only Wes sets it.
  A paused project is never scheduled and folds to the Paused shelf whatever
  its stage. Replaces `paused_by_user` and the user-pause meaning of
  `waiting_user`.

**Stored, agent-writable facts (not states):**

- **`build_approved`** - unchanged. `active` + unapproved = today's `planning`;
  `active` + approved = today's `building`.
- **`build_requested`** (new, boolean) - the agent's "plan is ready, may I
  build?" Today this hijacks `waiting_user`; approval (or revoke) clears it.
- **`blocked_on`** (new, short text or NULL) - the agent's "I need something
  from Wes": a purchase, a credential, a click. Replaces the agent-parked
  meaning of `waiting_user`. Cleared automatically when the next run on the
  project completes, so it cannot go stale silently.

**Derived, never stored** - the "whose turn is it" layer, computed per render:

- *agent working* - a run in flight (already `running_project_ids`).
- *needs you* - open question count > 0. No `needs_input` stage, no
  `resume_status`: answering the last question changes the count and the badge
  disappears by itself. Nothing to resume because nothing moved.
- *awaiting build OK* - `build_requested` and not `build_approved`.
- *waiting on you* - `blocked_on` is set.

### The dashboard under this model

Shelving stops being interpretation and becomes arithmetic, in one place:

```
running run                         -> top of Building (as today)
paused OR blocked_on OR open q's    -> Paused shelf   (Wes 06:02: "I don't
                                       care - I want them in the paused/backlog
                                       section!")
else stage                          -> its own shelf (backlog / active /
                                       review / done+abandoned fold)
```

Question urgency keeps tonight's rule unchanged: a *Wes-paused* or backlog
project's questions are demoted; agent-parked (`blocked_on` / open questions)
stay loud. That is `paused IS NOT NULL OR stage = 'backlog'` - no
interpretation column needed.

### Scheduling under this model

- `backlog` → triage runs (as `inbox` today).
- `active` + unapproved → plan runs; `active` + approved → build runs.
- `paused` set, or `review/done/abandoned` → not scheduled.
- Open questions or `blocked_on` do NOT stop scheduling by themselves if other
  todos remain workable - matching the existing contract ("asking does not
  mean stopping"). Today `needs_input`/`waiting_user` DO stop scheduling, which
  contradicts that contract; this fixes it. A project whose *only* remaining
  work is blocked will simply produce no-op-avoidant runs; the daily cap and
  the agent's own "stop when repeating" rule bound the waste, and if that
  proves too optimistic, "skip when blocked_on is set and no agent todos are
  open" is a one-line refinement.

### The agent contract

Agents stop steering an enum and report facts instead: `new_stage`
(`review` or null - the only stage move an agent may propose),
`request_build: true`, `blocked_on: "..."`. Questions already arrive
structurally.

Compatibility rule (the learning: a contract change is executed by the OLD
code on the run that ships it, and by in-flight runs after): the worker maps
the old vocabulary forever - `building`→`request_build`, `waiting_user`→
`blocked_on` (reason: last journal entry), `needs_input`→no-op (questions
already carry it), `review`→`new_stage: review`, `planning`/`done`→ignored
with today's logging.

## Migration (mechanical, reversible by backup)

| today | becomes |
|---|---|
| `inbox` | `backlog` |
| `planning` | `active` (build_approved already 0) |
| `building` | `active` (already 1) |
| `needs_input` | `active`; drop `resume_status` - open questions already carry the badge |
| `waiting_user` + `paused_by_user` | `active`, `paused` = that timestamp |
| `waiting_user`, agent-parked | `active` + `blocked_on = "see the last journal entry"`; if the park was the build gate (build_approved 0 and the "ready to build" journal line), `build_requested = 1` instead |
| `review` / `done` / `abandoned` | unchanged |

Keep the old column as `status_old` for one release rather than dropping it,
matching how other migrations here hedge. Journal history stays as text.

## Cost, honestly

This touches the schema, worker, dashboard, project page, config, Telegram
bot/nl parsing, agent contract, and on the order of a couple hundred of the
1409 tests. Estimate: two runs (one for schema+worker+contract with the compat
mapping, one for UI+bot+test sweep), each leaving the suite green and the old
vocabulary still accepted, so it can ship incrementally without a flag day.

## What this does NOT change

- The build gate itself (agents still cannot write code unapproved).
- Question flow, notifications, runs, journals.
- Any URL or page layout; badges change wording only where the state names do.
