"""One project's runs, reading another project's context - without a person in
the middle.

Wes, 2026-08-29:

    "Projects should be able to talk to one another when helpful or requested
    to. They should be able to inquire of one another or even review each
    other's context and files to learn and or understand things about one
    another that is useful. For example, I have some different tools that are
    related to my commander case store project. I want them to be able to
    understand the context from that project so I don't have to reexplain
    everything to them or be the middleman going between the different agents
    to bridge their context."

Five separate projects on this board are about the same physical product -
`commander-case`, `-counter-configurator`, `-custom-lid`, `-makerworld-poster`,
`-product-renderer`. Each has its own workspace, journal and todo list, and
each run on each one started from zero knowledge of the other four. The only
channel between them was Wes retyping what one already knew into a note on
another, which is the work this module exists to delete.

## Why this is a portal tool and not a hole in the fence

`app/hookguard.py` denies a run's *writes* anywhere in the data dir outside its
own workspace family, and denies a Bash command that so much as names another
project's directory. That fence is right and stays exactly as it is: "may read
that project's context" and "may run arbitrary shell commands rooted in that
project's directory" are not the same permission, and a shell command cannot be
told apart from a destructive one by looking at it.

So the portal serves the context itself, over the MCP server it already exposes
to its own runs (`app/portalmcp.py`). Same reasoning as the hook relay's: the
decision lives here, in one testable place, with the database in reach.

## Who may read whom

The reading project's **principal** (`people.principal` - the person a run works
on behalf of) must be a member of the target, or the target must have no members
at all. That is deliberately the same rule the dashboard already filters by, so
a run sees exactly the projects its person sees and nothing appears to an agent
that would not appear to the human it reports to. On a one-person install this
is every project and the rule costs nothing; on this one it is what keeps a run
on Karli's landing page out of Wes's shop projects.

A project that is missing and a project that is not readable get the **same**
answer. Telling a run "that exists, but not for you" would let it report the
existence of somebody else's project to a person who is not on it, which is the
one thing the rule is for.

## What "related" means, and why it is derived rather than declared

The tools are useless if a run never learns which projects are worth looking at,
so `prompt_section` names the neighbors. Relatedness is computed from the slug:
two projects are related when their slugs agree from the *left* on at least one
token that is rare on this board. `kingshot-gift-code` and `kingshot-auto-bear`
agree on *kingshot*; `board-games-tak` and `board-games-santorini` on *board
games*; `sparrow-net` and `finch-com` on nothing, because `net` and `com` are
stop tokens and the rest differs.

From the left, because Wes names a slug `parent-then-detail`. Matching on any
shared token instead paired `secret-shopper-helper` with
`board-games-secret-hitler` - one word in common and nothing else.

Derived, not declared, because a declared link is a form Wes would have to fill
in for thirty projects before the feature did anything at all - and his slugs
already carry the grouping. A declared "related projects" field is the obvious
next step *on top of* this, not instead of it.

The rarity ceiling scales with the board (`_df_ceiling`): a token in more than
40% of readable projects groups nothing useful, but on a three-project install
40% rounds below two and would make every token generic, so the floor is two.

Family - a parent, its children, their siblings - is left out of "related" and
handled separately, because `subprojects.prompt_section` already names every one
of them a few lines above in the same prompt. All that section withholds is the
slug these tools take, so that is all this one adds for them.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from app import config, db, filetree, people, promptbudget

log = logging.getLogger("portal.crossproject")

# One file may put this many bytes into a tool result. Well under the 500 KB the
# browser's file viewer allows: this text goes into an agent's context window,
# where half a megabyte of somebody else's source is not a favor.
MAX_FILE_BYTES = 60 * 1024

# Entries in one directory listing. `node_modules` exists.
LISTING_CAP = 200

# Recent journal entries in a context digest, and how much of each. `digest`
# gives the heading plus the opening paragraph, which is the shape the prompt
# builder already falls back to for older entries.
JOURNAL_ENTRIES = 8
JOURNAL_DIGEST_CAP = 600

# Related projects named in a run's prompt. Past a handful this stops being a
# pointer and starts being a second dashboard.
RELATED_CAP = 6

# A slug token in more than this share of readable projects groups nothing.
DF_SHARE = 0.4

# Tokens that appear in slugs and mean nothing about what a project is. Kept
# short on purpose - a stop list that grows to cover every generic-sounding word
# eventually eats a real one ("case", "board" and "home" all group real work).
#
# Everything here is at least three characters, because anything shorter is
# already dropped by the length floor in `_tokens`. A first draft also listed
# "a", "an", "my", "of", "or", "to" and "s"; a mutation sweep found that
# deleting the whole list changed nothing a test could see, which is what
# unreachable configuration looks like from the outside.
STOP_TOKENS = frozenset(
    {
        "and", "app", "com", "for", "her", "his", "net", "new", "org",
        "page", "project", "site", "the", "tool", "tools", "web", "www",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Denied(Exception):
    """A project this run may not read, or one that is not there.

    One exception for both cases on purpose - see the module docstring.
    """


def enabled() -> bool:
    return (db.get_setting("cross_project") or "1") != "0"


# ------------------------------------------------------------------ who


def _principal_id(reader_id: int) -> Optional[int]:
    person = people.principal(int(reader_id))
    return int(person["id"]) if person is not None else None


def readable(reader_id: int) -> list[sqlite3.Row]:
    """Every project a run on `reader_id` may read, most recently worked first.

    Excludes the reading project itself: a run reading its own context back
    through a tool has learned nothing its prompt did not already carry, and an
    agent that can ask the portal about itself will.
    """
    if not enabled():
        return []
    reader_id = int(reader_id)
    person_id = _principal_id(reader_id)
    mine = people.project_ids_for(person_id) if person_id is not None else set()
    memberless = db.memberless_project_ids()
    allowed = mine | memberless
    rows = db.list_projects(order_by="updated_at DESC")
    return [r for r in rows if int(r["id"]) != reader_id and int(r["id"]) in allowed]


def resolve(reader_id: int, slug: str) -> sqlite3.Row:
    """The readable project with this slug, or `Denied`."""
    wanted = str(slug or "").strip().strip("/").lower()
    if not wanted:
        raise Denied("Name a project by its slug. Use the `projects` tool to see them.")
    for row in readable(reader_id):
        if str(row["slug"]).lower() == wanted:
            return row
    raise Denied(
        f"No project `{wanted}` you can read. Use the `projects` tool for the "
        f"list you can."
    )


# ------------------------------------------------------------- relatedness


def _tokens(slug: str) -> list[str]:
    """A slug's significant tokens, in order and deduped.

    Order is load-bearing: relatedness is a shared *prefix*, not a shared word.
    """
    out: list[str] = []
    for tok in _TOKEN_RE.findall(str(slug or "").lower()):
        if len(tok) >= 3 and tok not in STOP_TOKENS and tok not in out:
            out.append(tok)
    return out


def _shared_prefix(mine: list[str], theirs: list[str]) -> list[str]:
    """The tokens two slugs agree on from the left.

    Why the left rather than anywhere: Wes names a slug `parent-then-detail`
    (`commander-case-custom-lid`, `board-games-tak`, `kingshot-gift-code`), so
    the leading tokens say what a project is *about* and later ones say which
    one it is. Matching on any shared token instead put `secret-shopper-helper`
    next to `board-games-secret-hitler`, which share the word "secret" and
    nothing else at all. Precision matters more than recall here: a missed
    neighbor is still one `projects` call away, while a wrong one spends prompt
    bytes pointing a run at work that has nothing to do with it.
    """
    shared: list[str] = []
    for a, b in zip(mine, theirs):
        if a != b:
            break
        shared.append(a)
    return shared


def _df_ceiling(n_projects: int) -> int:
    """How many projects a token may appear in and still group them.

    Never below 2, or a token shared by exactly two projects on a small board
    would be filtered out as generic and nothing would ever be related.
    """
    return max(2, math.floor(DF_SHARE * max(1, n_projects)))


def family_ids(project: sqlite3.Row) -> set[int]:
    """This project's parent, its siblings and its children.

    Not readable-filtered: this is "who is already named in the prompt by
    `subprojects.prompt_section`", and that section names them regardless.
    """
    ids: set[int] = set()
    parent_id = db.parent_id_of(project)
    if parent_id:
        ids.add(int(parent_id))
        ids.update(int(r["id"]) for r in db.child_projects(int(parent_id)))
    ids.update(int(r["id"]) for r in db.child_projects(int(project["id"])))
    ids.discard(int(project["id"]))
    return ids


def related(reader_id: int, cap: int = RELATED_CAP) -> list[sqlite3.Row]:
    """The readable NON-FAMILY projects whose slugs put them in the same body of
    work.

    Scored by the rarity of what they share, so a project sharing `commander`
    with four others outranks one sharing `board` with ten.

    Family is excluded because `subprojects.prompt_section` already names every
    parent, sibling and child directly above this section in the prompt, and on
    this board the strongest slug groupings *are* families - `board-games-tak`
    would otherwise carry a second copy of all seven of its siblings.
    """
    reader = db.get_project(int(reader_id))
    if reader is None:
        return []
    kin = family_ids(reader)
    others = [r for r in readable(reader_id) if int(r["id"]) not in kin]
    if not others:
        return []

    mine = _tokens(reader["slug"])
    if not mine:
        return []

    freq: dict[str, int] = {}
    per_project: dict[int, list[str]] = {}
    for row in others:
        toks = _tokens(row["slug"])
        per_project[int(row["id"])] = toks
        for tok in toks:
            freq[tok] = freq.get(tok, 0) + 1
    # The reader counts toward its own tokens' frequency: a token it shares with
    # one other project appears twice on the board, not once, and the ceiling is
    # a statement about the board.
    for tok in mine:
        freq[tok] = freq.get(tok, 0) + 1

    ceiling = _df_ceiling(len(others) + 1)
    scored: list[tuple[float, str, sqlite3.Row]] = []
    for row in others:
        shared = _shared_prefix(mine, per_project[int(row["id"])])
        # Only the tokens rare enough to group anything score. A prefix whose
        # first token is on every project's slug still counts through its
        # second, which is what keeps `board-games-*` together on a board where
        # "board" is everywhere.
        score = sum(1.0 / freq[tok] for tok in shared if freq[tok] <= ceiling)
        if score > 0:
            scored.append((score, str(row["title"] or "").lower(), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _score, _title, row in scored[:cap]]


def prompt_section(project: sqlite3.Row, offered: bool = True) -> str:
    """The `## Reading other projects` block of a run prompt, or ''.

    `offered` is whether this run actually carries the portal's MCP tools -
    `portalmcp.carries_tools(task)`. A prompt that names a tool the run was not
    given is worse than one that names nothing, so the section is empty on the
    tasks that get no MCP server.

    Empty when this project has neither family nor a related neighbor, which
    keeps an ordinary prompt on a one-of-a-kind project byte-for-byte unchanged.
    """
    if not offered or not enabled():
        return ""
    try:
        neighbors = related(int(project["id"]))
        kin = [
            row
            for row in readable(int(project["id"]))
            if int(row["id"]) in family_ids(project)
        ]
    except Exception:  # pragma: no cover - defensive; never lose a run over this
        log.exception("related() failed for %s", project["slug"])
        return ""
    if not neighbors and not kin:
        return ""

    lines = ["## Reading other projects"]
    if kin:
        # By slug only: their titles, states and descriptions are directly above
        # in the sub-project section, and the one thing that section does not
        # give is the slug these tools take.
        slugs = ", ".join(f"`{row['slug']}`" for row in kin)
        lines.append(
            "The family named above is readable from here - their slugs are "
            f"{slugs}."
        )
    if neighbors:
        lines.append(
            "These are other projects on this portal that look like they belong "
            "to the same body of work as this one. They are separate projects "
            "with their own workspaces, journals and runs - do NOT do their "
            "work here - but you can read them, and you should when this "
            "project's job depends on something one of them already worked out:"
        )
        lines.append("")
        for row in neighbors:
            desc = _one_line(row["description"] or row["initial_idea"])
            lines.append(
                f"- **{row['title']}** (`{row['slug']}`, {db.display_state(row)})"
                + (f" - {desc}" if desc else "")
            )
    lines.extend(
        [
            "",
            "Read one with the `project_context` tool (its brief, todo list and "
            "recent journal) and `project_files` (its workspace). Prefer that "
            "over asking a person to re-explain something another project "
            "already knows - that is exactly what these tools are for. "
            "`projects` lists every project you can read, not just these.",
        ]
    )
    return "\n".join(lines)


def _one_line(text: Optional[str], limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rsplit(" ", 1)[0] + "…"


# ------------------------------------------------------------- the answers


def listing(reader_id: int) -> str:
    """Every project this run may read, one per line."""
    rows = readable(reader_id)
    if not rows:
        return "There are no other projects on this portal you can read."
    near = {int(r["id"]) for r in related(reader_id)}
    lines = [
        f"{len(rows)} project(s) you can read. Use `project_context` with a slug "
        f"for any of them.",
        "",
    ]
    for row in rows:
        desc = _one_line(row["description"] or row["initial_idea"], 120)
        mark = " [related to yours]" if int(row["id"]) in near else ""
        lines.append(
            f"- `{row['slug']}` - **{row['title']}** ({db.display_state(row)})"
            f"{mark}" + (f" - {desc}" if desc else "")
        )
    return "\n".join(lines)


def digest(reader_id: int, slug: str) -> str:
    """Another project's context as prose: brief, state, todos, recent journal.

    Deliberately the same material this project's own run prompt is built from,
    shortened - a reader wants what the other project *knows*, and that is its
    description, what is on its list and what its last few runs reported.
    """
    project = resolve(reader_id, slug)
    pid = int(project["id"])
    workspace = config.PROJECTS_DIR / str(project["slug"])

    out = [
        f"# {project['title']} (`{project['slug']}`)",
        "",
        f"**State:** {db.display_state(project)}"
        + (f" - blocked on: {db.blocked_on(project)}" if db.blocked_on(project) else ""),
        f"**Kind:** {project['kind'] or 'unspecified'}",
        f"**Workspace:** `{workspace}`",
        "",
        "## What it is",
        " ".join((project["description"] or "").split()) or "(no description yet)",
    ]

    idea = " ".join((project["initial_idea"] or "").split())
    if idea:
        out += ["", "## The brief it started from", f"> {idea}"]

    out += ["", "## Its todo list", _todo_lines(pid)]

    entries = db.list_journal_asc(pid, limit=JOURNAL_ENTRIES, exclude=db.SIDE_THREAD)
    out += ["", f"## Its last {len(entries)} journal entries", _journal_lines(entries)]

    kids = db.child_projects(pid)
    if kids:
        out += [
            "",
            "## Its sub-projects",
            "\n".join(f"- `{k['slug']}` - {k['title']}" for k in kids),
        ]

    out += [
        "",
        f"Its files are in `{workspace}`. Browse them with `project_files` "
        f"(slug `{project['slug']}`).",
    ]
    return "\n".join(out)


def _todo_lines(project_id: int) -> str:
    rows = [r for r in db.list_todos(project_id) if not r["done"]]
    if not rows:
        return "(nothing open)"
    return "\n".join(
        f"- [{r['owner']}] {' '.join((r['text'] or '').split())}" for r in rows
    )


def _journal_lines(entries: Iterable[sqlite3.Row]) -> str:
    lines = []
    for row in entries:
        body = promptbudget.digest(row["content_md"] or "", cap=JOURNAL_DIGEST_CAP)
        lines.append(f"- [{row['ts']}] {row['author']}/{row['kind']}: {body}")
    return "\n".join(lines) or "(no entries yet)"


# --------------------------------------------------------------- the files


@dataclass(frozen=True)
class _Resolved:
    project: sqlite3.Row
    workspace: Path
    target: Path


def _inside(reader_id: int, slug: str, path: str) -> _Resolved:
    """Resolve `path` inside another project's workspace, refusing escapes.

    resolve() collapses symlinks before the containment test, so a link inside
    that workspace pointing at `~/.ssh` is rejected here rather than followed -
    the same rule `main._workspace_file` applies to the browser.
    """
    project = resolve(reader_id, slug)
    workspace = (config.PROJECTS_DIR / str(project["slug"])).resolve()
    if not workspace.is_dir():
        raise Denied(f"`{project['slug']}` has no workspace on disk yet.")
    rel = str(path or "").strip().lstrip("/")
    target = (workspace / rel).resolve() if rel else workspace
    try:
        target.relative_to(workspace)
    except ValueError:
        raise Denied(
            f"`{rel}` is outside `{project['slug']}`'s workspace."
        ) from None
    return _Resolved(project=project, workspace=workspace, target=target)


def browse(reader_id: int, slug: str, path: str = "") -> str:
    """List a directory or read a file in another project's workspace."""
    found = _inside(reader_id, slug, path)
    rel = found.target.relative_to(found.workspace).as_posix()
    rel = "" if rel == "." else rel
    if not found.target.exists():
        return (
            f"`{rel or '/'}` is not in {found.project['slug']}'s workspace. "
            f"Call this with no path to see what is."
        )
    if found.target.is_dir():
        return _render_listing(found, rel)
    return _render_file(found, rel)


