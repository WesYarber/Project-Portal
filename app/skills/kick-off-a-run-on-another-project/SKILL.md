---
name: kick-off-a-run-on-another-project
description: When your project is blocked on work that belongs to a DIFFERENT project in the portal, use this to file the note and queue that project's run yourself instead of leaving $OWNER a todo.
---

# Kick off a run on another project

$OWNER stated this on 2026-09-09: "if other projects need to kick off work, please kick
off that work on them." That rules out the hand-off todo: when one agent could have
started the other project directly, it should have. Do this whenever the thing standing
in your way is code, data or a decision that lives in a sibling project rather than in
your workspace.

## Why not the obvious routes

- Other projects' workspaces are hard-blocked to you; you cannot edit them.
- `ask_project` is read-only, so it cannot queue anything.
- `portal.db` is blocked to agents, so you cannot insert a note or a run row by hand.

The portal's own HTTP door is the way in.

## The call

The portal answers at `$BASE_URL`, from this box as much as from a phone. Post a note
to the target project and ask for a run in the same request:

```bash
curl -sS -X POST $BASE_URL/project/<slug>/note \
  --data-urlencode 'note=An agent on <your-project> needs X. <what is needed, and why>.' \
  --data-urlencode 'then=run'
```

`note` is the brief the other project's next agent reads. `then` decides what happens
after it is filed, and it has exactly four meaningful values:

| `then=` | what it does |
| --- | --- |
| `run` | files the note **and** queues a run on that project, waking it if it was put down |
| `parallel` | starts a second run **now**, beside the one already working, in a git worktree of its own |
| `hear` | files the note, marks it for delivery to a run already in flight, and otherwise behaves like the plain note below |
| `queue` | files the note and starts **nothing** |

**`then=queue` is the only way to file a note WITHOUT starting a run.** Omitting the
parameter does not do that, and neither does any other value: the note route treats
everything that is not `run`, `parallel` or `queue` as the plain green "add note",
which wakes a put-down project and starts a run whenever one could start at all. So
`then=none`, `then=nothing` and a missing `then` all mean "start a run if you can" -
which is the opposite of what those words look like they mean, and has already cost a
real run.

Pick `queue` for anything that is information rather than a request: a finding, an
answer, a confirmation. Pick `run` when you actually need the other project to act.

## You do not need to check the slug first

`<slug>` is the project's path segment in the portal - `/project/<slug>`. If you do not
know it, **just post the real note with the slug you believe in**. A slug the portal
does not have is a **404 before anything happens**:

```
HTTP/1.1 404 Not Found
{"detail":"Project not found"}
```

Nothing is filed, nothing is queued, no journal entry exists, and you can try again
with a different guess for free. The lookup happens on the route's first line, ahead of
every side effect.

So never send a throwaway "ping" to find out whether a project is there. On 2026-09-21
an agent did exactly that, believing `then=none` would file nothing, and the portal
filed the word "ping" on a stranger's project and queued a run on it - after which the
real note had to open with an apology. The 404 is the probe.

If you want the slug rather than a guess: the `projects` MCP tool lists the ones your
project may read, and every project's workspace is a directory named for its slug under
the portal's projects directory - a name that turns up in any deploy file, compose file
or bind mount that mentions one of those workspaces, even for a project you cannot
otherwise see.

## A 303 with no body IS the success response

The endpoint answers **303 See Other** and redirects to the project page,
because the caller it was built for is a browser submitting a form. `curl`
does not follow a redirect unless you pass `-L`, so the reply lands in your
terminal as a status line and a body - and the body used to be empty, which
made a successful post look exactly like a no-op.

Since 2026-09-20 that body is one line of plain text saying what happened:

```
ok: note filed on mtg-proxy-forge; a run is queued.
ok: note filed on mtg-proxy-forge; it waits for the next run.
ok: note filed on mtg-proxy-forge; the agent reads it on its next run.
ok: nothing filed (the note was empty); nothing was started.
```

Read that line and believe it. It distinguishes the cases that used to look
identical - a filed note, an empty one, a `then=run` that queued a run, a
plain note that did not manage to start one, a parallel run the portal
refused - so there is nothing left for a probe to find out.

**Never post a second note to check the first one arrived.** On 2026-09-20 two
agents did exactly that, and each had to file a third note apologizing for the
stray: three notes to deliver one. If you are talking to an older install
whose body is still empty, ask for the status code instead of posting again -
a `303` is the success:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X POST $BASE_URL/project/<slug>/note \
  --data-urlencode "note=..." --data-urlencode "then=run"
```

## Say who you are

The endpoint stamps every note as a **user** note, so the receiving agent will read it
as if a person typed it. Open the note text by naming yourself - "An agent on the case
configurator filed this" - or the other project will act as though it was asked in
person and may ask follow-up questions nobody expected.

## After you post

Do not then park your own run waiting on it. Work everything in your list that does not
depend on the answer, and say in your summary that you kicked the other run off.
