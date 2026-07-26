"""Scheduling decisions made against Wes's real Claude limits.

`app/limits.py` reads the account; this module is what the worker *does* with
the reading. Two behaviours, and they pull in opposite directions on purpose:

* **Hold.** When a window is nearly full, a scheduled run is very likely to die
  on a usage limit halfway through - which costs the portal a run row, a
  workspace left mid-edit, and an hour of backoff to discover something the
  account would have told it for free. So scheduled runs stop just short of the
  wall. Manual runs never do: Wes pressing *run now* is a decision, not a
  guess, and it is his allowance to spend.

* **Spend down.** The weekly window does not roll over. Whatever is unused at
  the reset is simply gone, and on a Max plan that is routinely most of it. So
  when the weekly window is a few hours from resetting with real headroom left,
  the portal asks - once per window - whether to spend the remainder, and if
  Wes says yes it lifts *its own* invented budget (N runs a portal-day, one run
  per pacing interval) until the reset. It never lifts the real limit; the hold
  above still applies, so a spend-down ends by running into the wall it was
  aiming at, not through it.

The answer is read back by polling the question row rather than by hooking the
answer path, because there are three ways to answer one (the project page, the
questions page and Telegram) and a hook would have to be installed in all of
them. A minute of latency on an eight-hour window costs nothing.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import cadence, config, db, limits, quickreplies, spawnauth

log = logging.getLogger("portal.pacing")

# Percentage of a window at which scheduled runs stop. Not 100: a run started
# at 97% has a good chance of dying partway through, and a half-finished run is
# worse than no run - it leaves an edited workspace and an error in the journal.
DEFAULT_HOLD_PERCENT = 90.0

# How close to the weekly reset the spend-down offer goes out. Long enough that
# several runs fit inside the remainder, short enough that saying yes is a
# decision about tonight rather than about the rest of the week.
SPEND_DOWN_LEAD_HOURS = 8
# How much of the weekly window has to be unused before it is worth asking. A
# window at 88% has nothing to spend and the question would be noise.
SPEND_DOWN_MIN_UNUSED = 25.0
# Pacing while spending down. The daily run budget is lifted entirely; the
# interval is shortened rather than removed so runs still start in a rhythm the
# parallel cap can absorb.
SPEND_DOWN_INTERVAL_MIN = 2
# Parallel runs allowed while spending down. Higher than the everyday cap
# because the point is to use an allowance before it evaporates, but still a
# cap: every in-flight run is a `claude -p` against one subscription, and past
# a handful they slow each other down rather than getting more done. Only ever
# raises the configured value, never lowers it.
SPEND_DOWN_PARALLEL = 4

# The endpoint jitters a window's resets_at by a second or so between polls
# (21:00:00 one fetch, 20:59:59 the next). Anything within this many seconds
# is the same window - without the tolerance, every flip of the jitter looked
# like a brand-new week and re-asked the spend-down question, which is how Wes
# got the same offer twelve times in one afternoon.
WINDOW_MATCH_TOLERANCE_SEC = 600

# While the portal is deliberately spending - a spend-down window or the
# ahead-of-pace boost below - scheduled runs hold the *session* window at this
# percentage instead of the ordinary hold. This is what spreads a burst across
# five-hour windows rather than slamming each one to the wall: the burst
# pauses, the session resets, the burst resumes. 70 because that is the number
# Wes gave ("don't go over my 5hr limit by past 70%").
SESSION_GUARD_SETTING = "spend_down_session_hold"
DEFAULT_SESSION_GUARD = 70.0

# The ahead-of-pace boost. If 60% of the week has passed and only 20% of the
# weekly window is spent, 40 points of allowance are on course to evaporate at
# the reset; rather than waiting for the last-8-hours offer, the portal
# quickens its own pacing a little, automatically, in proportion to how far
# ahead of pace it is. No question is asked - this only spends allowance that
# is otherwise being wasted, the ordinary daily budget still applies (scaled,
# not lifted), and the session guard above keeps it spread out.
WEEK_SEC = 7 * 86400
BOOST_START_SURPLUS = 12.0   # points ahead of pace before any boost at all
BOOST_POINTS_PER_STEP = 15.0  # each further N points add 1x to the factor
BOOST_MAX = 4.0

# Front-loading the spend curve. "Ahead of pace" needs a definition of pace,
# and a *linear* one (spend 1/7th of the window per day) is the wrong hedge for
# this account: the research burst caught the weekly meter resetting every ~72h
# while resets_at still claimed seven days, and the limit itself getting
# silently doubled and then reverted over a holiday. A window that might vanish
# a day early - or shrink - is one you want to have spent *ahead* of the clock,
# not banked for a Sunday that may never come. So the pace target is a concave
# curve, `100 * elapsed_fraction ** gamma` with gamma < 1: it expects more spent
# early and flattens out near the reset (by which point the window is nearly
# full anyway). gamma = 1 is the old linear behaviour, so this setting turns
# front-loading off cleanly; nothing below asks for a value outside (0, 1].
FRONT_LOAD_SETTING = "spend_front_load"
DEFAULT_FRONT_LOAD = 0.75

# The don't-saturate compliance guard. The Legal & Compliance page assumes
# "ordinary, individual usage", and 24/7 background running is the named reason
# the weekly limits exist - the enforcement reports of 2026 were all harness
# spoofing and credential-sharing, but a self-hosted scheduler that keeps every
# five-hour window pinned to the wall around the clock is the shape that *looks*
# like a bot. So the portal deliberately leaves idle gaps: if it has been
# running for more than `saturation_max_duty` percent of the recent wall clock,
# scheduled runs hold until enough idle time has accrued to drop back under.
# This is a floor on ordinary operation and the automatic ahead-of-pace boost;
# an explicit spend-down (which Wes said yes to, and which is bounded to the few
# hours before a weekly reset) is exempt - that is a decision to burn the
# allowance, not a machine quietly grinding all week. Manual runs never hold.
SATURATION_SETTING = "saturation_max_duty"
DEFAULT_SATURATION = 85.0
# Measured over a trailing five-hour span - one session window, the unit the
# compliance note names. An 85% ceiling leaves ~45 minutes idle in every such
# span. Ordinary paced operation (runs with a pacing gap between them) sits well
# under this; only genuinely back-to-back saturation trips it.
SATURATION_WINDOW_HOURS = 5.0

HOLD_SETTING = "limit_hold_percent"
OFFERED_FOR = "spend_down_offered_for"      # resets_at of the window already offered
OFFER_QUESTION = "spend_down_question_id"   # the open question waiting on an answer
ACTIVE_UNTIL = "spend_down_until"           # iso; set once Wes says yes

# The model a spend-down routes cheaper-pinned runs up to. A spend-down burns
# an all-model weekly window that is about to reset and otherwise be lost, and
# a window spent on Opus buys more than the same window spent on Haiku. So
# while a spend-down is running, a run configured for a cheaper model is routed
# up to this one. Two deliberate limits: it only ever *raises* a run's model,
# never lowers it; and it only happens during an explicit spend-down (the one
# Wes said yes to), never during the automatic ahead-of-pace boost - the boost
# just runs *more often*, and silently changing a project's model is a bigger
# decision than changing its cadence, so it waits for the yes.
SPEND_MODEL_SETTING = "spend_down_model"
DEFAULT_SPEND_MODEL = "opus"

# Capability/spend rank: roughly how much a run costs against the weekly window
# and how capable it is. Only the ordering matters - a run is routed up only to
# a strictly higher rank, so an unknown model (rank 99) is never touched.
MODEL_SPEND_RANK = {"haiku": 1, "fable": 2, "sonnet": 2, "opus": 3}


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Holding scheduled runs short of the wall
# ---------------------------------------------------------------------------


def hold_percent() -> float:
    try:
        value = float(db.get_setting(HOLD_SETTING) or DEFAULT_HOLD_PERCENT)
    except ValueError:
        return DEFAULT_HOLD_PERCENT
    # 0 or anything silly means "never hold"; treat it as off rather than as a
    # setting that stops the portal dead.
    return value if 1.0 <= value <= 100.0 else DEFAULT_HOLD_PERCENT


def session_guard() -> float:
    try:
        value = float(db.get_setting(SESSION_GUARD_SETTING) or DEFAULT_SESSION_GUARD)
    except ValueError:
        return DEFAULT_SESSION_GUARD
    return value if 1.0 <= value <= 100.0 else DEFAULT_SESSION_GUARD


def scheduled_hold(snapshot: Optional[dict] = None, threshold: Optional[float] = None) -> Optional[dict]:
    """The window that should stop a scheduled run right now, or None.

    Returns None whenever the reading is missing, failed or stale: an outage at
    Anthropic's end must not be able to idle the portal. Being wrong here costs
    one run that hits a limit and backs off, which is exactly the old
    behaviour - so the missing-data case degrades to what happened before.

    While the portal is deliberately spending (a spend-down window or the
    ahead-of-pace boost), the *session* window is held at the tighter guard
    instead - that pause is what spreads a burst across five-hour windows.
    """
    snapshot = limits.cached() if snapshot is None else snapshot
    if not snapshot.get("ok") or snapshot.get("stale"):
        return None
    threshold = hold_percent() if threshold is None else threshold
    guard = session_guard() if spend_mode(snapshot) else None
    full = []
    for entry in snapshot.get("windows") or []:
        limit = threshold
        if guard is not None and entry.get("key") == "five_hour" and guard < limit:
            limit = guard
        percent = entry.get("percent", 0.0)
        if percent >= limit:
            # "guarded" only when the guard is what is holding it - a window
            # past the ordinary threshold is a plain hold whatever mode is on.
            full.append(dict(entry, hold_at=limit, guarded=percent < threshold))
    if not full:
        return None
    # The fullest one, because that is the one a run would actually hit first.
    return max(full, key=lambda w: w["percent"])


def hold_reason(hold: dict) -> str:
    resets = hold.get("resets_in") or ""
    tail = f" and resets in {resets}" if resets else ""
    if hold.get("guarded"):
        return (
            f"pausing the burst - your {hold['label']} window is at "
            f"{hold['percent']:g}%{tail}, past the {hold['hold_at']:g}% guard that "
            f"spreads spending across sessions (run now still goes)"
        )
    return (
        f"holding scheduled runs - your {hold['label']} window is at "
        f"{hold['percent']:g}%{tail} (run now to spend it anyway)"
    )


# ---------------------------------------------------------------------------
# The don't-saturate compliance guard: leave idle gaps, don't run 24/7
# ---------------------------------------------------------------------------


def saturation_max_duty() -> Optional[float]:
    """The percent of recent wall clock the portal may run before it holds.

    Returns None when the guard is off: 0 or 100 both disable it (nothing to
    leave idle, or everything allowed), as does anything unparseable falling
    back to a default that is then in range. A configured value in (0, 100) is
    the live ceiling."""
    raw = db.get_setting(SATURATION_SETTING)
    try:
        value = DEFAULT_SATURATION if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SATURATION
    if value <= 0.0 or value >= 100.0:
        return None
    return value


def duty_cycle(intervals, window_start: datetime, now: datetime) -> float:
    """Fraction (0..1) of [window_start, now] the portal had a run in flight.

    `intervals` is a list of (start, end) datetimes; an end of None means the
    run is still going, so it counts as busy up to `now`. Overlapping runs
    (parallel slots) are merged, not summed - this measures wall-clock
    occupancy, how *continuously* the portal ran, not run-hours. Anything
    outside the window is clipped, so a run that began before it counts only for
    the part inside."""
    total = (now - window_start).total_seconds()
    if total <= 0:
        return 0.0
    clipped = []
    for start, end in intervals:
        if start is None:
            continue
        end = now if end is None else end
        start = max(start, window_start)
        end = min(end, now)
        if end > start:
            clipped.append((start, end))
    if not clipped:
        return 0.0
    clipped.sort(key=lambda pair: pair[0])
    busy = 0.0
    cur_start, cur_end = clipped[0]
    for start, end in clipped[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            busy += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
    busy += (cur_end - cur_start).total_seconds()
    return min(1.0, busy / total)


def saturation_hold(now: Optional[datetime] = None) -> Optional[dict]:
    """The saturation guard that should stop a scheduled run right now, or None.

    None whenever the guard is off, a spend-down is running (exempt), the recent
    duty cycle is under the ceiling, or anything goes wrong reading the runs -
    the guard fails *open*, because a bug in it must never be able to idle the
    portal, only ever to insert a gap it can explain."""
    now = now or datetime.now(timezone.utc)
    ceiling = saturation_max_duty()
    if ceiling is None or spending_down(now):
        return None
    try:
        window_start = now - timedelta(hours=SATURATION_WINDOW_HOURS)
        rows = db.runs_active_since(window_start.isoformat(timespec="seconds"))
        intervals = [
            (_parse_iso(row["started_at"]), _parse_iso(row["ended_at"]) if row["ended_at"] else None)
            for row in rows
        ]
        duty = duty_cycle(intervals, window_start, now) * 100.0
    except Exception:  # noqa: BLE001 - fail open, never idle the portal on a bug
        log.exception("saturation guard failed to read runs")
        return None
    if duty < ceiling:
        return None
    return {"duty": round(duty, 1), "ceiling": ceiling, "window_hours": SATURATION_WINDOW_HOURS}


def saturation_reason(hold: dict) -> str:
    return (
        f"leaving an idle gap - the portal has run {hold['duty']:g}% of the last "
        f"{hold['window_hours']:g}h, past the {hold['ceiling']:g}% saturation guard that "
        f"keeps steady use from looking like a 24/7 bot (run now still goes)"
    )


# ---------------------------------------------------------------------------
# Spending ahead of pace, before the last-minute offer
# ---------------------------------------------------------------------------


def front_load() -> float:
    """The gamma of the front-loaded pace curve, in (0, 1].

    < 1 front-loads (expects more spent early); 1 is plain linear pacing.
    Anything unparseable or outside the range falls back to the default rather
    than inverting the curve into a back-load, which would bank spending for the
    end of a window that may reset early - the exact failure this hedges.
    """
    try:
        value = float(db.get_setting(FRONT_LOAD_SETTING) or DEFAULT_FRONT_LOAD)
    except (TypeError, ValueError):
        return DEFAULT_FRONT_LOAD
    return value if 0.0 < value <= 1.0 else DEFAULT_FRONT_LOAD


def pace_target(elapsed_frac: float, gamma: Optional[float] = None) -> float:
    """The percent of a window that *should* be spent by now, front-loaded.

    `elapsed_frac` is how far through the window we are (0 at its start, 1 at
    its reset). Returns `100 * frac ** gamma`: with gamma < 1 the curve sits
    above the linear line for the whole window, so a run that is merely on the
    linear pace reads as a little behind and gets nudged to spend sooner.
    """
    gamma = front_load() if gamma is None else gamma
    frac = max(0.0, min(1.0, elapsed_frac))
    return (frac ** gamma) * 100.0


def weekly_pace(snapshot: Optional[dict] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """How the weekly spend compares to how much of the week has passed.

    Returns the weekly window with the *least* surplus (both weekly windows
    have to have headroom for a boost to be safe - if the Opus window is on
    pace, quickening runs would push it over even though the all-model window
    has plenty left), as {label, percent, elapsed, surplus, factor}. None when
    there is no trustworthy reading, in which case nothing boosts - the
    missing-data case must degrade to ordinary pacing, not to a burst.
    """
    snapshot = limits.cached() if snapshot is None else snapshot
    if not snapshot.get("ok") or snapshot.get("stale"):
        return None
    best: Optional[dict] = None
    for entry in snapshot.get("windows") or []:
        if not entry.get("key", "").startswith("seven_day"):
            continue
        # Pace on the *real* horizon, not the endpoint's claim: the weekly meter
        # was seen resetting every ~72h while resets_at insisted on seven days,
        # so a learned cadence (when established) both shortens the countdown and
        # the divisor. Falls back to resets_at / seven days until enough resets
        # have been observed, so early-life behaviour is unchanged.
        resets_in = cadence.effective_resets_in_sec(entry, now)
        if resets_in is None:
            continue
        week = cadence.effective_week_sec(entry["key"])
        frac = max(0.0, min(1.0, 1.0 - float(resets_in) / week))
        # `elapsed` is the honest wall-clock fraction of the window gone, kept
        # for the status line; `target` is the front-loaded curve the surplus
        # is measured against, so being on the linear line still reads as a
        # little behind and spends sooner.
        elapsed = frac * 100.0
        target = pace_target(frac)
        percent = float(entry.get("percent") or 0.0)
        surplus = target - percent
        if best is None or surplus < best["surplus"]:
            best = {
                "key": entry["key"],
                "label": entry["label"],
                "percent": percent,
                "elapsed": round(elapsed, 1),
                "target": round(target, 1),
                "surplus": round(surplus, 1),
            }
    if best is None:
        return None
    # Early in the week the surplus is naturally small (20% elapsed can only
    # ever be 20 points ahead), so the boost ramps up over the week by itself -
    # gentle on Tuesday, assertive on Sunday - which is exactly the "start
    # spending earlier, but spread it out" Wes asked for.
    if best["surplus"] < BOOST_START_SURPLUS:
        factor = 1.0
    else:
        factor = min(BOOST_MAX, 1.0 + (best["surplus"] - BOOST_START_SURPLUS) / BOOST_POINTS_PER_STEP)
    best["factor"] = round(factor, 2)
    return best


def boost_factor(snapshot: Optional[dict] = None, now: Optional[datetime] = None) -> float:
    """How much faster than configured the portal should run, >= 1.0.

    1.0 during a spend-down: that mode lifts the budget entirely, and layering
    a multiplier on top of "lifted" would mean nothing.
    """
    if spending_down(now):
        return 1.0
    pace = weekly_pace(snapshot, now)
    return pace["factor"] if pace else 1.0


def spend_mode(snapshot: Optional[dict] = None, now: Optional[datetime] = None) -> bool:
    """True while the portal is deliberately spending - either mode."""
    return spending_down(now) or boost_factor(snapshot, now) > 1.0


def run_budget(default: int) -> int:
    """The daily run budget, scaled up while ahead of pace.

    Scaled rather than lifted: the boost is automatic and unasked, so it keeps
    a ceiling - just a proportionally higher one. The spend-down window (which
    Wes says yes to) is the only thing that removes the ceiling.
    """
    factor = boost_factor()
    return default if factor <= 1.0 else int(default * factor)


# ---------------------------------------------------------------------------
# The spend-it-down window
# ---------------------------------------------------------------------------


def spend_down_candidate(snapshot: Optional[dict] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """The weekly window if it is about to reset with headroom going to waste.

    Only the seven-day windows are considered. A session window resets six
    times a day, so nothing is ever really wasted there; a weekly one resets
    once, and what is left on it at that moment is gone.
    """
    snapshot = limits.cached() if snapshot is None else snapshot
    now = now or datetime.now(timezone.utc)
    if not snapshot.get("ok") or snapshot.get("stale"):
        return None
    lead = timedelta(hours=SPEND_DOWN_LEAD_HOURS)
    best: Optional[dict] = None
    for entry in snapshot.get("windows") or []:
        if not entry.get("key", "").startswith("seven_day"):
            continue
        resets_at = _parse_iso(entry.get("resets_at") or "")
        if resets_at is None or not (now < resets_at <= now + lead):
            continue
        unused = 100.0 - float(entry.get("percent") or 0.0)
        if unused < SPEND_DOWN_MIN_UNUSED:
            continue
        candidate = dict(entry, unused=round(unused, 1))
        # If both weekly windows qualify, offer against the one with less to
        # spend - it is the one that will stop the burst.
        if best is None or candidate["unused"] < best["unused"]:
            best = candidate
    return best


def active_until(now: Optional[datetime] = None) -> Optional[datetime]:
    """The end of the running spend-down window, or None if none is running.

    Expiry is self-clearing: the setting is a timestamp, so a portal that was
    switched off through the reset comes back with no stale spend-down.
    """
    now = now or datetime.now(timezone.utc)
    until = _parse_iso(db.get_setting(ACTIVE_UNTIL) or "")
    if until is None:
        return None
    if until <= now:
        db.set_setting(ACTIVE_UNTIL, "")
        db.add_journal(
            None, "system", "status",
            "Spend-down window over - back to the portal's own run budget and pacing.",
        )
        return None
    return until


def spending_down(now: Optional[datetime] = None) -> bool:
    return active_until(now) is not None


def interval_min(default: int) -> int:
    """The pacing interval, shortened while spending down or ahead of pace.

    Never lengthened: if Wes has already set a short interval, neither mode is
    allowed to slow him down.
    """
    if spending_down():
        return min(default, SPEND_DOWN_INTERVAL_MIN)
    factor = boost_factor()
    if factor <= 1.0:
        return default
    return min(default, max(SPEND_DOWN_INTERVAL_MIN, int(default / factor)))


def parallel_cap(default: int) -> int:
    """How many runs may be in flight, widened while spending down.

    `max(...)` rather than a replacement: if Wes has set the everyday cap to 8
    he means 8, and a spend-down should never be the thing that *narrows* the
    portal.
    """
    return max(default, SPEND_DOWN_PARALLEL) if spending_down() else default


def budget_applies() -> bool:
    """Whether the portal's own runs-per-day cap is in force.

    False during a spend-down: that cap exists to stop the portal quietly
    eating a week's allowance, and a spend-down is Wes saying the allowance is
    there to be eaten.
    """
    return not spending_down()


def spend_model() -> str:
    """The model a spend-down upgrades cheaper-pinned runs to."""
    value = (db.get_setting(SPEND_MODEL_SETTING) or "").strip()
    return value if value in config.MODEL_VALUES else DEFAULT_SPEND_MODEL


def route_for_spend(configured: str, task: str = "", now: Optional[datetime] = None) -> tuple[str, str]:
    """(model to actually run, reason) - the spend-down's model upgrade.

    While a spend-down is running, a run pinned to a model cheaper than the
    spend model is routed up to it, so an expiring window is spent on quality
    rather than only on more runs. reason is "" whenever nothing changed.

    No-op unless a spend-down is actually running (the ahead-of-pace boost does
    not upgrade models - see SPEND_MODEL_SETTING). Never lowers a run's model:
    one already on the spend model or above is left alone. Research bursts are
    left alone too - they carry their own model (fable) and are the spend-down's
    own mechanism, not a project whose cost pin needs overriding here.
    """
    if task == "research" or not spending_down(now):
        return configured, ""
    target = spend_model()
    if MODEL_SPEND_RANK.get(configured, 99) >= MODEL_SPEND_RANK.get(target, 0):
        return configured, ""
    return target, f"spending the weekly window down on {target} (project is pinned to {configured})"


AFFIRMATIVE = re.compile(
    r"\b(yes|yep|yeah|yup|sure|ok|okay|go|do it|please|spend|burn|use it)\b", re.I
)
NEGATIVE = re.compile(r"\b(no|nope|don't|dont|do not|leave it|skip|stop|later)\b", re.I)


def is_affirmative(answer: str) -> bool:
    """Did Wes say yes? Whichever signal comes first wins: 'no - don't spend
    it' contains 'spend' and must not read as a yes, but 'Yes, spend it down.
    Don't go past 70%, though' is a yes with a condition - and 'negatives
    always win' read exactly that real answer of his as a decline."""
    text = (answer or "").strip()
    if not text:
        return False
    yes = AFFIRMATIVE.search(text)
    no = NEGATIVE.search(text)
    if yes and no:
        return yes.start() < no.start()
    return bool(yes)


