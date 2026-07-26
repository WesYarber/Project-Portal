"""Per-project agent override and the global default."""
from __future__ import annotations

from app import agent_runner, config, db


def test_default_model_is_opus():
    assert config.DEFAULT_MODEL == "opus"
    assert db.get_setting("worker_model") == "opus"


def test_project_with_no_override_inherits_global():
    project = db.create_project("Thing")
    db.set_setting("worker_model", "sonnet")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "sonnet"


def test_project_override_wins():
    project = db.create_project("Thing")
    db.set_setting("worker_model", "sonnet")
    db.update_project(project["id"], model="haiku")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "haiku"


def test_clearing_override_falls_back_to_global():
    project = db.create_project("Thing")
    db.update_project(project["id"], model="haiku")
    db.update_project(project["id"], model=None)
    db.set_setting("worker_model", "sonnet")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "sonnet"


def test_unknown_override_is_ignored():
    project = db.create_project("Thing")
    db.update_project(project["id"], model="gpt-9")
    db.set_setting("worker_model", "sonnet")
    assert agent_runner.resolve_model(db.get_project(project["id"])) == "sonnet"


def test_unknown_global_falls_back_to_default():
    db.set_setting("worker_model", "banana")
    assert agent_runner.resolve_model(None) == config.DEFAULT_MODEL


def test_reflect_run_uses_global_model():
    db.set_setting("worker_model", "haiku")
    assert agent_runner.resolve_model(None) == "haiku"
