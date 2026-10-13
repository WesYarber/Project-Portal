"""Files a journal entry names are links to them.

Wes, 2026-10-13: "I want to allow the journal, when it references files, to
hyperlink to them where I can click the link to open or download the file."

Both shapes an agent writes a file reference in resolve to the file page
(`/file/<slug>/<path>`), which renders the file and carries its download link:
a markdown link with a workspace-relative target, and a path in a code span -
the latter only when the file really exists in the workspace, since entries
name files outside it (source code, other machines) all the time.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, mediamd


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def _workspace(slug="manabase", files=("PLAN.md", "attachments/0154-SPEC.md")):
    project = db.create_project("Manabase", description="x", slug=slug)
    ws = config.PROJECTS_DIR / slug
    for name in files:
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text("hello", encoding="utf-8")
    return project


def _exists(*names):
    return lambda path: path in names


# --- the resolver ----------------------------------------------------------

def test_a_code_span_naming_a_real_file_becomes_a_link():
    html = "<p>see <code>attachments/SPEC.md</code></p>"
    out = mediamd.resolve_links(html, "/file/p", _exists("attachments/SPEC.md"))
    assert (
        '<a class="journal-file" href="/file/p/attachments/SPEC.md">'
        "<code>attachments/SPEC.md</code></a>"
    ) in out


def test_a_code_span_naming_a_missing_file_stays_text():
    html = "<p><code>app/config.py</code></p>"
    assert mediamd.resolve_links(html, "/file/p", _exists()) == html


def test_a_line_reference_links_to_the_file_and_keeps_its_text():
    html = "<p><code>PLAN.md:12</code></p>"
    out = mediamd.resolve_links(html, "/file/p", _exists("PLAN.md"))
    assert 'href="/file/p/PLAN.md"><code>PLAN.md:12</code></a>' in out


@pytest.mark.parametrize(
    "text",
    [
        "pytest -q",          # a command, not a path
        "../secrets/key",     # climbing out
        "VACUUM",             # a bare word: no separator, no extension
        "/etc/passwd",        # absolute
        "a &amp; b",          # escaped markup
    ],
)
def test_text_that_is_not_a_workspace_path_is_never_looked_up(text):
    asked = []
    html = f"<p><code>{text}</code></p>"
    out = mediamd.resolve_links(html, "/file/p", lambda p: asked.append(p) or True)
    assert out == html
    assert asked == []


def test_a_fenced_block_is_left_alone():
    html = "<pre><code>PLAN.md</code></pre>"
    assert mediamd.resolve_links(html, "/file/p", _exists("PLAN.md")) == html


def test_a_span_the_agent_already_linked_is_not_wrapped_twice():
    html = '<p><a href="https://x.test/"><code>PLAN.md</code></a></p>'
    assert mediamd.resolve_links(html, "/file/p", _exists("PLAN.md")) == html


def test_a_relative_markdown_link_resolves_to_the_file_page():
    html = '<p><a href="./notes/the plan.md">the plan</a></p>'
    out = mediamd.resolve_links(html, "/file/p")
    assert 'href="/file/p/notes/the%20plan.md"' in out


@pytest.mark.parametrize(
    "href", ["https://x.test/a", "/project/x", "#top", "mailto:a@b.test", "/raw/p/a.png"]
)
def test_absolute_links_are_untouched(href):
    html = f'<p><a href="{href}">x</a></p>'
    assert mediamd.resolve_links(html, "/file/p", _exists()) == html


def test_no_base_means_no_rewriting():
    html = '<p><a href="PLAN.md">x</a><code>PLAN.md</code></p>'
    assert mediamd.resolve_links(html, None, _exists("PLAN.md")) == html


# --- the pages ---------------------------------------------------------------

def test_the_project_journal_links_real_files_only(client):
    project = _workspace()
    db.add_journal(
        project["id"], "agent", "progress",
        "Read `attachments/0154-SPEC.md` and changed `app/config.py`; "
        "the [plan](PLAN.md) is updated.",
    )
    page = client.get("/project/manabase").text
    assert 'href="/file/manabase/attachments/0154-SPEC.md"><code>' in page
    assert 'href="/file/manabase/PLAN.md">plan</a>' in page
    assert "/file/manabase/app/config.py" not in page
    # And the link lands on a page that opens the file and offers it whole.
    view = client.get("/file/manabase/attachments/0154-SPEC.md")
    assert view.status_code == 200
    assert "/download/manabase/attachments/0154-SPEC.md" in view.text


def test_a_directory_is_not_linked_as_a_file(client):
    project = _workspace(files=("shots/v1.2/a.png",))
    db.add_journal(project["id"], "agent", "progress", "in `shots/v1.2`, see `shots/`")
    page = client.get("/project/manabase").text
    assert "<code>shots/v1.2</code></a>" not in page


def test_a_symlink_out_of_the_workspace_is_not_linked(client, tmp_path):
    project = _workspace(files=())
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    ws = config.PROJECTS_DIR / "manabase"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "leak.txt").symlink_to(outside)
    db.add_journal(project["id"], "agent", "progress", "see `leak.txt`")
    page = client.get("/project/manabase").text
    assert "<code>leak.txt</code></a>" not in page


def test_the_dashboard_feed_links_against_the_entry_project(client):
    project = _workspace()
    db.add_journal(project["id"], "agent", "progress", "see `PLAN.md`")
    feed = client.get("/activity/feed").text
    assert 'href="/file/manabase/PLAN.md"><code>PLAN.md</code></a>' in feed


def test_a_oneoff_reply_does_not_get_project_file_links(client):
    task = db.create_oneoff("check the thing")
    db.add_oneoff_message(task["id"], "agent", "see `PLAN.md` and [x](a.md)")
    page = client.get(f"/tasks/{task['id']}").text
    assert "journal-file" not in page
    assert "/file/" not in page.split("see")[-1].split("</div>")[0]