def offer_text(candidate: dict) -> str:
    return (
        f"Your {candidate['label']} Claude window resets in {candidate['resets_in']} "
        f"with {candidate['unused']:g}% of it unused - that headroom does not roll "
        f"over, it just disappears at the reset. Want me to spend it down? Say yes "
        f"and I will lift the portal's own run budget and pacing until then and work "
        f"through the backlog; say no and I will leave it alone."
    )


def _meta_project_id() -> Optional[int]:
    project = db.get_project_by_slug(config.META_PROJECT_SLUG)
    return project["id"] if project is not None else None


def pending_question() -> Optional[db.sqlite3.Row]:
    raw = db.get_setting(OFFER_QUESTION) or ""
    if not raw:
        return None
    try:
        return db.get_question(int(raw))
    except ValueError:
        return None


def _same_window(stored: str, candidate_resets: str) -> bool:
    """Whether a reset stamp refers to the window already offered against.

    Compared as timestamps with tolerance, not as strings: the endpoint
    reports the same weekly reset as 21:00:00 on one poll and 20:59:59 on the
    next, and an exact-string match turned that one-second jitter into a fresh
    question every few minutes.
    """
    if not stored:
        return False
    if stored == candidate_resets:
        return True
    a, b = _parse_iso(stored), _parse_iso(candidate_resets)
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= WINDOW_MATCH_TOLERANCE_SEC


