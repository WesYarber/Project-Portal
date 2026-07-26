"""Agents can show media in their replies, not just describe it.

Wes, 2026-07-23 03:54: "Make it where agents can and do use/include images,
videos, gifs in their replies when relevant."

The mechanism: a journal entry (or one-off reply) references media by
workspace-relative path in ordinary markdown image syntax; the markdown_media
filter resolves that against the inline-serving raw route, turns video/audio
references into players, and links images to their full-size selves. Grouped
by claim: the resolver, the rendered pages, the one-off raw route, and the
contract text that makes agents actually do it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import agent_runner, config, db, mediamd, oneoff

# A real 1x1 PNG, for routes that serve actual bytes.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _project(slug="manabase"):
    return db.create_project("Manabase", description="A life counter", slug=slug)


# --------------------------------------------------------------------------
# resolve_src: which srcs are rewritten, and to what
# --------------------------------------------------------------------------

def test_relative_src_roots_at_raw_base():
    assert mediamd.resolve_src("shots/a.png", "/raw/manabase") == "/raw/manabase/shots/a.png"


def test_dot_slash_prefix_is_stripped():
    assert mediamd.resolve_src("./shots/a.png", "/raw/manabase") == "/raw/manabase/shots/a.png"


def test_segments_are_percent_quoted():
    assert (
        mediamd.resolve_src("my shots/a b.png", "/raw/manabase")
        == "/raw/manabase/my%20shots/a%20b.png"
    )


@pytest.mark.parametrize(
    "src",
    [
        "https://example.com/a.png",
        "http://testhost:8501/x/",
        "/attachment/4",
        "/raw/other/a.png",
        "data:image/png;base64,AAAA",
        "#fragment",
    ],
)
def test_absolute_and_rooted_srcs_pass_through(src):
    assert mediamd.resolve_src(src, "/raw/manabase") == src


def test_parent_traversal_is_left_alone_not_dressed_up():
    # The /raw route refuses ../ anyway; rewriting it would only make the
    # refusal look like a portal URL that ought to have worked.
    assert mediamd.resolve_src("../other/a.png", "/raw/manabase") == "../other/a.png"


def test_no_base_means_no_rewriting():
    assert mediamd.resolve_src("shots/a.png", None) == "shots/a.png"


# --------------------------------------------------------------------------
# resolve_media: what the rendered entry becomes
# --------------------------------------------------------------------------

def test_image_resolves_and_links_to_itself():
    html = '<p><img alt="the layout" src="shots/dash.png" /></p>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert 'src="/raw/manabase/shots/dash.png"' in out
    assert 'href="/raw/manabase/shots/dash.png"' in out
    assert 'alt="the layout"' in out
    assert 'loading="lazy"' in out
    assert 'class="journal-media"' in out


def test_already_linked_image_is_not_double_wrapped():
    # [![alt](img)](target) renders as <a><img></a>; nesting anchors is
    # invalid HTML and breaks the outer link.
    html = '<p><a href="https://example.com"><img alt="x" src="a.png" /></a></p>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert out.count("<a ") == 1
    assert 'src="/raw/manabase/a.png"' in out


def test_video_reference_becomes_a_player():
    html = '<p><img alt="demo" src="demo.mp4" /></p>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert "<video" in out and "<img" not in out
    assert "controls" in out
    assert 'src="/raw/manabase/demo.mp4"' in out


def test_audio_reference_becomes_a_player():
    out = mediamd.resolve_media('<img alt="" src="take.mp3" />', "/raw/manabase")
    assert "<audio" in out and "controls" in out


def test_gif_stays_an_image():
    out = mediamd.resolve_media('<img alt="" src="loop.gif" />', "/raw/manabase")
    assert "<img" in out and "<video" not in out


def test_raw_video_tag_gets_src_resolved_but_keeps_its_element():
    html = '<video controls src="clip.webm"></video>'
    out = mediamd.resolve_media(html, "/raw/manabase")
    assert '<video controls src="/raw/manabase/clip.webm">' in out


def test_external_image_untouched_but_still_capped_and_linked():
    out = mediamd.resolve_media('<img alt="" src="https://e.com/a.png" />', "/raw/manabase")
    assert 'src="https://e.com/a.png"' in out
    assert 'class="journal-media"' in out


def test_text_without_media_is_unchanged():
    html = "<p>plain <strong>text</strong> only</p>"
    assert mediamd.resolve_media(html, "/raw/manabase") == html


# --------------------------------------------------------------------------
# The pages: journal entries render with resolved media
# --------------------------------------------------------------------------

def test_project_journal_resolves_relative_image(client):
    project = _project()
    db.add_journal(project["id"], "agent", "progress", "Look:\n\n![shot](shots/dash.png)")
    page = client.get("/project/manabase").text
    assert 'src="/raw/manabase/shots/dash.png"' in page


def test_dashboard_activity_resolves_against_the_entry_project(client):
    project = _project()
    db.add_journal(project["id"], "agent", "progress", "![shot](dash.png)")
    page = client.get("/").text
    assert 'src="/raw/manabase/dash.png"' in page


def test_oneoff_reply_resolves_against_the_task_workspace(client):
    task = db.create_oneoff("check the thing")
    db.add_oneoff_message(task["id"], "agent", "Here:\n\n![proof](proof.png)")
    page = client.get(f"/tasks/{task['id']}").text
    assert f'src="/tasks/{task["id"]}/raw/proof.png"' in page


# --------------------------------------------------------------------------
# The one-off raw route: serves, refuses, contains
# --------------------------------------------------------------------------

def test_oneoff_raw_serves_a_workspace_image(client):
    task = db.create_oneoff("check the thing")
    workspace = oneoff.workspace(task["id"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "proof.png").write_bytes(PNG)
    resp = client.get(f"/tasks/{task['id']}/raw/proof.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == PNG


def test_oneoff_raw_refuses_non_media(client):
    task = db.create_oneoff("check the thing")
    workspace = oneoff.workspace(task["id"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "script.sh").write_text("echo hi")
    assert client.get(f"/tasks/{task['id']}/raw/script.sh").status_code == 415


def test_oneoff_raw_refuses_escape(client):
    task = db.create_oneoff("check the thing")
    workspace = oneoff.workspace(task["id"])
    workspace.mkdir(parents=True, exist_ok=True)
    (config.TASKS_DIR / "secret.png").write_bytes(PNG)
    resp = client.get(f"/tasks/{task['id']}/raw/../secret.png")
    assert resp.status_code in (400, 404)


def test_oneoff_raw_unknown_task_404s(client):
    assert client.get("/tasks/999/raw/a.png").status_code == 404


# --------------------------------------------------------------------------
# The contract: agents are told the syntax exists
# --------------------------------------------------------------------------

def test_agent_contract_teaches_media_embeds():
    assert "![" in agent_runner.AGENT_CONTRACT
    assert "workspace-relative path" in agent_runner.AGENT_CONTRACT


def test_oneoff_contract_teaches_media_embeds():
    assert "![" in oneoff.ONEOFF_CONTRACT
