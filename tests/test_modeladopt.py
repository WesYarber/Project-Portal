"""Moving the portal onto a new model by itself, without breaking every run.

Wes, 2026-09-23: *"Always adopt the new models- no need to ask. Send me a
notification maybe still, though, as it's cool to know when they drop!"*

The check he withdrew the question for is still load-bearing, and these tests
exist because of what the probe found the same day:

    $ claude -p "Reply with only the word: ok" --model claude-opus-5-5
    [claude-code:unrecognized_model] {"model":"claude-opus-5-5", ...}
    API Error: 400 Claude Code 2.1.258 does not support this model;
    version 2.1.280 or newer is required.

...which came back with **exit status 0**. That transcript is pinned below as
`GATED_OUTPUT` rather than paraphrased, because every interesting decision in
this module is a decision about how to read it.
"""
from __future__ import annotations

import pytest

from app import config, db, modeladopt

# The real thing, copied verbatim from the 2026-09-23 probe.
GATED_OUTPUT = (
    '"claude-opus-5-5" isn\'t described by this version\'s model catalog; update '
    "Claude Code, or map it with behavesAs on a modelPicker row.\n"
    '[claude-code:unrecognized_model] {"model":"claude-opus-5-5","query_source":"sdk"}\n'
    "API Error: 400 Claude Code 2.1.258 does not support this model; "
    "version 2.1.280 or newer is required. Run 'claude update', or update the "
    "Claude desktop app, then try again.\n"
)

CATALOG = [
    {"id": "claude-opus-5", "display_name": "Claude Opus 5", "created_at": "2026-07-24T00:00:00Z"},
    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "created_at": "2026-06-29T00:00:00Z"},
]


def _ok(*_a, **_k):
    return {"ok": True, "required_cli": "", "error": ""}


def _gated(*_a, **_k):
    return modeladopt.read_probe(GATED_OUTPUT, 0)


def _no_verdict(*_a, **_k):
    return {"ok": False, "required_cli": "", "error": "could not run claude: no such file"}


# --------------------------------------------------------------------------
# Reading a model id
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model_id,family", [
    ("claude-opus-5-5", "opus"),
    ("claude-opus-6", "opus"),
    ("claude-fable-5-1", "fable"),
    ("claude-sonnet-5", "sonnet"),
    ("claude-haiku-4-5-20251001", "haiku"),
    ("gpt-9", None),
    ("claude-mercury-1", None),
    ("", None),
])
def test_the_family_is_read_out_of_the_id(model_id, family):
    assert modeladopt.family_of(model_id) == family


def test_a_family_name_is_matched_whole_not_as_a_prefix():
    # "claude-opusculum-1" must not read as the opus family.
    assert modeladopt.family_of("claude-opusculum-1") is None


@pytest.mark.parametrize("model_id,parts", [
    ("claude-opus-5-5", (5, 5)),
    ("claude-opus-5", (5,)),
    ("claude-fable-5-1", (5, 1)),
    ("claude-haiku-4-5-20251001", (4, 5)),   # the date stamp is not a version part
])
def test_the_version_is_the_numbers_after_the_family(model_id, parts):
    assert modeladopt.version_parts(model_id) == parts


def test_a_date_stamp_never_outranks_a_real_release():
    # 20260101 > 5 as an integer, so a naive comparison would let a snapshot of
    # Opus 5 displace Opus 5.5 and quietly move every run back a release.
    assert not modeladopt.supersedes("claude-opus-5-20260101", "claude-opus-5-5")
    assert modeladopt.supersedes("claude-opus-5-5", "claude-opus-5-20260101")


@pytest.mark.parametrize("new,current,expected", [
    ("claude-opus-5-5", "claude-opus-5", True),
    ("claude-opus-6", "claude-opus-5-5", True),
    ("claude-opus-5", "claude-opus-5-5", False),
    ("claude-opus-5-5", "claude-opus-5-5", False),      # equal is not newer
    ("claude-fable-5-1", "claude-opus-5", False),       # a different dropdown entry
    ("claude-opus-5-5", None, True),                    # nothing to beat
    ("gpt-9", None, False),                             # not a family we spawn
    ("claude-opus", None, False),                       # no readable version
])
def test_supersedes(new, current, expected):
    assert modeladopt.supersedes(new, current) is expected


def test_the_newest_of_a_family_wins_the_catalog():
    models = CATALOG + [{"id": "claude-opus-5-5", "display_name": "", "created_at": ""}]
    assert modeladopt.best_in_family(models, "opus") == "claude-opus-5-5"
    assert modeladopt.best_in_family(models, "haiku") is None


def test_only_the_newest_of_a_family_is_a_candidate():
    # Two releases of one family in a single catalog: the older is not adopted
    # on its way past, however new it is to the seen-set.
    models = CATALOG + [
        {"id": "claude-opus-5-5", "display_name": "", "created_at": ""},
        {"id": "claude-opus-6", "display_name": "", "created_at": ""},
    ]
    assert modeladopt.candidate("claude-opus-6", models)
    assert not modeladopt.candidate("claude-opus-5-5", models)