def should_offer(snapshot: Optional[dict] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """The candidate to ask about, or None because there is nothing to ask.

    Asked at most once per weekly window, keyed on that window's reset time:
    an unanswered offer is not repeated (it is still sitting in his questions
    list), and neither is an answered one, whichever way he answered.
    """
    now = now or datetime.now(timezone.utc)
    # Belt to `limits.cached()`'s braces. That guard already starves this
    # function of a candidate on an API-key install, but this is the one
    # pacing decision that *writes to the owner* - "your weekly window resets
    # in 6h with 47% unused, shall I spend it?" is a nonsense question to put
    # in front of somebody whose runs bill a card and whose allowance expiring
    # costs them nothing. A wrong hold wastes a run; a wrong question wastes
    # their attention and teaches them to ignore the next one.
    if not spawnauth.paces_on_subscription():
        return None
    if spending_down(now):
        return None
    pending = pending_question()
    if pending is not None and pending["status"] == "open":
        stored = _parse_iso(db.get_setting(OFFERED_FOR) or "")
        if stored is None or stored > now:
            # Belt to the braces below: whatever the reset stamps say, a
            # spend-down question he has not answered yet means the portal has
            # nothing new to ask him.
            return None
        # The window that question offered has already reset, so its headroom
        # no longer exists - retire it rather than leaving two spend-down
        # questions open at once.
        db.dismiss_question(pending["id"])
        db.set_setting(OFFER_QUESTION, "")
    candidate = spend_down_candidate(snapshot, now)
    if candidate is None:
        return None
    if _same_window(db.get_setting(OFFERED_FOR) or "", candidate.get("resets_at") or ""):
        return None
    return candidate


def record_offer(candidate: dict, question_id: int) -> None:
    db.set_setting(OFFERED_FOR, candidate.get("resets_at") or "")
    db.set_setting(OFFER_QUESTION, str(question_id))


def create_offer_question(candidate: dict) -> Optional[db.sqlite3.Row]:
    """Raise the offer as a real question on the meta-project.

    A question rather than a notification because it needs an answer and
    because every channel Wes already answers on - the project page, the
    questions page, Telegram - works on questions for free.
    """
    project_id = _meta_project_id()
    if project_id is None:
        return None
    text = offer_text(candidate)
    filing = db.file_question(
        project_id,
        text,
        context=(
            f"{candidate['label']} at {candidate['percent']:g}%, resets "
            f"{candidate['resets_at']}. Nothing is running differently until you answer."
        ),
        # The offer is yes/no-shaped by construction; one tap should answer it.
        quick_options=quickreplies.encode(quickreplies.derive(text)),
    )
    if not filing.created:
        # An offer for a different window whose wording is near identical -
        # "you asked me way too many times here, I just want to be asked once".
        # Still record it against the matched question so the OFFERED_FOR stamp
        # advances; otherwise this path retries on every tick.
        record_offer(candidate, filing.row["id"])
        log.info("Spend-down offer deduped against open question %s", filing.row["id"])
        return None
    record_offer(candidate, filing.row["id"])
    return filing.row


def start(until: datetime) -> None:
    db.set_setting(ACTIVE_UNTIL, until.isoformat(timespec="seconds"))
    db.add_journal(
        None, "system", "status",
        f"Spend-down window open until {until.isoformat(timespec='minutes')}: the portal's "
        f"own run budget and pacing are lifted, the real Claude limits still apply.",
    )


def settle_offer(now: Optional[datetime] = None) -> Optional[bool]:
    """If the outstanding offer has been answered, act on it.

    Returns True if a spend-down just started, False if Wes declined, None if
    there was nothing to settle.
    """
    now = now or datetime.now(timezone.utc)
    question = pending_question()
    if question is None:
        return None
    if question["status"] == "open":
        return None
    db.set_setting(OFFER_QUESTION, "")
    if question["status"] != "answered" or not is_affirmative(question["answer"] or ""):
        return False
    until = _parse_iso(db.get_setting(OFFERED_FOR) or "")
    if until is None or until <= now:
        # He said yes after the window had already turned over. Nothing to
        # spend; do not open a window with no end.
        return False
    # "Yes, but don't go past 70% of my 5hr limit" - a percentage in an
    # affirmative answer is an instruction about the session guard, so it
    # becomes the setting rather than being read and forgotten.
    cap = re.search(r"(\d{1,3})\s*%", question["answer"] or "")
    if cap and 10 <= int(cap.group(1)) <= 100:
        db.set_setting(SESSION_GUARD_SETTING, cap.group(1))
    start(until)
    return True


def status_line(now: Optional[datetime] = None) -> str:
    """One line for the dashboard, empty when nothing special is happening."""
    until = active_until(now)
    if until is not None:
        left = int((until - (now or datetime.now(timezone.utc))).total_seconds())
        return (
            f"spending down the weekly window - budget and pacing lifted for "
            f"{limits.humanize_until(left)}, cheaper-pinned runs upgraded to {spend_model()}"
        )
    pace = weekly_pace(now=now)
    if pace is not None and pace["factor"] > 1.0:
        learned = cadence.describe(pace["key"])
        tail = f"; {learned}" if learned else ""
        return (
            f"ahead of pace on the {pace['label']} window ({pace['percent']:g}% used vs a "
            f"{pace['target']:g}% front-loaded target, {pace['elapsed']:g}% of the window gone) - "
            f"running x{pace['factor']:g} faster, session held at {session_guard():g}%{tail}"
        )
    return ""
