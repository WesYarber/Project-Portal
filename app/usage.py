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
