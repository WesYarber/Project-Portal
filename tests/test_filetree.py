"""The workspace file tree: one directory at a time, folders shut by default."""
from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from app import config, db, filetree, main


@pytest.fixture()
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture()
def project(client):
    return db.create_project("Tree Test", "a project with a tree")


@pytest.fixture()
def workspace(project):
    ws = config.PROJECTS_DIR / project["slug"]
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# hi", encoding="utf-8")
    (ws / "bun.lock").write_text("lock", encoding="utf-8")
    (ws / "src").mkdir()
    (ws / "src" / "app.js").write_text("let x = 1;", encoding="utf-8")
    (ws / "src" / "deep").mkdir()
    (ws / "src" / "deep" / "buried.txt").write_text("x", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    return ws


# --- children() -------------------------------------------------------------


def test_only_the_named_directory_is_listed(workspace):
    """The whole point: nothing recurses, so a huge subtree costs nothing."""
    names = [e.name for e in filetree.children(workspace)]
    # Directory first, then files case-insensitively: bun.lock before README.md.
    assert names == ["src", "bun.lock", "README.md"]
    assert "app.js" not in names
    assert "buried.txt" not in names


def test_directories_sort_above_files_then_alphabetically(workspace):
    (workspace / "zzz").mkdir()
    (workspace / "AAA.md").write_text("x", encoding="utf-8")
    entries = filetree.children(workspace)
    assert [e.is_dir for e in entries] == [True, True, False, False, False]
    assert [e.name for e in entries][:2] == ["src", "zzz"]
    # Case-insensitive, or README.md would sort above bun.lock on capital R
    # rather than on name.
    assert [e.name for e in entries][2:] == ["AAA.md", "bun.lock", "README.md"]


def test_a_child_path_is_relative_to_the_workspace_root(workspace):
    deep = filetree.children(workspace, "src/deep")
    assert [e.path for e in deep] == ["src/deep/buried.txt"]
    # POSIX separators, because the path goes straight into a URL.
    assert "\\" not in deep[0].path


def test_the_git_directory_is_never_listed(workspace):
    assert ".git" not in {e.name for e in filetree.children(workspace)}


def test_a_missing_or_unreadable_directory_is_empty_not_an_error(workspace):
    assert filetree.children(workspace, "nope") == []
    assert filetree.children(workspace / "gone-entirely") == []
    # A file is not a directory: asking for its children is empty, not a crash.
    assert filetree.children(workspace, "README.md") == []


def test_a_symlinked_directory_is_listed_as_a_file(workspace, tmp_path):
    """A symlink out of the workspace must not be an openable folder.

    Listing it as a file means clicking it hits the file routes, which resolve()
    before their containment check and reject it - rather than the tree walking
    the reader straight out of the workspace.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    os.symlink(outside, workspace / "escape")

    entry = next(e for e in filetree.children(workspace) if e.name == "escape")
    assert entry.is_dir is False


# --- count_files() ----------------------------------------------------------


def test_the_count_walks_everything_but_prunes_what_the_tree_hides(workspace):
    count, capped = filetree.count_files(workspace)
    # README.md, bun.lock, src/app.js, src/deep/buried.txt - and NOT .git/HEAD.
    assert (count, capped) == (4, False)


def test_the_count_stops_at_the_cap(workspace):
    for i in range(30):
        (workspace / f"f{i}.txt").write_text("x", encoding="utf-8")
    count, capped = filetree.count_files(workspace, cap=10)
    assert capped is True
    assert count == 10


def test_a_missing_workspace_counts_zero(project):
    assert filetree.count_files(config.PROJECTS_DIR / "nothing-here") == (0, False)


# --- the /tree route --------------------------------------------------------


def test_the_route_returns_just_the_children_of_that_directory(client, project, workspace):
    body = client.get(f"/tree/{project['slug']}/src").text
    assert "app.js" in body
    assert "deep" in body
    # Not the root's files: this is one level, not a whole tree.
    assert "bun.lock" not in body


def test_the_route_serves_the_root_with_no_path(client, project, workspace):
    body = client.get(f"/tree/{project['slug']}").text
    assert "README.md" in body
    assert "src" in body


def test_the_route_cannot_escape_the_workspace(client, project, workspace):
    """Asserted against the resolver, not over HTTP.

    An `http://.../tree/x/../..` is collapsed by the *client* before it is
    sent, so a request-level test proves the client normalizes paths and
    nothing about this code. The containment check is what has to hold.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        main._workspace_dir(project["slug"], "../../..")
    assert caught.value.status_code == 400

    # And percent-encoded, which does survive the client and reach the route.
    assert client.get(f"/tree/{project['slug']}/..%2f..").status_code in (400, 404)


def test_the_route_refuses_a_file(client, project, workspace):
    """Or /tree/x/README.md becomes a way to ask the tree about files."""
    assert client.get(f"/tree/{project['slug']}/README.md").status_code == 404


def test_the_route_404s_on_an_unknown_project(client):
    assert client.get("/tree/no-such-project/").status_code == 404


# --- the project page --------------------------------------------------------


def test_the_page_renders_only_the_root_level(client, project, workspace):
    body = client.get(f"/project/{project['slug']}").text
    assert "README.md" in body
    # The whole reason for the tree: a file three levels down is not in the
    # HTML of the project page at all.
    assert "buried.txt" not in body
    # The workspace's own src/app.js - not the portal's /static/app.js, which
    # is on every page.
    assert "/file/tree-test/src/app.js" not in body


def test_a_folder_on_the_page_carries_the_url_that_fills_it(client, project, workspace):
    body = client.get(f"/project/{project['slug']}").text
    assert f'data-tree-src="/tree/{project["slug"]}/src"' in body


def test_folders_arrive_shut(client, project, workspace):
    body = client.get(f"/project/{project['slug']}").text
    assert "<details class=\"tree-dir\"" in body
    assert "tree-dir\" open" not in body


def test_the_header_counts_every_file_not_just_the_root(client, project, workspace):
    body = client.get(f"/project/{project['slug']}").text
    assert "4 files" in body


def test_the_tree_still_lives_inside_the_ten_row_scroll_cap(client, project, workspace):
    body = client.get(f"/project/{project['slug']}").text
    assert "file-tree-scroll scroll-cap scroll-cap-files" in body


def test_an_empty_workspace_says_so(client, project):
    (config.PROJECTS_DIR / project["slug"]).mkdir(parents=True, exist_ok=True)
    body = client.get(f"/project/{project['slug']}").text
    assert "No files in workspace yet." in body
