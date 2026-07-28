"""Splitting a project into sub-projects.

Wes's ask, in his words:

    "Create a class/object for sub-projects where something can be split out
    like my project involving making different games to host on my site. It has
    many games in there, and the smart thing for it to do would be to split them
    into multiple subprojects that live under that original project that I
    spawned in. Some games won't be developed for now, but they will also all
    need their own context separate from one another."

The last sentence is the whole design constraint. "Their own context" is not a
tab on a page - it is a separate journal, a separate todo list, a separate run
history, a separate workspace and a separate prompt. The portal already has
exactly one thing shaped like that, and it is a project. So a sub-project *is*
a project, with `parent_id` pointing at the one it was split out of, and it
inherits every feature projects have for free: runs, questions, todos, the
build gate, research bursts, the file tree.

Three deliberate limits:

* **One level deep.** A sub-project cannot itself be split. Wes asked for games
  under a games project, not a tree; and a hierarchy of unbounded depth turns
  every listing, every prompt and every delete into a graph walk that has to
  worry about cycles. `create_child` refuses, loudly, rather than silently
  re-parenting to the grandparent.

* **Workspaces stay flat.** A child's files live in its own top-level folder,
  not inside the parent's. Nesting them would put every child's git repo inside
  the parent's, make the parent's file tree (and its orphaned-work scan) walk
  all of them, and give a parent's agent write access to its children's code.
  The relationship is carried by the *name* instead - a child of `board-games`
  is `board-games-catan` - so the folder still reads as belonging to the parent
  from a shell.

* **Nothing is moved.** Splitting creates new, empty projects beside the parent;
  it never migrates the parent's journal, todos or files into them. An agent
  deciding which of forty journal entries belongs to which game would be
  guessing, and a wrong guess loses work that Wes cannot get back.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Iterable, Optional

from app import config, db

log = logging.getLogger(__name__)

# How many children one report may create in one go. A split is a handful of
# deliverables; anything past this is a runaway loop or a misread list, and
# fifty new projects on the dashboard is not something Wes can undo with one
# click.
MAX_CHILDREN_PER_REPORT = 12


class SplitError(ValueError):
    """A sub-project could not be created. The message is shown to Wes."""


def child_slug(parent: sqlite3.Row, title: str) -> str:
    """The folder name for a new child: the parent's slug, then a short name.

    `board-games` + "Settlers of Catan" -> `board-games-catan`.

    The prefix is what makes the flat workspace layout readable: `ls` in the
    projects directory groups a family together, and the ssh line for a child
    says which project it belongs to without anyone having to look it up.
    """
    stem = db.short_slug(title or "")
    parent_slug = (parent["slug"] or "").strip("-")
    # A child titled the same as its parent ("Board games" under `board-games`)
    # would otherwise become `board-games-board-games`.
    if stem and (parent_slug == stem or parent_slug.endswith(f"-{stem}")):
        base = parent_slug
    else:
        base = f"{parent_slug}-{stem}" if parent_slug else stem
    return db.unique_slug(base)


def can_have_children(project: Optional[sqlite3.Row]) -> bool:
    """Only a top-level project may be split. See the one-level rule above."""
    return project is not None and db.parent_id_of(project) is None


def create_child(
    parent: sqlite3.Row,
    title: str,
    description: str = "",
    kind: str = "",
) -> sqlite3.Row:
    """Split one deliverable out of `parent` into a project of its own."""
    title = db.clean_title(title or "")
    if not title:
        raise SplitError("A sub-project needs a title.")
    if not can_have_children(parent):
        raise SplitError(
            f"{parent['title']} is itself a sub-project, and sub-projects cannot "
            "be split again. Add this to its parent instead."
        )
    if kind not in config.PROJECT_KINDS:
        # An unknown kind from an agent report is dropped rather than stored:
        # the child inherits the parent's, which is nearly always right (every
        # game under a games project is software) and is at worst a field Wes
        # can change on the details form.
        kind = parent["kind"]

    # Children start in the backlog whatever the parent is doing. Wes was
    # explicit that "some games won't be developed for now" - so a split must
    # not commit him to working on every piece of it: nothing runs on a child
    # until he (or a note) activates it, exactly like any other idea.
    #
    # Priority steps down one from the parent so a family cannot crowd the run
    # rotation out from under everything else on the dashboard.
    child = db.create_project(
        title=title,
        description=(description or "").strip(),
        kind=kind,
        stage="backlog",
        priority=max(0, (parent["priority"] or 0) - 1),
        slug=child_slug(parent, title),
        parent_id=parent["id"],
    )

    # Build approval is inherited. Wes approving "board games for my site" was
    # approval to write the games; making him press the button again once per
    # game would be the portal forgetting a decision he has already made. The
    # child is still in the backlog, so nothing starts building until it is
    # activated in the ordinary way.
    if db.build_approved(parent):
        db.update_project(child["id"], build_approved=1)
        child = db.get_project(child["id"])

    db.add_journal(
        child["id"], "system", "status",
        f"Split out of **[{parent['title']}](/project/{parent['slug']})**. "
        f"This sub-project has its own workspace, journal, todo list and runs.",
    )
    db.add_journal(
        parent["id"], "system", "status",
        f"Sub-project created: **[{child['title']}](/project/{child['slug']})** "
        f"(`{child['slug']}`).",
    )
    log.info("Split %s out of %s", child["slug"], parent["slug"])
    return child


def move_workspace(slug: str, target: str) -> None:
    """Rename a workspace directory on disk.

    Both names are built from user-controlled text, so the source and the
    destination must each resolve to a direct child of the projects root. A
    missing source is fine - a project that never ran has no folder yet, and
    the right folder appears under the new name on its first run.
    """
    root = config.PROJECTS_DIR.resolve()
    src = (config.PROJECTS_DIR / slug).resolve()
    dst = (config.PROJECTS_DIR / target).resolve()
    if src.parent != root or dst.parent != root or src == root or dst == root:
        raise SplitError("Invalid workspace name.")
    if dst.exists():
        raise SplitError(f"`{target}` already exists on disk.")
    if src.is_dir():
        src.rename(dst)


def can_become_child(project: sqlite3.Row) -> bool:
    """Whether the convert-to-sub-project controls should be offered at all.

    Mirrors the hard guards in `adopt` that no choice of parent can fix: the
    portal's own project stays top-level, and a project with children of its
    own cannot become one (one level deep, always).
    """
    return (
        project["slug"] != config.META_PROJECT_SLUG
        and not db.child_projects(project["id"])
    )


def adoptive_parents(project: sqlite3.Row) -> list[sqlite3.Row]:
    """The projects this one could become a sub-project of: every top-level
    project except itself and its current parent, alphabetical so the dropdown
    is scannable."""
    current = db.parent_id_of(project)
    return [
        row
        for row in db.list_projects("title COLLATE NOCASE ASC")
        if db.parent_id_of(row) is None
        and row["id"] != project["id"]
        and row["id"] != current
    ]


def _family_slug(parent: sqlite3.Row, project: sqlite3.Row) -> str:
    """The folder name an adopted project takes: the parent's slug, then the
    name the project already had (minus a previous parent's prefix, when it is
    moving between families). Built from the existing slug rather than
    re-derived from the title, because the current folder name is the one Wes
    and every past run's notes already know."""
    parent_slug = (parent["slug"] or "").strip("-")
    stem = (project["slug"] or "").strip("-")
    old_pid = db.parent_id_of(project)
    if old_pid:
        old_parent = db.get_project(old_pid)
        old_slug = (old_parent["slug"] or "").strip("-") if old_parent else ""
        if old_slug and stem.startswith(old_slug + "-"):
            stem = stem[len(old_slug) + 1 :]
    candidate = stem if stem.startswith(parent_slug + "-") else f"{parent_slug}-{stem}"
    if candidate == project["slug"]:
        # Already named for this family (a re-adopted release keeps its
        # folder). unique_slug would see the name as taken - by this very
        # project - and wrongly append -2.
        return candidate
    return db.unique_slug(candidate)


