"""Reading a run's diff, and turning one of its lines into the next prompt.

The load-bearing tests here are the ones about *where the quoted text comes
from*: the whole point of app/rundiff.py pulling the line out of git rather
than out of the form is that the journal is what a later run believes, so a
comment must never be able to claim an agent wrote a line it did not.
"""
from __future__ import annotations

import subprocess

import pytest
from starlette.testclient import TestClient

from app import config, db, revert, rundiff


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check
    )


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def project(temp_data_dir):
    row = db.create_project(
        "Widget", description="x", stage="active", build_approved=True, slug="widget"
    )
    ws = config.PROJECTS_DIR / "widget"
    ws.mkdir(parents=True, exist_ok=True)
    git(ws, "init", "-q", "-b", "main")
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "T")
    (ws / "app.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "base")
    return row


@pytest.fixture
def ws(project):
    return config.PROJECTS_DIR / "widget"


def a_run(project, ws, *, write: dict, message="agent work", status="ok", remove=()):
    before = head(ws)
    run_id = db.create_run(project["id"], "build", "opus")
    for name, text in write.items():
        target = ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text)
    for name in remove:
        (ws / name).unlink()
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", message)
    db.finish_run(run_id, status)
    db.set_run_workspace_heads(run_id, before, head(ws))
    return run_id


def diff_for(run_id):
    row = db.get_run_with_project(run_id)
    return rundiff.for_run(revert.landed(row))


# --- reading the diff ------------------------------------------------------


