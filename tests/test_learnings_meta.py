"""Added / last-confirmed dates for live learnings (#220).

learnings.md is fed into every run of every project, and Wes asked for its lines
to read as durable facts rather than a dated log - so the dates live in a
sidecar, keyed by the same normalized text the write gate dedupes on, never in
the prompt-facing file. The point these tests defend: a re-observed fact (which
the write gate already drops as a NOOP) is *confirmation* the fact still holds,
so the gate records it - and a line confirmed often and recently is plainly
load-bearing while one added once and never re-seen is a compaction candidate.
"""
from __future__ import annotations

from datetime import datetime, timezone

from starlette.testclient import TestClient

from app import config, memory, worker


def _write(text: str) -> None:
    config.LEARNINGS_MD.parent.mkdir(parents=True, exist_ok=True)
    config.LEARNINGS_MD.write_text(text, encoding="utf-8")


def _key(text: str) -> str:
    return worker._learning_key(text)


DAY1 = datetime(2026, 4, 1, tzinfo=timezone.utc)
DAY2 = datetime(2026, 7, 23, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The sidecar's pure helpers
# --------------------------------------------------------------------------

def test_touch_creates_then_confirms():
    meta: dict = {}
    memory.meta_touch(meta, "k", day="2026-04-01")
    assert meta["k"] == {"added": "2026-04-01", "confirmed": "2026-04-01", "count": 1}
    # A later sighting bumps confirmed and count, leaves added alone.
    memory.meta_touch(meta, "k", day="2026-07-23")
    assert meta["k"] == {"added": "2026-04-01", "confirmed": "2026-07-23", "count": 2}


def test_touch_ignores_an_empty_key():
    meta: dict = {}
    memory.meta_touch(meta, "", day="2026-04-01")
    assert meta == {}


def test_supersede_carries_the_added_date_and_bumps_count():
    meta = {"old": {"added": "2026-04-01", "confirmed": "2026-05-01", "count": 3}}
    memory.meta_supersede(meta, "old", "new", day="2026-07-23")
    assert "old" not in meta
    assert meta["new"] == {"added": "2026-04-01", "confirmed": "2026-07-23", "count": 4}


def test_supersede_of_an_untracked_line_dates_from_today():
    meta: dict = {}
    memory.meta_supersede(meta, "old", "new", day="2026-07-23")
    assert meta["new"] == {"added": "2026-07-23", "confirmed": "2026-07-23", "count": 2}


def test_supersede_in_place_keeps_the_entry():
    # A rephrasing whose normalized key is unchanged must not delete itself.
    meta = {"k": {"added": "2026-04-01", "confirmed": "2026-05-01", "count": 2}}
    memory.meta_supersede(meta, "k", "k", day="2026-07-23")
    assert meta["k"] == {"added": "2026-04-01", "confirmed": "2026-07-23", "count": 3}


def test_forget_removes_a_key():
    meta = {"k": {"added": "x", "confirmed": "y", "count": 1}}
    memory.meta_forget(meta, "k")
    memory.meta_forget(meta, "absent")  # no-op, no raise
    assert meta == {}


def test_prune_drops_orphaned_keys():
    meta = {"live": {}, "gone": {}}
    memory.meta_prune(meta, ["live"])
    assert set(meta) == {"live"}


def test_load_save_round_trip(temp_data_dir):
    memory.save_learnings_meta({"k": {"added": "2026-04-01", "confirmed": "2026-04-01", "count": 1}})
    assert memory.load_learnings_meta() == {
        "k": {"added": "2026-04-01", "confirmed": "2026-04-01", "count": 1}
    }


def test_a_missing_sidecar_reads_as_empty(temp_data_dir):
    assert memory.load_learnings_meta() == {}


def test_a_corrupt_sidecar_reads_as_empty(temp_data_dir):
    memory.learnings_meta_path().parent.mkdir(parents=True, exist_ok=True)
    memory.learnings_meta_path().write_text("{not json", encoding="utf-8")
    assert memory.load_learnings_meta() == {}


def test_a_non_object_sidecar_reads_as_empty(temp_data_dir):
    memory.learnings_meta_path().parent.mkdir(parents=True, exist_ok=True)
    memory.learnings_meta_path().write_text("[1, 2, 3]", encoding="utf-8")
    assert memory.load_learnings_meta() == {}


# --------------------------------------------------------------------------
# The write gate feeds the sidecar
# --------------------------------------------------------------------------

def test_a_new_learning_is_dated_added_and_confirmed_today(temp_data_dir):
    worker._append_learnings(["Bun is the default runtime on the box"], when=DAY1)
    ent = memory.load_learnings_meta()[_key("Bun is the default runtime on the box")]
    assert ent == {"added": "2026-04-01", "confirmed": "2026-04-01", "count": 1}


def test_re_observing_a_fact_confirms_it_without_touching_the_file(temp_data_dir):
    worker._append_learnings(["Bun is the default runtime on the box"], when=DAY1)
    before = config.LEARNINGS_MD.read_text()
    # A later run notices the same fact: the file is a NOOP, but the sidecar
    # records a fresh confirmation.
    worker._append_learnings(["Bun is the default runtime on the box."], when=DAY2)
    assert config.LEARNINGS_MD.read_text() == before  # file untouched
    ent = memory.load_learnings_meta()[_key("Bun is the default runtime on the box")]
    assert ent["added"] == "2026-04-01"
    assert ent["confirmed"] == "2026-07-23"
    assert ent["count"] == 2


def test_a_rephrasing_carries_the_original_added_date(temp_data_dir):
    worker._append_learnings(["The public IP is dynamic and changes"], when=DAY1)
    added_key = _key("The public IP is dynamic and changes")
    assert memory.load_learnings_meta()[added_key]["added"] == "2026-04-01"
    # A refined rephrasing supersedes it (>=0.6 word overlap): the new entry
    # keeps the April added date and counts as a fresh confirmation.
    worker._append_learnings(["The public IP is dynamic and changes often"], when=DAY2)
    meta = memory.load_learnings_meta()
    new_key = _key("The public IP is dynamic and changes often")
    assert new_key in meta
    assert meta[new_key]["added"] == "2026-04-01"
    assert meta[new_key]["confirmed"] == "2026-07-23"
    # The old key is gone, not left orphaned beside the new one.
    assert added_key not in meta


def test_deleting_a_fact_forgets_its_dates(temp_data_dir):
    worker._append_learnings(["Port 25 outbound is blocked to MX hosts"], when=DAY1)
    assert memory.load_learnings_meta()  # something is tracked
    worker._append_learnings(
        [{"op": "delete", "text": "Port 25 outbound is blocked to MX hosts"}], when=DAY2
    )
    assert memory.load_learnings_meta() == {}


def test_a_hand_removed_line_is_pruned_from_the_sidecar(temp_data_dir):
    worker._append_learnings(["fact alpha here", "fact beta here"], when=DAY1)
    assert len(memory.load_learnings_meta()) == 2
    # Wes edits the file directly, removing alpha; the next write GCs its meta.
    _write("- fact beta here\n")
    worker._append_learnings(["fact gamma here"], when=DAY2)
    meta = memory.load_learnings_meta()
    assert _key("fact alpha here") not in meta
    assert _key("fact beta here") in meta
    assert _key("fact gamma here") in meta


# --------------------------------------------------------------------------
# Freshness listing and the page
# --------------------------------------------------------------------------

def test_freshness_lists_stalest_first_with_undated_last(temp_data_dir):
    worker._append_learnings(["fact one here"], when=DAY1)   # confirmed 2026-04-01
    worker._append_learnings(["fact two here"], when=DAY2)   # confirmed 2026-07-23
    # A line with no sidecar entry (hand-added after the gate wrote its two).
    with open(config.LEARNINGS_MD, "a", encoding="utf-8") as f:
        f.write("- an undated hand line\n")
    fresh = worker.learnings_freshness()
    texts = [e.text for e in fresh]
    assert texts == ["fact one here", "fact two here", "an undated hand line"]
    assert fresh[0].confirmed == "2026-04-01" and fresh[0].tracked
    assert fresh[2].count == 0 and not fresh[2].tracked


def test_freshness_is_empty_when_the_file_is_missing(temp_data_dir):
    assert worker.learnings_freshness() == []


def test_the_memory_page_shows_the_entry_ages_section(temp_data_dir):
    from app import main

    worker._append_learnings(["Bun is the default runtime on the box"], when=DAY1)
    worker._append_learnings(["Bun is the default runtime on the box."], when=DAY2)
    client = TestClient(main.app)
    html = client.get("/memory").text
    assert "Entry ages" in html
    assert "seen 2" in html  # confirmed twice