def test_a_family_with_no_pin_does_not_adopt_an_older_sibling():
    # sonnet ships unpinned, so `pins()` has nothing for it and every sonnet id
    # supersedes "nothing". The catalog check is the only thing standing here.
    models = [
        {"id": "claude-sonnet-5", "display_name": "", "created_at": ""},
        {"id": "claude-sonnet-5-2", "display_name": "", "created_at": ""},
    ]
    assert "sonnet" not in modeladopt.pins()
    assert not modeladopt.candidate("claude-sonnet-5", models)
    assert modeladopt.candidate("claude-sonnet-5-2", models)


# --------------------------------------------------------------------------
# Reading the probe
# --------------------------------------------------------------------------

def test_the_400_is_a_failure_even_though_the_cli_exited_zero():
    # THE trap. rc=0 with the refusal in stdout; a probe that read the exit
    # status would have adopted a model that 400s on every run.
    verdict = modeladopt.read_probe(GATED_OUTPUT, 0)
    assert verdict["ok"] is False


def test_the_probe_harvests_the_minimum_cli_version_from_the_refusal():
    assert modeladopt.read_probe(GATED_OUTPUT, 0)["required_cli"] == "2.1.280"


def test_a_plain_answer_is_a_pass():
    assert modeladopt.read_probe("ok\n", 0)["ok"] is True


def test_silence_is_not_a_pass():
    # A silent success and a silent failure look identical, and calling the
    # second one the first is the expensive mistake.
    verdict = modeladopt.read_probe("", 0)
    assert verdict["ok"] is False and verdict["required_cli"] == ""


def test_a_nonzero_exit_with_no_marker_is_still_a_failure():
    assert modeladopt.read_probe("something", 3)["ok"] is False


def test_an_error_with_no_version_in_it_yields_no_version():
    verdict = modeladopt.read_probe("API Error: 500 upstream exploded", 0)
    assert verdict["ok"] is False and verdict["required_cli"] == ""


# --------------------------------------------------------------------------
# Stored state
# --------------------------------------------------------------------------

def test_an_adoption_layers_over_the_shipped_pins_rather_than_replacing_them():
    modeladopt.record("sonnet", "claude-sonnet-9")
    pins = modeladopt.pins()
    assert pins["sonnet"] == "claude-sonnet-9"
    assert pins["fable"] == config.CLI_MODEL_IDS["fable"]   # shipped pin survives


def test_a_corrupt_settings_row_reads_as_empty_not_as_a_crash():
    db.set_setting(modeladopt.PINS_KEY, "{not json")
    assert modeladopt.pin_overrides() == {}
    assert modeladopt.pins() == config.CLI_MODEL_IDS


def test_recording_without_a_version_clears_a_gate_that_no_longer_applies():
    modeladopt.record("opus", "claude-opus-6", "2.2.0")
    assert modeladopt.min_cli()["opus"] == "2.2.0"
    modeladopt.record("opus", "claude-opus-7")
    assert "opus" not in modeladopt.min_cli_overrides()