def test_the_diff_names_the_file_and_counts_both_sides(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\nsix\n"})
    diff = diff_for(run_id)
    assert [f.path for f in diff.files] == ["app.py"]
    assert (diff.insertions, diff.deletions) == (2, 1)


def test_added_and_removed_lines_carry_the_right_line_numbers(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    f = diff_for(run_id).files[0]
    added = [line for line in f.lines if line.kind == "add"]
    removed = [line for line in f.lines if line.kind == "del"]
    assert [(line.text, line.new) for line in added] == [("TWO", 2)]
    # A deleted line has no new-side number at all - it is not in the file any
    # more, and numbering it as if it were would point a comment at the line
    # that replaced it.
    assert [(line.text, line.old, line.new) for line in removed] == [("two", 2, None)]


def test_a_hunk_header_says_where_in_the_file_and_cannot_be_commented_on(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    f = diff_for(run_id).files[0]
    hunks = [line for line in f.lines if line.kind == "hunk"]
    assert hunks and hunks[0].text.startswith("line 1")
    # The raw `@@ -1,5 +1,5 @@` is deliberately not kept: the portal's terminal
    # font has no glyph for `@`, so it renders as two tofu boxes.
    assert "@@" not in hunks[0].text
    assert not hunks[0].commentable


def test_a_hunk_header_keeps_the_section_git_named(project, ws):
    body = "def thing():\n" + "".join(f"    step{i}()\n" for i in range(12))
    (ws / "deep.py").write_text(body)
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "add deep")
    changed = body.replace("    step9()\n", "    step9(fast=True)\n")
    run_id = a_run(project, ws, write={"deep.py": changed})
    f = next(f for f in diff_for(run_id).files if f.path == "deep.py")
    hunk = next(line for line in f.lines if line.kind == "hunk")
    assert "in def thing():" in hunk.text


def test_a_new_file_is_all_additions(project, ws):
    run_id = a_run(project, ws, write={"new.py": "fresh\nlines\n"})
    diff = diff_for(run_id)
    f = next(f for f in diff.files if f.path == "new.py")
    assert [line.text for line in f.lines if line.kind == "add"] == ["fresh", "lines"]
    assert not [line for line in f.lines if line.kind == "del"]


def test_a_deleted_file_is_all_removals(project, ws):
    run_id = a_run(project, ws, write={}, remove=["app.py"])
    f = diff_for(run_id).files[0]
    assert f.path == "app.py"
    assert [line.kind for line in f.lines if line.commentable] == ["del"] * 5


def test_a_binary_file_is_listed_with_no_lines_to_comment_on(project, ws):
    run_id = a_run(project, ws, write={"logo.png": b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"})
    f = next(f for f in diff_for(run_id).files if f.path == "logo.png")
    assert f.binary
    assert f.lines == []


def test_a_run_that_committed_nothing_has_no_diff(project, ws):
    run_id = db.create_run(project["id"], "build", "opus")
    db.finish_run(run_id, "ok")
    db.set_run_workspace_heads(run_id, head(ws), head(ws))
    assert diff_for(run_id) is None


def test_a_rename_names_both_paths(project, ws):
    git(ws, "mv", "app.py", "renamed.py")
    before = head(ws)
    run_id = db.create_run(project["id"], "build", "opus")
    git(ws, "commit", "-qm", "move it")
    db.finish_run(run_id, "ok")
    db.set_run_workspace_heads(run_id, before, head(ws))
    f = diff_for(run_id).files[0]
    assert f.path == "renamed.py"
    assert f.renamed and f.old_path == "app.py"


# --- the caps, which are decided before the diff is fetched ----------------


def test_a_huge_diff_is_listed_but_not_rendered(project, ws, monkeypatch):
    monkeypatch.setattr(rundiff, "MAX_TOTAL_LINES", 10)
    run_id = a_run(project, ws, write={"app.py": "\n".join(f"line {i}" for i in range(60))})
    diff = diff_for(run_id)
    assert diff.too_big
    assert [f.path for f in diff.files] == ["app.py"]
    assert diff.files[0].lines == []
    assert "more than this page will render" in diff.note


def test_a_long_file_is_truncated_and_says_by_how_much(project, ws, monkeypatch):
    monkeypatch.setattr(rundiff, "MAX_LINES_PER_FILE", 5)
    run_id = a_run(project, ws, write={"app.py": "\n".join(f"line {i}" for i in range(40)) + "\n"})
    f = diff_for(run_id).files[0]
    assert len(f.lines) == 5
    assert f.truncated > 0


def test_more_files_than_the_cap_are_counted_rather_than_dropped(project, ws, monkeypatch):
    monkeypatch.setattr(rundiff, "MAX_FILES", 2)
    run_id = a_run(project, ws, write={f"f{i}.py": f"x{i}\n" for i in range(5)})
    diff = diff_for(run_id)
    assert len(diff.files) == 2
    assert diff.unlisted == 3
    # The header must still say five, or a reviewer reads two files and
    # believes that is the whole change.
    assert diff.changed_files == 5


def test_the_body_is_never_fetched_for_a_diff_over_the_cap(project, ws, monkeypatch):
    """The cap is an ordering guarantee, not a truncation: a run that committed
    a vendored library must not be read into memory at all."""
    monkeypatch.setattr(rundiff, "MAX_TOTAL_LINES", 10)
    calls = []
    real = rundiff._git

    def spy(repo, *args):
        calls.append(args)
        return real(repo, *args)

    monkeypatch.setattr(rundiff, "_git", spy)
    run_id = a_run(project, ws, write={"app.py": "\n".join(f"line {i}" for i in range(60))})
    diff_for(run_id)
    assert calls, "no git call was made at all"
    assert all("--numstat" in args for args in calls), calls


# --- the quote, which comes from git and not from the form -----------------


def test_the_quote_names_the_file_and_line_and_fences_the_code(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    f = diff_for(run_id).files[0]
    line = next(line for line in f.lines if line.kind == "add")
    quote = rundiff.quote_for(f, line)
    assert "`app.py:2`" in quote
    assert "```\n+TWO\n```" in quote


def test_a_removed_line_is_quoted_by_its_old_number_and_says_so(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nthree\nfour\nfive\n"})
    f = diff_for(run_id).files[0]
    line = next(line for line in f.lines if line.kind == "del")
    assert "`app.py:2 (removed)`" in rundiff.quote_for(f, line)


def test_line_at_refuses_a_path_that_is_not_in_this_diff(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    assert rundiff.line_at(diff, "somewhere/else.py", 1) is None


def test_line_at_refuses_an_index_past_the_end(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    assert rundiff.line_at(diff, "app.py", 9999) is None
    assert rundiff.line_at(diff, "app.py", -1) is None


def test_line_at_refuses_a_hunk_header(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    f = diff.files[0]
    hunk_index = next(i for i, line in enumerate(f.lines) if line.kind == "hunk")
    assert rundiff.line_at(diff, "app.py", hunk_index) is None


def test_the_note_puts_the_code_above_the_comment(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    f = diff_for(run_id).files[0]
    line = next(line for line in f.lines if line.kind == "add")
    body = rundiff.note_body(run_id, f, line, "shouting is not a fix")
    assert body.index("+TWO") < body.index("shouting is not a fix")
    assert f"run #{run_id}" in body


def test_a_pasted_essay_is_cut_to_the_comment_budget(project, ws):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    f = diff_for(run_id).files[0]
    line = next(line for line in f.lines if line.kind == "add")
    body = rundiff.note_body(run_id, f, line, "x" * 9000)
    assert "x" * rundiff.MAX_COMMENT_CHARS in body
    assert "x" * (rundiff.MAX_COMMENT_CHARS + 1) not in body


# --- the page and the round trip -------------------------------------------


def test_the_run_page_shows_the_diff(project, ws, client):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    html = client.get(f"/run/{run_id}").text
    assert 'id="rundiff"' in html
    assert "app.py" in html
    assert "+TWO" in html or ">TWO<" in html


def test_every_commentable_line_offers_a_radio_and_hunks_do_not(project, ws, client):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    html = client.get(f"/run/{run_id}").text
    diff = diff_for(run_id)
    commentable = sum(1 for line in diff.files[0].lines if line.commentable)
    assert html.count('name="index"') == commentable


def test_commenting_on_a_line_files_a_note_quoting_that_line(project, ws, client):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    index = next(i for i, line in enumerate(diff.files[0].lines) if line.kind == "add")

    r = client.post(
        f"/run/{run_id}/comment",
        data={"path": "app.py", "index": str(index), "comment": "keep it lowercase"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/run/{run_id}?filed=1")

    notes = [j for j in db.list_journal(project["id"]) if j["kind"] == "note"]
    assert len(notes) == 1
    body = notes[0]["content_md"]
    assert "`app.py:2`" in body
    assert "+TWO" in body
    assert "keep it lowercase" in body


def test_the_quoted_line_comes_from_git_not_from_the_form(project, ws, client):
    """A forged `line`/`quote` field must not be able to put words in the
    agent's mouth: the route takes only an address, and reads the text itself."""
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    index = next(i for i, line in enumerate(diff.files[0].lines) if line.kind == "add")

    client.post(
        f"/run/{run_id}/comment",
        data={
            "path": "app.py",
            "index": str(index),
            "comment": "look at this",
            "quote": "os.system('rm -rf /')",
            "line": "os.system('rm -rf /')",
            "text": "os.system('rm -rf /')",
        },
        follow_redirects=False,
    )
    body = [j for j in db.list_journal(project["id"]) if j["kind"] == "note"][0]["content_md"]
    assert "rm -rf" not in body
    assert "+TWO" in body


def test_a_comment_with_no_line_picked_is_refused_in_a_sentence(project, ws, client):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    r = client.post(
        f"/run/{run_id}/comment",
        data={"path": "app.py", "index": "", "comment": "this bit"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Pick a line first" in r.text
    assert not [j for j in db.list_journal(project["id"]) if j["kind"] == "note"]


def test_a_comment_on_a_file_not_in_the_diff_is_refused(project, ws, client):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    r = client.post(
        f"/run/{run_id}/comment",
        data={"path": "/etc/passwd", "index": "1", "comment": "hm"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Pick a line first" in r.text
    assert not [j for j in db.list_journal(project["id"]) if j["kind"] == "note"]


def test_a_comment_wakes_a_project_left_in_review(project, ws, client):
    """Wes's rule: a note on a project he had put down brings it back. A diff
    comment is a note, so it has to behave like one - and 'in review' is the
    exact state a project is in when somebody is reading its diff."""
    db.set_user_state(project, "review")
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    index = next(i for i, line in enumerate(diff.files[0].lines) if line.kind == "add")
    client.post(
        f"/run/{run_id}/comment",
        data={"path": "app.py", "index": str(index), "comment": "fix this"},
        follow_redirects=False,
    )
    assert db.display_state(db.get_project(project["id"])) == "active"


def test_a_comment_with_no_words_still_files_the_line(project, ws, client):
    """Sending a line with an empty box is a legitimate gesture - 'look at
    this' - and it must not post an empty note that says nothing."""
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    diff = diff_for(run_id)
    index = next(i for i, line in enumerate(diff.files[0].lines) if line.kind == "add")
    client.post(
        f"/run/{run_id}/comment",
        data={"path": "app.py", "index": str(index), "comment": "   "},
        follow_redirects=False,
    )
    body = [j for j in db.list_journal(project["id"]) if j["kind"] == "note"][0]["content_md"]
    assert "+TWO" in body


def test_the_confirmation_only_shows_after_the_redirect(project, ws, client):
    run_id = a_run(project, ws, write={"app.py": "one\nTWO\nthree\nfour\nfive\n"})
    assert "Filed on" not in client.get(f"/run/{run_id}").text
    assert "Filed on" in client.get(f"/run/{run_id}?filed=1").text
