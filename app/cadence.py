"""Learning the *real* reset cadence of the weekly window, empirically.

`app/limits.py` reads each window's `resets_at` straight from the account, and
until now the whole pacing layer trusted it. The research burst (RESEARCH.md
§6) found that trust is misplaced: a careful monitoring gist caught the weekly
meter silently rolling back toward zero every **~72 hours** while `resets_at`
went on claiming a reset seven days out. If that is true here, the boost in
`app/pacing.py` badly under-reads reality - it divides elapsed time by a
seven-day `WEEK_SEC`, so a window that actually turns over in three days looks
only ~40% elapsed when it is really 100%, and the portal sits on headroom that
evaporates instead of spending it.

`resets_at` cannot be the teacher, because the whole point is that it lies. The
one signal that never lies is the utilization number itself: within a window it
only ever climbs (usage accumulates), so **any meaningful drop between two good
readings is a reset that just happened**, whatever `resets_at` says. This module
watches the polled readings for those drops, records when each one occurred, and
turns a run of them into a learned interval the pacing layer can pace on instead
of - or rather, alongside, taking whichever is sooner - the endpoint's claim.

Everything here is deliberately conservative and self-correcting:

* It learns only from trustworthy readings (a fetch that actually succeeded and
  is not stale), so an outage never invents a phantom reset.
* It needs several clean resets before it will assert a cadence at all, and it
  drops implausibly short gaps (jitter, a double-detection) before averaging,
  so one fluke cannot move the number.
* It clamps the result to a sane band; anything outside it is ignored rather
  than acted on.
* Every reader falls back to the old `resets_at`/seven-day behavior when
  nothing has been learned yet, so the portal on day one behaves exactly as it
  did before this module existed.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import db

log = logging.getLogger("portal.cadence")

# The learned state lives in one settings row: a small dict keyed by window
# ("seven_day", "seven_day_opus"), each carrying the last reading seen and a
# short ring of the reset timestamps observed for it.
STATE_KEY = "reset_cadence_json"

# A drop of at least this many utilization points between two consecutive good
# readings is treated as a reset. Utilization never falls mid-window, so the
# threshold only exists to shrug off endpoint rounding jitter - a genuine reset
# drops by whatever the window was sitting at, which is far more than this.
RESET_DROP_THRESHOLD = 15.0

# How many reset timestamps to keep. Enough to average over a few weeks of the
# real cadence and let an old, stale rhythm age out; not so many that a changed
# cadence takes forever to re-learn.
RESET_RING = 12

# Before the module will assert *any* learned interval: this many recorded
# resets (so at least MIN_GAPS gaps between them survive the plausibility
# filter). A single gap could be a coincidence - Anthropic was seen doubling
# and reverting limits over one holiday - so a cadence is only trusted once it
# has repeated.
MIN_RESETS = 3
MIN_GAPS = 2

# The plausible band for a *weekly* window's real cadence. A gap shorter than
# the floor is almost certainly a spurious double-detection and is dropped
# before averaging; a learned interval longer than the ceiling is implausible
# for a window the account calls "weekly" and is ignored entirely (the reader
# falls back to the seven-day default).
MIN_INTERVAL_SEC = 12 * 3600      # 12h
MAX_INTERVAL_SEC = 8 * 86400      # 8 days
WEEK_SEC = 7 * 86400              # the default horizon, matching pacing.WEEK_SEC


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse_iso(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _load() -> dict:
    raw = db.get_setting(STATE_KEY) or ""
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    db.set_setting(STATE_KEY, json.dumps(state))


def _weekly_entries(snapshot: dict):
    for entry in snapshot.get("windows") or []:
        if str(entry.get("key") or "").startswith("seven_day"):
            yield entry


# ---------------------------------------------------------------------------
# Watching the readings go by
# ---------------------------------------------------------------------------


def record_reading(snapshot: Optional[dict], now: Optional[datetime] = None) -> None:
    """Fold one fresh reading into what we know about the reset cadence.

    Called by `limits.refresh()` after a *successful* fetch. A reading whose
    utilization has fallen since the last one we saw marks a reset at `now`.
    Never raises: a malformed snapshot leaves the learned state untouched.
    """
    if not isinstance(snapshot, dict) or not snapshot.get("ok") or snapshot.get("stale"):
        return
    now = _now(now)
    state = _load()
    changed = False
    for entry in _weekly_entries(snapshot):
        key = entry.get("key")
        if not key:
            continue
        try:
            percent = float(entry.get("percent") or 0.0)
        except (TypeError, ValueError):
            continue
        rec = state.get(key) or {}
        prev = rec.get("last_percent")
        resets = list(rec.get("resets") or [])
        if isinstance(prev, (int, float)) and (float(prev) - percent) >= RESET_DROP_THRESHOLD:
            resets.append(now.isoformat(timespec="seconds"))
            resets = resets[-RESET_RING:]
            log.info(
                "cadence: %s reset detected (%.1f%% -> %.1f%%); %d observed",
                key, prev, percent, len(resets),
            )
        state[key] = {
            "last_percent": round(percent, 1),
            "last_at": now.isoformat(timespec="seconds"),
            "resets": resets,
        }
        changed = True
    if changed:
        _save(state)


# ---------------------------------------------------------------------------
# What we have learned
# ---------------------------------------------------------------------------


def _resets(key: str) -> list[datetime]:
    rec = _load().get(key) or {}
    out = [dt for dt in (_parse_iso(v) for v in (rec.get("resets") or [])) if dt]
    out.sort()
    return out


def last_reset_at(key: str) -> Optional[datetime]:
    """When we last saw this window roll over, or None if never."""
    resets = _resets(key)
    return resets[-1] if resets else None


def observed_interval_sec(key: str) -> Optional[float]:
    """The learned reset interval in seconds, or None if not established yet.

    The median of the plausible gaps between recorded resets - median rather
    than mean so a single odd gap (a poll the portal was down for, a missed
    detection) cannot drag it. None whenever there is not yet enough clean
    evidence, or the answer lands outside the plausible band.
    """
    resets = _resets(key)
    if len(resets) < MIN_RESETS:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(resets, resets[1:])]
    gaps = [g for g in gaps if g >= MIN_INTERVAL_SEC]
    if len(gaps) < MIN_GAPS:
        return None
    interval = statistics.median(gaps)
    if interval > MAX_INTERVAL_SEC:
        return None
    return interval


def predicted_next_reset(key: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """When the learned cadence says this window next turns over, or None.

    The last observed reset plus the learned interval, rolled forward past now
    if the portal has been idle across one (so a stale last-reset never yields a
    predicted reset in the past).
    """
    now = _now(now)
    interval = observed_interval_sec(key)
    last = last_reset_at(key)
    if interval is None or last is None:
        return None
    step = timedelta(seconds=interval)
    nxt = last + step
    # Bounded roll-forward: interval is >= 12h so this loops a handful of times
    # at most even after a long outage.
    while nxt <= now:
        nxt += step
    return nxt


def effective_resets_in_sec(entry: dict, now: Optional[datetime] = None) -> Optional[int]:
    """Seconds until this window really resets - the *sooner* of the two views.

    The endpoint's own countdown and the learned prediction, whichever is
    closer, because the risk being managed is headroom evaporating unspent and
    the sooner reset is the one that does it. Falls back to the endpoint's
    figure alone when nothing has been learned, so behavior is unchanged until
    the cadence is established.
    """
    now = _now(now)
    api = entry.get("resets_in_sec")
    api = int(api) if isinstance(api, (int, float)) and api >= 0 else None
    predicted = predicted_next_reset(entry.get("key") or "", now)
    pred = int((predicted - now).total_seconds()) if predicted else None
    if pred is not None and pred < 0:
        pred = None
    candidates = [s for s in (api, pred) if s is not None]
    if not candidates:
        return api
    return min(candidates)


def effective_week_sec(key: str) -> float:
    """The horizon to divide elapsed time by - learned cadence or seven days."""
    return observed_interval_sec(key) or float(WEEK_SEC)


def describe(key: str) -> str:
    """A short human note on the learned cadence, '' when nothing is learned.

    Used to tell Wes, at the moment the portal acts on it, that it is pacing on
    a measured ~72h rhythm rather than the endpoint's seven-day claim.
    """
    interval = observed_interval_sec(key)
    if interval is None:
        return ""
    hours = interval / 3600.0
    if hours >= 48:
        span = f"~{hours / 24:.1f}d"
    else:
        span = f"~{hours:.0f}h"
    return f"learned reset cadence {span} (endpoint claims 7d)"