def test_the_label_follows_the_adoption(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    modeladopt.record("opus", "claude-opus-6", label="Opus 6")
    assert modeladopt.labels()["opus"] == "Opus 6"
    assert dict(config.model_choices())["opus"] == "Opus 6"


@pytest.mark.parametrize("display,expected", [
    ("Claude Opus 5.5", "Opus 5.5"),
    ("Opus 5.5", "Opus 5.5"),
    ("", "claude-opus-5-5"),
])
def test_the_label_drops_the_vendor_prefix(display, expected):
    assert modeladopt.short_label(display, "claude-opus-5-5") == expected


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

def test_a_spawnable_model_is_adopted_outright(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    models = CATALOG + [{"id": "claude-opus-6", "display_name": "Claude Opus 6", "created_at": ""}]
    verdict = modeladopt.adopt(models[-1], models, probe_fn=_ok)

    assert verdict["adopted"] and not verdict["gated"]
    assert modeladopt.pins()["opus"] == "claude-opus-6"
    assert config.cli_model("opus") == "claude-opus-6"
    assert modeladopt.gated() == {}


def test_a_model_this_cli_cannot_spawn_is_still_adopted_but_gated(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "2.1.258", raising=False)
    models = CATALOG + [{"id": "claude-opus-6", "display_name": "Claude Opus 6", "created_at": ""}]
    verdict = modeladopt.adopt(models[-1], models, probe_fn=_gated)

    assert verdict["adopted"] and verdict["gated"]
    assert verdict["required_cli"] == "2.1.280"
    assert modeladopt.pins()["opus"] == "claude-opus-6"
    assert modeladopt.gated() == {"opus": "claude-opus-6"}
    # ...and NOTHING spawns it yet: runs degrade to the bare alias.
    assert config.cli_model("opus") == "opus"


def test_a_probe_that_reached_no_verdict_adopts_nothing(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    models = CATALOG + [{"id": "claude-opus-6", "display_name": "Claude Opus 6", "created_at": ""}]
    verdict = modeladopt.adopt(models[-1], models, probe_fn=_no_verdict)

    assert not verdict["adopted"]
    assert "opus" not in modeladopt.pin_overrides()
    assert config.cli_model("opus") == config.CLI_MODEL_IDS["opus"]
    # ...but it is remembered, so tomorrow tries again.
    assert "claude-opus-6" in modeladopt.pending()


def test_a_model_that_is_not_ours_is_never_probed():
    called = []

    def spy(model_id, **_k):
        called.append(model_id)
        return _ok()

    verdict = modeladopt.adopt({"id": "gpt-9", "display_name": "GPT-9"}, CATALOG, probe_fn=spy)
    assert not verdict["adopted"] and called == []


def test_retrying_a_pending_model_adopts_it_when_the_probe_recovers(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    models = CATALOG + [{"id": "claude-opus-6", "display_name": "Claude Opus 6", "created_at": ""}]
    modeladopt.adopt(models[-1], models, probe_fn=_no_verdict)
    assert modeladopt.pending()

    verdicts = modeladopt.retry_pending(models, probe_fn=_ok)
    assert [v["model_id"] for v in verdicts] == ["claude-opus-6"]
    assert modeladopt.pins()["opus"] == "claude-opus-6"
    assert modeladopt.pending() == {}


def test_a_pending_model_overtaken_by_a_newer_one_is_dropped_not_adopted(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    models = CATALOG + [{"id": "claude-opus-6", "display_name": "Claude Opus 6", "created_at": ""}]
    modeladopt.adopt(models[-1], models, probe_fn=_no_verdict)

    modeladopt.record("opus", "claude-opus-7")
    assert modeladopt.retry_pending(models, probe_fn=_ok) == []
    assert modeladopt.pending() == {}
    assert modeladopt.pins()["opus"] == "claude-opus-7"


# --------------------------------------------------------------------------
# The gate clearing
# --------------------------------------------------------------------------

def test_a_gate_clears_once_the_cli_is_new_enough_and_reports_once(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "2.1.258", raising=False)
    modeladopt.record("opus", "claude-opus-6", "2.1.280", label="Opus 6")
    assert modeladopt.cleared_gates() == []          # still waiting

    monkeypatch.setattr(config, "_cli_version_cache", "2.1.280", raising=False)
    cleared = modeladopt.cleared_gates()
    assert [c["model_id"] for c in cleared] == ["claude-opus-6"]
    assert cleared[0]["label"] == "Opus 6"
    # Consumed, so the "it is live" notification fires exactly once.
    assert modeladopt.cleared_gates() == []


def test_a_gate_superseded_while_it_waited_is_dropped_silently(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "2.1.258", raising=False)
    modeladopt.record("opus", "claude-opus-6", "2.1.280")
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    modeladopt.record("opus", "claude-opus-7")       # newer, spawnable now

    assert modeladopt.cleared_gates() == []
    assert modeladopt.gated() == {}


def test_a_gate_naming_a_model_the_alias_no_longer_points_at_is_not_announced(monkeypatch):
    # The two settings rows can disagree: `pin_overrides()` reads as empty when
    # its row is corrupt (see the corrupt-JSON test above), while GATED_KEY
    # survives intact. The gate must then be dropped, not announced - saying
    # "Opus 6 is live" about a model no alias points at is a notification about
    # nothing, and it would repeat on every check.
    monkeypatch.setattr(config, "_cli_version_cache", "9.9.9", raising=False)
    db.set_setting(modeladopt.GATED_KEY, '{"opus": "claude-opus-6"}')
    db.set_setting(modeladopt.PINS_KEY, "{not json")
    assert modeladopt.pins()["opus"] != "claude-opus-6"

    assert modeladopt.cleared_gates() == []
    assert modeladopt.gated() == {}


def test_recording_a_gated_pin_on_a_cli_that_already_meets_it_is_not_gated(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "3.0.0", raising=False)
    modeladopt.record("opus", "claude-opus-6", "2.1.280")
    assert modeladopt.gated() == {}
    assert config.cli_model("opus") == "claude-opus-6"


# --------------------------------------------------------------------------
# The spawn boundary
# --------------------------------------------------------------------------

def test_the_shipped_opus_pin_is_the_model_wes_adopted_on_2026_09_23():
    assert config.CLI_MODEL_IDS["opus"] == "claude-opus-5-5"
    assert config.MODEL_MIN_CLI["opus"] == "2.1.280"


def test_the_shipped_pin_degrades_on_the_cli_that_refused_it(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "2.1.258", raising=False)
    assert config.cli_model("opus") == "opus"


def test_the_shipped_pin_takes_effect_once_the_cli_is_updated(monkeypatch):
    monkeypatch.setattr(config, "_cli_version_cache", "2.1.280", raising=False)
    assert config.cli_model("opus") == "claude-opus-5-5"