def adopt(parent: sqlite3.Row, project: sqlite3.Row) -> sqlite3.Row:
    """Convert an existing project into a sub-project of `parent`.

    Wes's ask, in his words: "Be able to convert a project to a subproject of
    another project without losing stuff." Nothing is copied, so nothing can be
    lost: the journal, todos, questions, runs and attachment index all hang off
    the project id, which does not change, and the files move in one directory
    rename. What changes is the parent pointer and the folder name, which takes
    the family scheme (`tak` under `board-games` -> `board-games-tak`) so a
    shell listing still groups the family together.
    """
    if project["id"] == parent["id"]:
        raise SplitError(f"{project['title']} cannot be its own sub-project.")
    if project["slug"] == config.META_PROJECT_SLUG:
        raise SplitError(
            "The portal's own project stays top-level - the self-update check "
            "is pinned to its slug, and converting would rename it."
        )
    if not can_have_children(parent):
        raise SplitError(
            f"{parent['title']} is itself a sub-project, and sub-projects only "
            "go one level deep. Pick its parent instead."
        )
    own = db.child_projects(project["id"])
    if own:
        names = ", ".join(row["title"] for row in own[:5])
        raise SplitError(
            f"{project['title']} has {len(own)} sub-project"
            f"{'s' if len(own) != 1 else ''} of its own ({names}) and sub-projects "
            "only go one level deep. Re-home or delete them first."
        )
    if db.parent_id_of(project) == parent["id"]:
        raise SplitError(
            f"{project['title']} is already a sub-project of {parent['title']}."
        )
    if project["id"] in db.running_project_ids():
        raise SplitError(
            "An agent is running in this workspace right now, and converting "
            "renames the folder out from under it. Stop the run, then convert."
        )

    old_slug = project["slug"]
    new_slug = _family_slug(parent, project)
    if new_slug != old_slug:
        move_workspace(old_slug, new_slug)
    db.update_project(project["id"], parent_id=parent["id"], slug=new_slug)

    renamed = (
        f" Its workspace was renamed `{old_slug}` -> `{new_slug}`."
        if new_slug != old_slug
        else ""
    )
    db.add_journal(
        project["id"], "user", "status",
        f"Converted into a sub-project of "
        f"**[{parent['title']}](/project/{parent['slug']})**. Journal, todos, "
        f"questions, runs and files all came along.{renamed}",
    )
    db.add_journal(
        parent["id"], "user", "status",
        f"**[{project['title']}](/project/{new_slug})** (`{new_slug}`) was "
        f"converted into a sub-project of this one.",
    )
    log.info("Converted %s into a child of %s", new_slug, parent["slug"])
    return db.get_project(project["id"])


