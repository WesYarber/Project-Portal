"""What the CLI itself says about why an API call is being retried.

Until now the portal decided a run had hit a usage limit by looking for the
words "limit" and "rate" in the result text and in stderr (`agent_runner.
_looks_rate_limited`). That is a guess about a sentence written for a person,
and it fails in both directions: a wording change makes a real limit invisible,
and a network outage whose message happens to say "rate" parks the whole
scheduler for as long as the usage meter says the window is closed.

The CLI has been telling us the answer the whole time. `--output-format
stream-json` emits a `system` event with subtype `api_retry` every time it is
about to retry a failed API call, and it carries the error as structured data
rather than as prose::

    {"type": "system", "subtype": "api_retry",
     "attempt": 2, "max_retries": 10, "retry_delay_ms": 8000,
     "error_status": 429,
     "error": {"message": "...", "status": 429, "request_id": "...",
               "formatted": "...",
               "connection": {"code": "ETIMEDOUT", ...} | null,
               "is_network_down": false,
               "rate_limits": {"resets_at": 1754570000,
                               "rate_limit_type": "..."} | null}}

Two fields carry most of the weight, and both are load-bearing:

* **`error.rate_limits` is non-null if and only if this was a quota 429.** The
  CLI's own schema says so in as many words ("Quota-429 headers surfaced by the
  retry banner; null when not a quota 429"), which makes it the authoritative
  answer to the question the string match was estimating. A 429 that is a
  short-term throttle rather than an exhausted allowance has it null, and those
  are genuinely different events: one clears in seconds, the other in hours.
* **`resets_at` comes from Anthropic's own 429 headers**, so it is ground truth
  about when the allowance returns. The portal's existing answer to that
  question is a *separate* HTTP call to the usage endpoint made during the
  failing run's teardown - a reading that can itself fail, and one whose reset
  cadence RESEARCH.md §6 flagged as thinly evidenced. A number that arrived
  attached to the refusal beats a number fetched afterwards from somewhere else.

## Retries happen on runs that go on to succeed

This is the part that changes scheduling rather than just reporting. The CLI
retries *internally*: a run can spend ten minutes stalled against a quota wall,
get through, and finish green. Today that run teaches the portal nothing, so the
next one is launched straight into the same wall - and the one after that.
Reading these events means the portal can back off on the evidence rather than
on the body count.

It is also the only reason a stalled run is visible at all. An `api_retry` event
is the sole output during a retry wait, so a console that drops it shows a run
that has silently stopped moving, which is exactly the quiet failure Wes objects
to on principle.

## Absence proves nothing

A run can die on a usage limit without ever emitting one of these - the CLI only
retries what it considers retryable, and a hard refusal is not. So this module
is evidence *for* a category, never against one, and `agent_runner` keeps the
old string match as the fallback for when no event arrived at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# The categories a retry can fall into, most specific first. These are the
# portal's names, not the CLI's: the CLI hands over a status code and a couple
# of booleans, and the point of this module is to turn those into the small
# vocabulary the scheduler actually reasons in.
QUOTA = "quota"  # the allowance is spent - the only one that justifies a long hold
THROTTLED = "throttled"  # a 429 that is not a quota 429: too fast, not too much
OVERLOADED = "overloaded"  # Anthropic's capacity, not this account's
NETWORK = "network"  # this box could not reach them at all
SERVER = "server"  # some other 5xx
AUTH = "auth"  # 401/403 - the login, which no amount of waiting fixes
OTHER = "other"

# How each category reads in a log line or a journal entry.
LABELS: dict[str, str] = {
    QUOTA: "usage limit",
    THROTTLED: "throttled",
    OVERLOADED: "overloaded",
    NETWORK: "network unreachable",
    SERVER: "server error",
    AUTH: "auth",
    OTHER: "error",
}


@dataclass(frozen=True)
class Retry:
    """One `api_retry` event, read."""

    category: str
    attempt: int = 0
    max_retries: int = 0
    delay_ms: int = 0
    status: Optional[int] = None
    message: str = ""
    # Only ever set on a QUOTA retry, straight off Anthropic's 429 headers.
    resets_at: Optional[datetime] = None
    limit_type: Optional[str] = None

    @property
    def label(self) -> str:
        return LABELS.get(self.category, OTHER)

    def describe(self) -> str:
        """The one-line form used in the run console and in journal entries."""
        bits = [self.label]
        if self.status:
            bits.append(f"HTTP {self.status}")
        if self.limit_type:
            bits.append(str(self.limit_type))
        head = ", ".join(bits)
        wait = f"retrying in {self.delay_ms / 1000:.0f}s" if self.delay_ms else "retrying"
        attempts = ""
        if self.attempt and self.max_retries:
            attempts = f" (attempt {self.attempt}/{self.max_retries})"
        return f"{head} - {wait}{attempts}"


def _int(value, default: Optional[int] = None) -> Optional[int]:
    """Read a number defensively. Every field here comes off somebody else's
    wire format, and a run must not die because one arrived as a string."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resets_at(rate_limits: dict) -> Optional[datetime]:
    """`resets_at` is a unix timestamp in seconds. Anything else - missing,
    zero, a string, absurdly out of range - is treated as absent rather than
    trusted, because this value decides how long the whole scheduler sleeps."""
    epoch = _int(rate_limits.get("resets_at"))
    if not epoch or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def classify(event: dict) -> Optional[Retry]:
    """One stream event -> a `Retry`, or None if this is not an api_retry.

    The order of the checks is the whole design. `rate_limits` is tested first
    and on its own, because it is the CLI's own explicit statement that this was
    a quota 429 - inferring quota from the 429 status instead would sweep in
    every short-term throttle and hold the scheduler for hours over something
    that clears in seconds.
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") != "system" or event.get("subtype") != "api_retry":
        return None

    error = event.get("error")
    error = error if isinstance(error, dict) else {}
    # `error_status` is null for connection errors that never got an HTTP
    # response; the nested copy is the same number and is read as a fallback.
    status = _int(event.get("error_status"), _int(error.get("status")))
    message = str(error.get("formatted") or error.get("message") or "").strip()

    rate_limits = error.get("rate_limits")
    if isinstance(rate_limits, dict):
        category = QUOTA
        resets_at = _resets_at(rate_limits)
        limit_type = rate_limits.get("rate_limit_type") or None
    else:
        resets_at = None
        limit_type = None
        if error.get("is_network_down") or isinstance(error.get("connection"), dict):
            category = NETWORK
        elif status is None:
            # No HTTP response and no connection detail: still not something
            # that reached their servers, so it is this box's problem.
            category = NETWORK
        elif status in (401, 403):
            category = AUTH
        elif status == 429:
            category = THROTTLED
        elif status in (503, 529):
            category = OVERLOADED
        elif status >= 500:
            category = SERVER
        else:
            category = OTHER

    return Retry(
        category=category,
        attempt=_int(event.get("attempt"), 0) or 0,
        max_retries=_int(event.get("max_retries"), 0) or 0,
        delay_ms=max(0, _int(event.get("retry_delay_ms"), 0) or 0),
        status=status,
        message=message,
        resets_at=resets_at,
        limit_type=str(limit_type) if limit_type else None,
    )


@dataclass
class RetryLog:
    """What one run's retries added up to.

    Accumulated as the stream arrives, because the events are only on the wire -
    a run that retried and then succeeded leaves no trace of it in the result
    event, which is precisely the case worth knowing about.
    """

    count: int = 0
    delay_ms: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    # The most recent quota retry, kept whole: its `resets_at` is what the
    # scheduler backs off to. Latest rather than first, because a later 429
    # carries a later reset and waiting to the earlier one wakes into a wall.
    quota: Optional[Retry] = None

    def observe(self, event: dict) -> Optional[Retry]:
        retry = classify(event)
        if retry is None:
            return None
        self.count += 1
        self.delay_ms += retry.delay_ms
        self.categories[retry.category] = self.categories.get(retry.category, 0) + 1
        if retry.category == QUOTA:
            if self.quota is None or _newer(retry, self.quota):
                self.quota = retry
        return retry

    @property
    def saw_quota(self) -> bool:
        return self.quota is not None

    @property
    def delay_seconds(self) -> float:
        return self.delay_ms / 1000.0

    def summary(self) -> Optional[str]:
        """A sentence for the run's journal entry, or None when nothing retried.

        Worth writing even on a run that succeeded: minutes spent waiting on
        somebody else's server is the difference between a slow agent and a
        slow API, and only this can tell them apart afterwards.
        """
        if not self.count:
            return None
        kinds = ", ".join(
            f"{LABELS.get(name, name)} x{n}" if n > 1 else LABELS.get(name, name)
            for name, n in sorted(self.categories.items(), key=lambda kv: -kv[1])
        )
        return (
            f"{self.count} API retr{'y' if self.count == 1 else 'ies'} "
            f"({kinds}), {self.delay_seconds:.0f}s waiting"
        )


def _newer(candidate: Retry, current: Retry) -> bool:
    """Prefer the retry that knows more. A later event with no `resets_at` must
    not displace an earlier one that had it, or the authoritative reset is lost
    to a follow-up retry whose headers were thinner."""
    if candidate.resets_at is None:
        return False
    if current.resets_at is None:
        return True
    return candidate.resets_at >= current.resets_at