def _render_listing(found: _Resolved, rel: str) -> str:
    entries = filetree.children(found.workspace, rel)
    where = f"{found.project['slug']}/{rel}" if rel else str(found.project["slug"])
    if not entries:
        return f"`{where}` is empty."
    shown = entries[:LISTING_CAP]
    lines = [f"`{where}` contains {len(entries)} entr(y/ies):", ""]
    for entry in shown:
        lines.append(f"- {entry.path}/" if entry.is_dir else f"- {entry.path}")
    if len(entries) > len(shown):
        lines.append(f"- … and {len(entries) - len(shown)} more, not listed")
    return "\n".join(lines)


def _render_file(found: _Resolved, rel: str) -> str:
    size = found.target.stat().st_size
    head = f"`{found.project['slug']}/{rel}` ({size:,} bytes)"
    raw = found.target.read_bytes()[:MAX_FILE_BYTES]
    if b"\x00" in raw:
        # An image is the most useful binary in one of these workspaces - a
        # screenshot is how a project shows what it built. hookguard permits a
        # *read* of any path outside the portal's credentials, so pointing at
        # the file is not a way around the fence; it is the fence's read side.
        return (
            f"{head} is a binary file, so there is nothing to quote. Its full "
            f"path is `{found.target}` - if it is an image or a PDF, your own "
            f"Read tool will open it from there."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    if size > MAX_FILE_BYTES:
        head += f", showing the first {MAX_FILE_BYTES:,}"
        text += f"\n\n… truncated at {MAX_FILE_BYTES:,} bytes …"
    return f"{head}:\n\n{text}"


# --------------------------------------------------------------- the tools


def tool_specs(name: str) -> list[dict]:
    """The three MCP tool definitions, addressed to the run's own principal.

    `name` is that person's name, only so the descriptions can say whose
    re-explaining these tools exist to spare.
    """
    return [
        {
            "name": "projects",
            "description": (
                "List the OTHER projects on this portal that you are allowed to "
                "read - their slugs, titles, state and one-line descriptions.\n\n"
                "Use it when you suspect another project has already worked "
                "something out that this one needs, and you do not know its "
                "slug. Then read it with `project_context`."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "project_context",
            "description": (
                "Read another project's context: what it is, the brief it "
                "started from, its open todo list and the headings of its "
                "recent journal entries.\n\n"
                f"This is how you learn what a related project already knows "
                f"without making {name} re-explain it. Reach for it whenever "
                f"this project's work depends on decisions, data or vocabulary "
                f"that belong to another one - a shared product, a shared "
                f"customer, a tool this project is a piece of. Read before you "
                f"ask.\n\n"
                "Takes the project's slug, which `projects` lists."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "The other project's slug, e.g. `commander-case`.",
                    }
                },
                "required": ["slug"],
            },
        },
        {
            "name": "project_files",
            "description": (
                "Look inside another project's workspace: with no path, the "
                "files at its root; with a directory, what is in it; with a "
                "file, its text.\n\n"
                "Your own Bash and file tools are fenced out of other projects' "
                "workspaces, so this is the way to read one. Useful for the "
                "source of a tool this project talks to, a README, a data file "
                "or a plan another project wrote."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "The other project's slug.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "A path relative to that workspace's root. Leave it "
                            "out for the root listing."
                        ),
                    },
                },
                "required": ["slug"],
            },
        },
    ]


TOOL_NAMES = frozenset({"projects", "project_context", "project_files"})


def handle(reader_id: int, name: str, args: Any) -> str:
    """Run one of these tools and return its text. Raises `Denied` for a refusal."""
    if not isinstance(args, dict):
        args = {}
    if not enabled():
        raise Denied("Cross-project reading is switched off on this portal.")
    if name == "projects":
        return listing(reader_id)
    if name == "project_context":
        return digest(reader_id, str(args.get("slug") or ""))
    if name == "project_files":
        return browse(reader_id, str(args.get("slug") or ""), str(args.get("path") or ""))
    raise Denied(f"No such tool: {name}")