def release(project: sqlite3.Row) -> sqlite3.Row:
    """Promote a sub-project back to a top-level project - the inverse of
    `adopt`, minus the rename. The folder keeps its name, both because a slug
    is just a directory name now (they no longer track titles) and because not
    moving it means a running agent cannot be disturbed, so unlike a conversion
    there is nothing to guard."""
    pid = db.parent_id_of(project)
    if not pid:
        raise SplitError(f"{project['title']} is already a top-level project.")
    parent = db.get_project(pid)
    db.update_project(project["id"], parent_id=None)
    if parent is not None:
        db.add_journal(
            project["id"], "user", "status",
            f"Promoted to a top-level project (was a sub-project of "
            f"**[{parent['title']}](/project/{parent['slug']})**). The workspace "
            f"keeps its folder name, `{project['slug']}`.",
        )
        db.add_journal(
            parent["id"], "user", "status",
            f"Sub-project **[{project['title']}](/project/{project['slug']})** "
            f"is now a top-level project of its own.",
        )
    log.info("Released %s from %s", project["slug"], parent["slug"] if parent else "?")
    return db.get_project(project["id"])


def apply_report(project: sqlite3.Row, report: dict) -> list[sqlite3.Row]:
    """Create the sub-projects an agent's report asked for.

    Shape (everything optional, like the rest of the report):

        "subprojects": {"add": [{"title": "...", "description": "...",
                                 "kind": "software"}]}

    Every failure here is logged and skipped rather than raised. This runs
    inside `_apply_report`, after the journal entry is already written; letting
    one malformed entry abort the rest would throw away the todos, learnings and
    questions of a run that otherwise succeeded.
    """
    spec = report.get("subprojects")
    if not isinstance(spec, dict):
        # Tolerate a bare list - it is the shape a model reaches for first, and
        # rejecting it would silently drop a split Wes asked for.
        spec = {"add": spec} if isinstance(spec, list) else {}
    entries = spec.get("add")
    if not isinstance(entries, list) or not entries:
        return []

    if not can_have_children(project):
        log.warning(
            "Ignoring %d sub-projects from %s: it is itself a sub-project",
            len(entries), project["slug"],
        )
        db.add_journal(
            project["id"], "system", "status",
            "This run asked to create sub-projects, but sub-projects cannot be "
            "split again. Nothing was created.",
        )
        return []

    created: list[sqlite3.Row] = []
    # Titles already used by this family, so a run that repeats last run's list
    # does not create a second copy of every game. Compared on the normalized
    # title rather than the slug, because the slug is uniquified and would
    # therefore never collide.
    existing = {
        _norm(row["title"]) for row in db.child_projects(project["id"])
    }
    for entry in entries[:MAX_CHILDREN_PER_REPORT]:
        if not isinstance(entry, dict):
            continue
        title = db.clean_title(entry.get("title") or "")
        if not title or _norm(title) in existing:
            continue
        try:
            created.append(create_child(
                project, title,
                description=entry.get("description") or "",
                kind=entry.get("kind") or "",
            ))
        except (SplitError, sqlite3.Error) as exc:
            log.warning("Could not split %r out of %s: %s", title, project["slug"], exc)
            continue
        existing.add(_norm(title))
    if len(entries) > MAX_CHILDREN_PER_REPORT:
        log.warning(
            "Report on %s asked for %d sub-projects; created the first %d",
            project["slug"], len(entries), MAX_CHILDREN_PER_REPORT,
        )
    return created


