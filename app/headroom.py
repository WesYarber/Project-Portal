"""Don't start a run that will not fit in what is left of the session window.

The failure this exists for, seven times over on 2026-08-06 and 2026-08-07:

    841  commander-case                    20:18  311 events  session limit
    842  site-wide-tools                   20:26  137 events  session limit
    843  karli-s-social-media-landing-page 20:26   86 events  session limit
    844  proxytable                        20:28   13 events  session limit
    870  karli-s-social-media-landing-page 01:15  138 events  session limit
    871  site-wide-tools                   01:22    4 events  session limit
    872  commander-case                    01:35    4 events  session limit

Every one of those runs booted, reached the model, did real work, billed for
it, and was then killed by the CLI with "You've hit your session limit". Every
one of them started within 35 minutes of that five-hour window's reset. Three
of them left an edited workspace behind and a red row on the runs page, which
is the outcome `pacing.DEFAULT_HOLD_PERCENT` is written to prevent - its own
comment says "a run started at 97% has a good chance of dying partway through,
and a half-finished run is worse than no run".

**Why the fixed threshold could not prevent it.** 90% is a statement about the
meter. Whether a run survives is a statement about the *gap between the meter
and the wall* versus what the run is about to spend. Those are only the same
question if you know what a run costs, and the portal did not know: it read the
meter constantly and had never once written down what one of its own runs did
to it. So the guard was a guess with no way to be checked, and the evidence
above says the guess was too small.

**So measure it.** Every run stamps the five-hour meter as it starts and again
as it ends (`runs.session_percent_start` / `_end`). The difference is how far
the meter moved over one run's lifetime, and the reserve the guard holds back
is a high percentile of the recent ones.

Three things about that measurement worth stating plainly, because each one
looks like a bug otherwise:

* **It is not one run's isolated cost, and should not be.** The portal runs up
  to six agents at once, so the delta across one run's life includes whatever
  its neighbors spent in the same minutes. That is the correct quantity: the
  guard is answering "how far will the meter move before this run finishes",
  under the concurrency the portal actually uses.
* **A pair that straddles a reset is dropped, not clamped to zero.** The window
  resets every five hours, so a run alive across one ends lower than it started.
  That is missing data, not a free run.
* **A non-positive delta is dropped for the same reason.** A run that died at
  the starting line, or one whose two stamps read the same cached snapshot,
  says nothing about what a working run spends. Dropping them biases the
  reserve upward, which is the direction to be wrong in: over-reserving delays
  a run until a reset that is at most five hours away, under-reserving destroys
  work that has already been done and paid for.

The same asymmetry is why the statistic is the 80th percentile and not the
median. A median reserve is too small half the time by construction.

**It can never idle the portal.** Three separate reasons, and all three are
load-bearing: the reserve is capped at `MAX_RESERVE`, so a wild measurement
cannot push the hold down to nothing; there is no hold at all when the usage
reading is missing or stale, because `pacing.scheduled_hold` returns None
before it ever asks for a reserve; and a manual run ignores holds entirely, so
Wes pressing the button always runs.

Scoped to the five-hour window on purpose. The delta measured here is in
five-hour-window points, and a weekly window's points are not the same unit -
90% of a week still leaves many hours of runs, which is why none of the seven
deaths were weekly. Applying a number measured in one unit to a window
denominated in another would be a guess wearing a measurement's clothes.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from app import db, limits

log = logging.getLogger("portal.headroom")

# The key of the five-hour window in a `limits.parse()` snapshot.
SESSION_KEY = "five_hour"

# The reserve used until enough runs have been measured.
#
# No longer provisional. It started at 12.0 - a floor argued from the seven
# killed runs, which all began under the 90% hold and so had at least 10 points
# of headroom and died anyway. That was the smallest number the evidence then
# supported, and the evidence has since arrived: 9 measured runs on this
# install cost [11, 12, 17, 17, 17, 19, 19, 26, 35] points of the five-hour
# window, whose p80 - the same statistic `measured_reserve` computes - is 21.8.
# So the old default was under half of what a run actually needs, and a fresh
# install spent its first five runs reserving too little to survive.
#
# Rounded UP from 21.8, because the two directions are not symmetric: too high
# holds a run back for a few minutes, too low kills one part-way through and
# throws away everything it had done. Still well under MAX_RESERVE, so a
# measurement is free to move it either way once one exists.
DEFAULT_RESERVE = 22.0

# Below this many usable pairs the percentile is noise, so the default stands.
MIN_SAMPLES = 5

# How many recent runs to measure over. The account's limits have been doubled
# and reverted inside a fortnight and the portal's own prompt sizes move, so a
# long history describes a portal that no longer exists.
SAMPLE_LIMIT = 40

# Where in the sorted costs to sit. See the module docstring: the loss is
# asymmetric, so the statistic is too.
PERCENTILE = 0.8

# The ceiling on the reserve, whatever the measurement says. A run that somehow
# measured 60 points would hold every scheduled run below 40% of the session
# window forever, which is a broken portal rather than a careful one.
MAX_RESERVE = 35.0

# An explicit override, in points. Blank (the default) means "measure it".
RESERVE_SETTING = "session_headroom_reserve"


def session_percent(snapshot: Optional[dict] = None) -> Optional[float]:
    """The five-hour window's utilization right now, or None if unknown.

    None covers every shape of not-knowing - no snapshot, a failed fetch, a
    reading old enough that the window may have reset under it, an API-key
    install with no subscription window at all - because all of them have to
    reach the caller as "do not write this down", and a plausible-looking 0.0
    would be indistinguishable from a genuinely empty window.
    """
    snapshot = limits.cached() if snapshot is None else snapshot
    if not isinstance(snapshot, dict):
        return None
    if not snapshot.get("ok") or snapshot.get("stale"):
        return None
    for entry in snapshot.get("windows") or []:
        if entry.get("key") != SESSION_KEY:
            continue
        try:
            return max(0.0, min(100.0, float(entry.get("percent") or 0.0)))
        except (TypeError, ValueError):
            return None
    return None


def _stamp(run_id: Optional[int], which: str) -> None:
    """Write one end of the pair, and never let doing so break a run.

    This is telemetry hanging off the side of a live agent run. A missing
    snapshot, a locked database or a column that a not-yet-migrated install
    does not have must all cost a log line, not the run.
    """
    if not run_id:
        return
    try:
        percent = session_percent()
        if percent is None:
            return
        db.record_run_session_meter(run_id, **{which: percent})
    except Exception:  # pragma: no cover - defensive
        log.debug("could not stamp the session meter for run %s", run_id, exc_info=True)


def stamp_start(run_id: Optional[int]) -> None:
    """Record the meter as this run begins."""
    _stamp(run_id, "start")


def stamp_end(run_id: Optional[int]) -> None:
    """Record the meter as this run ends."""
    _stamp(run_id, "end")


def observed_costs(pairs: Sequence[tuple[float, float]]) -> list[float]:
    """How far the meter moved over each run, dropping the unusable pairs.

    Pure, so the rules in the module docstring are testable without a database:
    a pair whose end is at or below its start is a window that reset mid-run or
    a run that never reached the model, and either way it is missing data
    rather than a run that cost nothing.
    """
    out = []
    for start, end in pairs:
        delta = end - start
        if delta > 0:
            out.append(delta)
    return out


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """The `q` quantile of `values`, interpolating between the two neighbors.

    Written out rather than pulled from statistics.quantiles because that
    function needs at least two data points and raises on one, and this is
    called with whatever history happens to exist.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def measured_reserve(limit: int = SAMPLE_LIMIT) -> Optional[float]:
    """What the portal's own recent runs say a run needs, or None if unmeasured.

    None rather than a fallback so a caller can tell "not enough evidence yet"
    from "measured, and it came out at the default" - which is the difference
    between a number worth showing Wes and one that is still a guess.

    Deliberately unclamped: this is the measurement, and a measurement that
    quietly came out at exactly `MAX_RESERVE` would hide the fact that
    something is wrong with it. `reserve()` is the one place the cap applies,
    because that is the value that actually gates runs.
    """
    try:
        pairs = db.recent_session_meter_pairs(limit)
    except Exception:  # pragma: no cover - defensive
        log.debug("could not read the session meter history", exc_info=True)
        return None
    costs = observed_costs(pairs)
    if len(costs) < MIN_SAMPLES:
        return None
    return percentile(costs, PERCENTILE)


def configured_reserve() -> Optional[float]:
    """An explicit reserve set in Settings, or None to measure it.

    Out-of-range and unparseable both read as None. A typo in a settings field
    must not be able to hold every scheduled run.
    """
    raw = (db.get_setting(RESERVE_SETTING) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 0.0 <= value <= MAX_RESERVE else None


def reserve() -> float:
    """Points of the five-hour window to keep free for the run about to start.

    Settings first, then the measurement, then the provisional default - and
    clamped, so no path through this returns a number that could idle the
    portal.
    """
    for candidate in (configured_reserve(), measured_reserve()):
        if candidate is not None:
            return max(0.0, min(MAX_RESERVE, candidate))
    return DEFAULT_RESERVE


def sample_size(limit: int = SAMPLE_LIMIT) -> int:
    """How many usable measurements the reserve is standing on, for the UI."""
    try:
        return len(observed_costs(db.recent_session_meter_pairs(limit)))
    except Exception:  # pragma: no cover - defensive
        return 0
