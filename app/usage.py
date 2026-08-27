"""Usage aggregated over time, plus the ASCII bars that draw it.

`usage_snapshot()` in `app.main` answers "how much budget is left *today*". This
module answers the other half - what has the portal actually been doing over the
last N days, what did it cost, and how often did it fail. Everything here is a
pure function over rows so it can be tested without a running worker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from app import db

# Eighth-blocks, lightest to fullest. Index 0 is reserved for "nothing at all",
# so a day with zero runs reads as a gap rather than as a very small bar.
BARS = " ▁▂▃▄▅▆▇█"


# --------------------------------------------------------------------------
# How to render the CLI's cost figure
# --------------------------------------------------------------------------
# `total_cost_usd` from the Claude CLI is what those tokens *would* cost at API
# rates. Wes is on a Max subscription and is not billed per token, so a dollar
# sign on these numbers reads as a bill he isn't receiving. Default to calling
# it "weight" - same magnitude, same ordering, no false claim about money - and
# keep the honest-dollars rendering available for anyone who wants it.
COST_UNIT_CHOICES = ("weight", "usd")
DEFAULT_COST_UNITS = "weight"
COST_NOUNS = {"weight": "weight", "usd": "cost"}


def cost_units() -> str:
    value = db.get_setting("cost_units") or DEFAULT_COST_UNITS
    return value if value in COST_UNIT_CHOICES else DEFAULT_COST_UNITS


def cost_noun(units: Optional[str] = None) -> str:
    """The column heading / label for the cost figure in the active units."""
    return COST_NOUNS[(units or cost_units())]


def format_cost(value: Any, precision: int = 3, units: Optional[str] = None) -> str:
    """Render a cost figure. `None` (a run the CLI didn't price) renders as '-'
    rather than as zero, which would claim the run was free."""
    if value is None:
        return "-"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    if (units or cost_units()) == "usd":
        return f"${amount:.{precision}f}"
    return f"{amount:.{precision}f}w"


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def run_duration(row: Any) -> Optional[int]:
    """Wall-clock seconds for a finished run, or None while it is still going."""
    started, ended = _parse(_get(row, "started_at")), _parse(_get(row, "ended_at"))
    if started is None or ended is None:
        return None
    return max(0, int((ended - started).total_seconds()))


def _get(row: Any, key: str) -> Any:
    """Read a field from either a sqlite3.Row or a plain dict."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def sparkline(values: Sequence[float]) -> str:
    """One block glyph per value, scaled against the largest.

    Scaling is relative because the interesting thing about a run-count series
    is its shape, not its absolute height - the numbers are printed alongside.
    """
    if not values:
        return ""
    peak = max(values)
    if peak <= 0:
        return " " * len(values)
    out = []
    for value in values:
        if value <= 0:
            out.append(BARS[0])
        else:
            # 1..8, so any non-zero day is visible.
            level = 1 + int(round((len(BARS) - 2) * (value / peak)))
            out.append(BARS[min(level, len(BARS) - 1)])
    return "".join(out)


def _empty_day(date: str) -> dict:
    return {
        "date": date,
        "runs": 0,
        "ok": 0,
        "failed": 0,
        "cancelled": 0,
        "cost": 0.0,
        "turns": 0,
        "duration_sec": 0,
        "finished": 0,
    }


def bucket_by_day(rows: Iterable[Any], days: int, today: Optional[str] = None) -> list[dict]:
    """Group runs into one entry per UTC day, oldest first.

    Days with no runs are still present - a usage chart with holes punched out
    of it would misrepresent an idle stretch as a shorter timeline.
    """
    days = max(1, days)
    end = (
        datetime.fromisoformat(today).date()
        if today
        else datetime.now(timezone.utc).date()
    )
    dates = [(end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    buckets = {date: _empty_day(date) for date in dates}

    for row in rows:
        started = _get(row, "started_at") or ""
        bucket = buckets.get(started[:10])
        if bucket is None:  # outside the window
            continue
        bucket["runs"] += 1
        status = _get(row, "status")
        if status == "ok":
            bucket["ok"] += 1
        elif status == "cancelled":
            # A run Wes stopped on purpose is neither a success nor a failure;
            # counting it as failed would make the success rate punish him for
            # using the cancel button.
            bucket["cancelled"] += 1
        elif status != "running":
            bucket["failed"] += 1
        bucket["cost"] += float(_get(row, "cost_usd") or 0.0)
        bucket["turns"] += int(_get(row, "num_turns") or 0)
        seconds = run_duration(row)
        if seconds is not None:
            bucket["duration_sec"] += seconds
            bucket["finished"] += 1

    return [buckets[date] for date in dates]


def summarize(buckets: Sequence[dict]) -> dict:
    runs = sum(b["runs"] for b in buckets)
    finished = sum(b["finished"] for b in buckets)
    duration = sum(b["duration_sec"] for b in buckets)
    ok = sum(b["ok"] for b in buckets)
    failed = sum(b["failed"] for b in buckets)
    # Graded = runs that actually reached a verdict. Runs still in flight and
    # runs canceled by hand have no verdict to average in.
    graded = ok + failed
    return {
        "runs": runs,
        "ok": ok,
        "failed": failed,
        "cancelled": sum(b["cancelled"] for b in buckets),
        "cost": round(sum(b["cost"] for b in buckets), 4),
        "turns": sum(b["turns"] for b in buckets),
        "duration_sec": duration,
        # Averaged over *finished* runs only; a run still in flight has no
        # duration yet and would drag the mean down toward zero.
        "avg_duration_sec": int(duration / finished) if finished else 0,
        "avg_cost": round(sum(b["cost"] for b in buckets) / runs, 4) if runs else 0.0,
        "success_rate": round(100.0 * ok / graded, 1) if graded else 0.0,
        "busiest": max(buckets, key=lambda b: b["runs"])["date"] if buckets else "",
    }


# --------------------------------------------------------------------------
# Where a run's tokens actually go
# --------------------------------------------------------------------------
# Wes, 2026-08-07: "I seem to blow through so much more usage than I used to.
# Is it all the bloat from system prompt, memory, chat history, etc?"
#
# Answering that took an afternoon of hand-written SQL against the runs table,
# which is the wrong way to answer a question that will be asked again. This is
# that answer, made standing.
#
# The finding, from the transcript of run 924 (153 turns, 26.2M cache reads):
# a run re-reads its whole context on every single turn, so cost is not the
# number of tokens in a prompt, it is that number multiplied by the turns after
# it. Of those 26.2M reads, 19% was the portal's own prompt, 30% was Claude
# Code's system prompt plus tool schemas, and 51% was the conversation the run
# generated itself. Only the first slice is the portal's to cut, and it is the
# smallest of the three.

# The portal's prompt is plain markdown English. Measured across the four active
# projects on 2026-08-07, an 88 KB prompt was ~22k tokens: 4.0 bytes per token.
# Used only to express one slice as a percentage of another, so a few percent of
# drift here does not change what the panel says.
BYTES_PER_TOKEN = 4.0

# What every run carries before a single byte of the portal's prompt: Claude
# Code's own system prompt plus the JSON schemas of every tool it can call.
# Measured from run 924's transcript on 2026-08-07 - its context opened at
# 56,793 tokens against a 90 KB (~22.5k token) prompt, leaving ~34.3k that the
# portal neither writes nor can shrink. Named here so the panel can say plainly
# which slice is nobody's to cut, rather than quietly folding it into the
# portal's own share and overstating what a prompt diet would buy.
CLI_HEAD_TOKENS = 34_300


def anatomy(rows: Iterable[Any]) -> dict:
    """Split what runs re-read into the prompt, the CLI's head, and the run itself.

    Three slices, because they have three different owners:

    * **prompt** - the portal writes this, and `promptbudget` already caps its
      four unbounded blocks. It is the only slice a portal change can move.
    * **cli** - Claude Code's system prompt and tool schemas. Fixed per turn and
      not ours; shown so it is visible rather than blamed on the prompt.
    * **run** - everything the run itself said and read back. Governed by how
      long the run goes on, which is a question about what runs are asked to do,
      not about prompt size.

    Every slice is `tokens x turns`, because that is how a re-read context bills.
    A run with no recorded turns or no recorded reads contributes nothing rather
    than dividing by zero - the token columns landed on 2026-07-28 and every run
    before that has NULL in them, exactly as `prompt_sizes` documents.
    """
    prompt = cli = reads = 0
    counted = 0
    for row in rows:
        turns = _get(row, "num_turns") or 0
        read = _get(row, "cache_read_tokens") or 0
        pbytes = _get(row, "prompt_bytes") or 0
        if not turns or not read or not pbytes:
            continue
        counted += 1
        reads += read
        prompt += int(pbytes / BYTES_PER_TOKEN) * turns
        cli += CLI_HEAD_TOKENS * turns

    if not counted or not reads:
        return {"runs": 0, "prompt_pct": 0.0, "cli_pct": 0.0, "run_pct": 0.0, "reads": 0}

    # The head slices are modeled (bytes/4, a measured constant) while `reads` is
    # recorded, so a pathological run could model more head than it actually
    # read. Clamp rather than emit a negative share: the honest reading of that
    # case is "essentially all of it was head", not a nonsense number.
    prompt_pct = min(100.0, 100.0 * prompt / reads)
    cli_pct = min(100.0 - prompt_pct, 100.0 * cli / reads)
    run_pct = 100.0 - prompt_pct - cli_pct
    # Bars are built here rather than in the template, the way `by_project`
    # already does it: `share_bar` is a function, not a registered Jinja filter.
    return {
        "runs": counted,
        "reads": reads,
        "prompt_pct": round(prompt_pct, 1),
        "cli_pct": round(cli_pct, 1),
        "run_pct": round(run_pct, 1),
        "prompt_bar": share_bar(prompt_pct / 100.0),
        "cli_bar": share_bar(cli_pct / 100.0),
        "run_bar": share_bar(run_pct / 100.0),
    }


def turn_trend(buckets: Sequence[dict]) -> dict:
    """Turns per run and weight per turn, oldest half against newest half.

    The two numbers that actually moved. Between 2026-07-21 and 2026-08-04 the
    portal's builds went from 56 turns a run to 172, while the weight of a single
    turn roughly doubled - and because a longer run also carries a fatter context
    on every one of those extra turns, the two multiply rather than add. That is
    the whole of why a run went from ~4 to ~25, and neither half of it is prompt
    size.

    Split by whole days rather than by run, so a day with one expensive run does
    not outvote a day with forty cheap ones. Days with no finished runs are
    skipped on both sides; with fewer than two such days there is no trend to
    report and `runs` comes back 0.
    """
    live = [b for b in buckets if b.get("runs") and b.get("turns")]
    if len(live) < 2:
        return {"runs": 0}

    half = len(live) // 2

    def stat(days: Sequence[dict]) -> dict:
        runs = sum(d["runs"] for d in days)
        turns = sum(d["turns"] for d in days)
        cost = sum(d["cost"] for d in days)
        return {
            "turns_per_run": round(turns / runs, 1) if runs else 0.0,
            "cost_per_turn": round(cost / turns, 4) if turns else 0.0,
            "cost_per_run": round(cost / runs, 2) if runs else 0.0,
        }

    older, newer = stat(live[:half]), stat(live[half:])
    return {
        "runs": sum(d["runs"] for d in live),
        "older": older,
        "newer": newer,
        # Signed so the template can say "up" or "down" without recomputing it,
        # and 0 when there is nothing to divide by rather than a false "flat".
        "turns_change_pct": _change(older["turns_per_run"], newer["turns_per_run"]),
        "per_turn_change_pct": _change(older["cost_per_turn"], newer["cost_per_turn"]),
        "per_run_change_pct": _change(older["cost_per_run"], newer["cost_per_run"]),
    }


def _change(old: float, new: float) -> int:
    """Percent change old -> new, 0 when there is no baseline to change from."""
    if not old:
        return 0
    return int(round(100.0 * (new - old) / old))


def prompt_sizes(rows: Iterable[Any], names: dict[int, dict]) -> dict:
    """How big the prompts have been, and which project builds the biggest one.

    Every run's prompt is rebuilt from scratch, and a run re-reads it once per
    turn - so a kilobyte here is not a kilobyte, it is a kilobyte times however
    many turns the run takes. That is what makes prompt size worth a panel
    rather than a column: it is the one number on the page that multiplies.

    Only runs that recorded a size count. The column landed on 2026-07-28, so
    every run before that has NULL in it, and averaging those in as zero would
    show the prompt shrinking on exactly the day the portal started measuring
    it. An empty rollup returns `runs: 0` and the page draws nothing.
    """
    sized = [r for r in rows if _get(r, "prompt_bytes")]
    if not sized:
        return {"runs": 0, "avg_kb": 0.0, "max_kb": 0.0, "biggest": None, "by_project": []}

    per_project: dict[Any, list[int]] = {}
    for row in sized:
        per_project.setdefault(_get(row, "project_id"), []).append(int(_get(row, "prompt_bytes")))

    table = []
    for pid, sizes in per_project.items():
        info = names.get(pid) or {}
        table.append({
            "project_id": pid,
            "title": info.get("title") or "(not a project)",
            "slug": info.get("slug") or "",
            "runs": len(sizes),
            "avg_kb": round(sum(sizes) / len(sizes) / 1024, 1),
            "max_kb": round(max(sizes) / 1024, 1),
        })
    table.sort(key=lambda d: -d["avg_kb"])

    total = sum(int(_get(r, "prompt_bytes")) for r in sized)
    return {
        "runs": len(sized),
        "avg_kb": round(total / len(sized) / 1024, 1),
        "max_kb": round(max(int(_get(r, "prompt_bytes")) for r in sized) / 1024, 1),
        "biggest": table[0] if table else None,
        "by_project": table,
    }


def share_bar(fraction: float, width: int = 16) -> str:
    """A proportional ASCII bar, `fraction` in 0..1. Any non-zero share gets at
    least one block so a cheap project doesn't render as an empty row."""
    width = max(1, width)
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    if fraction > 0:
        filled = max(1, filled)
    return "█" * filled + "·" * (width - filled)


def by_project(rows: Iterable[Any], names: dict[int, dict]) -> list[dict]:
    """Which projects are eating the budget, most expensive first.

    Cost is the ranking key rather than run count: two short runs and one long
    one are not the same draw on the day's usage. Runs with no project (the
    daily reflect) are grouped under a single synthetic entry rather than
    dropped, so the shares still add up to the window total.
    """
    groups: dict[Optional[int], dict] = {}
    for row in rows:
        pid = _get(row, "project_id")
        group = groups.get(pid)
        if group is None:
            meta = names.get(pid) if pid is not None else None
            group = groups[pid] = {
                "project_id": pid,
                "title": (meta or {}).get("title") or ("memory / reflect" if pid is None else f"project #{pid}"),
                "slug": (meta or {}).get("slug") or "",
                "runs": 0, "ok": 0, "failed": 0, "cancelled": 0,
                "cost": 0.0, "turns": 0, "duration_sec": 0, "finished": 0,
            }
        group["runs"] += 1
        status = _get(row, "status")
        if status == "ok":
            group["ok"] += 1
        elif status == "cancelled":
            group["cancelled"] += 1
        elif status != "running":
            group["failed"] += 1
        group["cost"] += float(_get(row, "cost_usd") or 0.0)
        group["turns"] += int(_get(row, "num_turns") or 0)
        seconds = run_duration(row)
        if seconds is not None:
            group["duration_sec"] += seconds
            group["finished"] += 1

    total_cost = sum(g["cost"] for g in groups.values())
    total_runs = sum(g["runs"] for g in groups.values())
    out = []
    for group in groups.values():
        # Fall back to run share when nothing has a recorded cost, so the bars
        # still say something on a window of runs the CLI didn't price.
        share = (group["cost"] / total_cost) if total_cost else (group["runs"] / total_runs if total_runs else 0.0)
        graded = group["ok"] + group["failed"]
        out.append({
            **group,
            "cost": round(group["cost"], 4),
            "share": round(100.0 * share, 1),
            "bar": share_bar(share),
            "success_rate": round(100.0 * group["ok"] / graded, 1) if graded else 0.0,
            "avg_duration_sec": int(group["duration_sec"] / group["finished"]) if group["finished"] else 0,
        })
    out.sort(key=lambda g: (-g["cost"], -g["runs"], g["title"]))
    return out


def history(
    days: int = 14,
    project_id: Optional[int] = None,
    today: Optional[str] = None,
    only_projects: Optional[set[int]] = None,
) -> dict:
    """Spend and run history. `only_projects` scopes the per-project breakdown.

    That breakdown names every project it counts, which makes this an easy leak
    to miss: /activity looks like a page about runs, and the by_project table
    under the chart is the part that spells out titles. The day buckets and
    totals are narrowed with it, so the numbers on the page describe the same
    set of projects the table below them lists.
    """
    """The full time-series payload shared by `/activity` and `/api/usage/history`."""
    days = max(1, min(days, 365))
    end = (
        datetime.fromisoformat(today).date() if today else datetime.now(timezone.utc).date()
    )
    since = (end - timedelta(days=days - 1)).isoformat()
    rows = db.runs_since(since, project_id=project_id)
    if only_projects is not None:
        rows = [r for r in rows if r["project_id"] in only_projects]
    buckets = bucket_by_day(rows, days, today=today)
    names = {
        p["id"]: {"title": p["title"], "slug": p["slug"]}
        for p in db.list_projects()
        if only_projects is None or p["id"] in only_projects
    }
    return {
        "days": days,
        "buckets": buckets,
        "totals": summarize(buckets),
        "by_project": by_project(rows, names),
        "prompts": prompt_sizes(rows, names),
        "anatomy": anatomy(rows),
        "trend": turn_trend(buckets),
        "runs_spark": sparkline([b["runs"] for b in buckets]),
        "cost_spark": sparkline([b["cost"] for b in buckets]),
    }


# ---------------------------------------------------------------------------
# The GitHub-style activity grid
# ---------------------------------------------------------------------------

# Thirteen weeks is a quarter of a year, which is long enough to show a rhythm
# and short enough to stay a strip rather than a chart. Wes asked for "small and
# unobtrusive"; the width is the constraint that keeps it that way.
HEATMAP_WEEKS = 13

# Run counts, not costs: the question the grid answers is "was anything working
# on this", and a cheap run is still a day something happened.
HEATMAP_THRESHOLDS = (1, 3, 6)


def heatmap_level(runs: int) -> int:
    """0 for an idle day, then 1-4 by how busy it was."""
    if runs <= 0:
        return 0
    return 1 + sum(1 for t in HEATMAP_THRESHOLDS if runs > t)


def heatmap(
    weeks: int = HEATMAP_WEEKS, project_id: Optional[int] = None, today: Optional[str] = None
) -> dict:
    """A week-per-column activity grid, oldest week first.

    Columns are calendar weeks starting Sunday, so every row is one weekday the
    way GitHub's grid is. The window starts on a Sunday for that reason; the
    tail of the last column - the days after today - is `None` rather than a
    zero, so an idle day and a day that hasn't happened yet look different.
    """
    weeks = max(1, min(weeks, 53))
    end = (
        datetime.fromisoformat(today).date() if today else datetime.now(timezone.utc).date()
    )
    # Sunday-based column index: Python's weekday() is Monday=0.
    lead = (end.weekday() + 1) % 7
    start = end - timedelta(days=lead + 7 * (weeks - 1))
    days = (end - start).days + 1

    rows = db.runs_since(start.isoformat(), project_id=project_id)
    buckets = {b["date"]: b for b in bucket_by_day(rows, days, today=end.isoformat())}

    columns: list[list[Optional[dict]]] = []
    for w in range(weeks):
        column: list[Optional[dict]] = []
        for d in range(7):
            date = start + timedelta(days=w * 7 + d)
            if date > end:
                column.append(None)
                continue
            bucket = buckets.get(date.isoformat())
            runs = bucket["runs"] if bucket else 0
            column.append({"date": date.isoformat(), "runs": runs, "level": heatmap_level(runs)})
        columns.append(column)

    total = sum(cell["runs"] for column in columns for cell in column if cell)
    return {"weeks": columns, "total": total, "days": days}


def humanize_seconds(secs: Optional[int]) -> str:
    if secs is None:
        return "-"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    hours, minutes = divmod(secs // 60, 60)
    return f"{hours}h {minutes:02d}m"