def _norm(title: str) -> str:
    return " ".join((title or "").lower().split())


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

# Told to a top-level project that has no children yet. This is the "have agents
# notice when a project should be split" half of the ask: an agent will not
# invent a feature nothing in its prompt mentions.
_SPLIT_HINT = """\
## Sub-projects

This project has none. If it is really several independent deliverables that
each want their own context - the way a "games for my site" project is really
one project per game - split it, by putting them in your report:

  "subprojects": {"add": [{"title": "...", "description": "one or two sentences"}]}

Each one becomes a project in its own right, with its own workspace, journal,
todo list and runs, listed under this one. Only split when the pieces genuinely
have separate contexts and could be built in any order - phases of one build
belong on the todo list, not in separate projects. Nothing is moved when you
split: the children start empty, so anything of theirs already in this
workspace has to be copied over by a later run.\
"""


def prompt_section(project: sqlite3.Row) -> str:
    """The sub-project block of a run prompt. May be empty."""
    parent_id = db.parent_id_of(project)
    if parent_id:
        parent = db.get_project(parent_id)
        if parent is None:  # parent deleted out from under it
            return ""
        siblings = [
            row for row in db.child_projects(parent_id) if row["id"] != project["id"]
        ]
        lines = [
            "## This is a sub-project",
            f"It is a piece of **{parent['title']}** (`{parent['slug']}`), whose "
            f"brief is:",
            f"> {_one_line(parent['initial_idea'] or parent['description'])}",
            "",
            "Work only on your own piece of that. Your workspace, journal and todo "
            "list are yours alone - the parent's are separate, and so are your "
            f"sibling sub-projects'. Your files are in `{config.PROJECTS_DIR / project['slug']}`, "
            "not in the parent's workspace. You cannot be split again.",
        ]
        if siblings:
            lines.append("")
            lines.append("Sibling sub-projects (do not do their work):")
            lines.extend(f"- {row['title']} ({db.display_state(row)})" for row in siblings)
        return "\n".join(lines)

    children = db.child_projects(project["id"])
    if not children:
        return _SPLIT_HINT
    lines = [
        "## Sub-projects",
        "This project has been split. Each of these is a project in its own "
        "right, with its own workspace, journal, todos and runs - an agent runs "
        "on each of them separately, so do NOT do their work here:",
        "",
    ]
    for row in children:
        desc = _one_line(row["description"] or row["initial_idea"])
        lines.append(
            f"- **{row['title']}** (`{row['slug']}`, {db.display_state(row)})"
            + (f" - {desc}" if desc else "")
        )
    lines.append("")
    lines.append(
        "Your job here is the part that is common to all of them - the shared "
        "plan, the shared decisions, the umbrella. Add another with "
        '`"subprojects": {"add": [{"title": "...", "description": "..."}]}` in '
        "your report; one that already exists (by title) is ignored, so it is "
        "safe to leave the list out entirely."
    )
    return "\n".join(lines)


