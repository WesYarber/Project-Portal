"""Getting at a project from the browser: downloading its files, and the
one-click ssh command that drops you into its directory.

Wes picked option (b) for the terminal - a copyable `ssh ... -t 'cd ...'` line -
over an ssh:// handler or an embedded web shell, so there is no server-side
shell here to test; the command string and the download route are the feature.

Also covers the note form's 14:35 fixes: Enter must not submit on a phone, the
add-note button is the green affirmative one, and the upload limit is only
mentioned when a file actually exceeds it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import attachments, config, db, main


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project("Dice Tower", "A thing.", stage="active", build_approved=True, slug="dice-tower")
    workspace = config.PROJECTS_DIR / "dice-tower"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "PLAN.md").write_text("# Plan\nstep one\n", encoding="utf-8")
    (workspace / "render.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return row


# --- downloads -------------------------------------------------------------

def test_a_text_file_downloads_with_its_bytes(client, project):
    resp = client.get("/download/dice-tower/PLAN.md")
    assert resp.status_code == 200
    assert resp.text == "# Plan\nstep one\n"
    assert "attachment" in resp.headers["content-disposition"]
    assert "PLAN.md" in resp.headers["content-disposition"]


def test_a_binary_file_still_downloads_as_an_attachment(client, project):
    """The viewer now *shows* a PNG (see test_fileview), but download is still
    the way to get the bytes - and it is still attachment-only."""
    resp = client.get("/download/dice-tower/render.png")
    assert resp.status_code == 200
    assert resp.content.startswith(b"\x89PNG")
    # Never inline: workspace files are written by agents, and serving one
    # inline would be script execution on the portal's own origin.
    assert resp.headers["content-type"] == "application/octet-stream"


def test_download_cannot_escape_the_workspace(client, project):
    secret = config.DATA_DIR / "portal.db"
    assert secret.exists()
    for path in ("../portal.db", "../../etc/passwd", "..%2Fportal.db"):
        resp = client.get(f"/download/dice-tower/{path}")
        assert resp.status_code in (400, 404), path


def test_download_of_a_missing_file_is_a_404(client, project):
    assert client.get("/download/dice-tower/nope.txt").status_code == 404
    assert client.get("/download/no-such-project/PLAN.md").status_code == 404


def test_every_workspace_file_has_a_download_link(client, project):
    body = client.get("/project/dice-tower").text
    assert '/download/dice-tower/PLAN.md' in body
    assert '/download/dice-tower/render.png' in body


# --- the ssh command -------------------------------------------------------

def test_the_ssh_command_lands_in_the_project_directory(temp_data_dir):
    command = config.ssh_command("dice-tower")
    # The user@host half is this installation's own identity, so it is read
    # from the site config rather than pinned to whatever box runs the suite.
    assert command.startswith(f"ssh {config.SITE.handle} -t ")
    assert str(config.PROJECTS_DIR / "dice-tower") in command
    # -t for a TTY, exec so it's a login shell rather than a nested one.
    assert "exec bash -l" in command


def test_the_ssh_command_is_on_the_page_with_a_copy_button(client, project):
    body = client.get("/project/dice-tower").text
    assert f"ssh {config.SITE.handle}" in body
    assert 'data-copy="#ssh-command"' in body


def test_a_renamed_project_gets_the_new_path(client, project):
    client.post(
        "/project/dice-tower/details",
        data={"title": "Dice Tower", "description": "A thing.", "new_slug": "tower"},
    )
    assert str(config.PROJECTS_DIR / "tower") in config.ssh_command("tower")
    assert "/tower" in client.get("/project/tower").text


# --- the note form (14:35) -------------------------------------------------

def test_the_add_note_button_is_the_green_affirmative_one(client, project):
    body = client.get("/project/dice-tower").text
    assert '<button type="submit" class="btn go">add note</button>' in body
    css = (config.BASE_DIR / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert "button.go, .btn.go" in css
    assert "--ansi-green" in css.split("button.go, .btn.go")[1][:200]


def test_the_upload_limit_is_not_written_on_the_page(client, project):
    """It shows up when a file is actually too big, not before."""
    body = client.get("/project/dice-tower").text
    assert "Images, audio, video or any file" not in body
    limit = attachments.human_size(attachments.MAX_UPLOAD_BYTES)
    assert f"Up to {limit} each" not in body
    # ...but the size the browser checks against is still handed to it.
    assert f'data-max-bytes="{attachments.MAX_UPLOAD_BYTES}"' in body
    assert f'data-max-label="{limit}"' in body


def test_the_oversize_message_names_the_limit(temp_data_dir):
    js = (config.BASE_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "too big (max " in js
    assert "oversizeWarning" in js


def test_enter_only_submits_with_a_hardware_keyboard(temp_data_dir):
    """iOS turns its own shift key on for auto-capitalisation, so Return on a
    phone arrived as shiftKey=true and submitted the note mid-sentence."""
    js = (config.BASE_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "(pointer: fine)" in js
    assert "ev.shiftKey && hasHardwareKeyboard()" in js


def test_the_note_box_asks_for_a_return_key(client, project):
    assert 'enterkeyhint="enter"' in client.get("/project/dice-tower").text


def test_a_note_with_newlines_survives_the_round_trip(client, project):
    client.post(
        "/project/dice-tower/note",
        data={"note": "line one\nline two\n\nline four"},
    )
    entries = [row["content_md"] for row in db.list_journal(project["id"])]
    assert "line one\nline two\n\nline four" in entries