def _one_line(text: Optional[str], limit: int = 160) -> str:
    """First sentence-ish of a description, for a one-per-line listing."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------
# Guards for the rest of the portal
# --------------------------------------------------------------------------

def blocks_delete(project: sqlite3.Row) -> Optional[str]:
    """Why this project may not be deleted yet, or None.

    A parent row deleted out from under its children would leave them pointing
    at nothing: they would vanish from the dashboard's family grouping, keep a
    dangling `parent_id`, and still be sitting on disk. Refusing and saying
    which children are in the way is better than either cascading (deleting
    projects Wes did not name) or orphaning them silently.
    """
    children = db.child_projects(project["id"])
    if not children:
        return None
    names = ", ".join(row["title"] for row in children[:5])
    more = f" and {len(children) - 5} more" if len(children) > 5 else ""
    return (
        f"{project['title']} has {len(children)} sub-project"
        f"{'s' if len(children) != 1 else ''} ({names}{more}). Delete or re-home "
        "them first."
    )


def group_for_shelf(projects: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Order one dashboard shelf so each child follows its parent.

    Children stay ordinary cards - clickable, with their own status badge - and
    gain an indent plus the parent's name. The alternative, hiding them inside
    the parent's card, would mean a sub-project that an agent is actively
    building on is invisible from the dashboard, which is the one place Wes
    looks to see what is happening.

    A child whose parent is on a *different* shelf (or has none on this page)
    is not dropped: it is emitted in its own position with the parent label
    still attached, because "not shown at all" is the worst possible answer.
    """
    rows = list(projects)
    by_parent: dict[int, list[sqlite3.Row]] = {}
    here = {row["id"] for row in rows}
    for row in rows:
        pid = db.parent_id_of(row)
        if pid and pid in here:
            by_parent.setdefault(pid, []).append(row)

    out: list[dict[str, Any]] = []
    for row in rows:
        pid = db.parent_id_of(row)
        if pid and pid in here:
            continue  # emitted under its parent below
        parent_title = ""
        if pid:
            parent = db.get_project(pid)
            parent_title = parent["title"] if parent else ""
        out.append({"project": row, "child": bool(pid), "parent_title": parent_title})
        for kid in by_parent.get(row["id"], []):
            out.append({"project": kid, "child": True, "parent_title": row["title"]})
    return out
