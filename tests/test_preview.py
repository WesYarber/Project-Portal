"""One standard place to click to see what a project built.

Wes, 2026-07-22 02:00, re-pasting an older request:

    "When a project is building a web page or serving a web page somewhere, add
    a place to click to launch/build/view the web page. Whatever needs to be
    done to view the tool or whatever it is. Should make it easy and
    standardized for opening what was built."

Grouped by claim: finding the page, building the address, the preview server
(including the things it must refuse), the report field, and the button.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import config, db, preview


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


@pytest.fixture
def preview_client(temp_data_dir):
    """The preview server, which is a different ASGI app on a different port."""
    preview._MOUNTS.clear()
    return TestClient(preview.preview_app, base_url="http://testserver")


def _project(slug="manabase", **fields):
    row = db.create_project("Manabase", description="A life counter", slug=slug)
    if fields:
        db.update_project(row["id"], **fields)
        row = db.get_project(row["id"])
    return row


def _workspace(slug, *files):
    """Make a workspace with the named relative paths in it."""
    root = config.PROJECTS_DIR / slug
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"<!doctype html><title>{rel}</title>")
    return root


# --------------------------------------------------------------------------
# Finding the page
# --------------------------------------------------------------------------

def test_an_index_html_in_the_workspace_root_is_the_web_root(temp_data_dir):
    _workspace("manabase", "index.html", "app.js")
    assert preview.web_root("manabase") == ""


def test_a_built_dist_directory_is_found(temp_data_dir):
    _workspace("simpleclicktrack", "dist/index.html")
    assert preview.web_root("simpleclicktrack") == "dist"


def test_the_root_wins_over_a_built_directory(temp_data_dir):
    # A project with both is serving the root one; dist is very often a stale
    # copy from a build nobody has re-run.
    _workspace("both", "index.html", "dist/index.html")
    assert preview.web_root("both") == ""


def test_a_project_with_no_page_has_none(temp_data_dir):
    _workspace("thermalproxy", "main.py", "README.md")
    assert preview.web_root("thermalproxy") is None
    assert preview.has_page("thermalproxy") is False


def test_a_missing_workspace_is_not_an_error(temp_data_dir):
    assert preview.web_root("never-created") is None


def test_an_index_inside_node_modules_is_not_a_web_root(temp_data_dir):
    # The candidate list is a fixed set of directory names, so a package that
    # happens to ship an index.html can never be mistaken for the project.
    _workspace("noisy", "node_modules/leftpad/index.html")
    assert preview.web_root("noisy") is None


def test_nothing_deeper_than_one_level_is_searched(temp_data_dir):
    _workspace("deep", "a/b/c/index.html")
    assert preview.web_root("deep") is None


def test_an_index_html_that_is_a_directory_is_not_a_page(temp_data_dir):
    root = config.PROJECTS_DIR / "odd"
    (root / "index.html").mkdir(parents=True)
    assert preview.web_root("odd") is None


# --------------------------------------------------------------------------
# The address
# --------------------------------------------------------------------------

def test_the_base_url_keeps_the_host_you_are_reading_the_page_on():
    # Wes reads the portal on the LAN address and on the tailnet one; a link
    # built from a hardcoded host would be dead on one of them.
    assert preview.base_url("http", "10.0.0.21:8500") == (
        f"http://10.0.0.21:{config.PREVIEW_PORT}"
    )


def test_https_gets_the_https_preview_port():
    assert preview.base_url("https", "testhost.tailnet1234.ts.net") == (
        f"https://testhost.tailnet1234.ts.net:{config.PREVIEW_HTTPS_PORT}"
    )


def test_a_missing_host_falls_back_to_the_server_name():
    assert config.HOST_LABEL in preview.base_url("http", "")


def test_a_project_with_a_page_gets_a_link(temp_data_dir):
    project = _project()
    _workspace("manabase", "index.html")
    link = preview.link_for(project, "http", "10.0.0.21:8500")
    assert link["url"] == f"http://10.0.0.21:{config.PREVIEW_PORT}/manabase/"
    assert "workspace" in link["hint"]


def test_a_project_with_nothing_to_show_gets_no_link(temp_data_dir):
    project = _project()
    _workspace("manabase", "README.md")
    assert preview.link_for(project, "http", "h") is None


def test_an_explicit_url_wins_over_the_scan(temp_data_dir):
    # A project that runs its own server cannot be served as static files, and
    # a link to a stale static copy of it would be worse than no link.
    project = _project(preview_url="http://testhost:3000/")
    _workspace("manabase", "index.html")
    link = preview.link_for(project, "http", "10.0.0.21:8500")
    assert link["url"] == "http://testhost:3000/"
    assert "serves on" in link["hint"]


def test_link_for_none_is_none():
    assert preview.link_for(None) is None


def test_the_url_is_escaped_into_the_path(temp_data_dir):
    project = _project(slug="board-games-settlers-catan")
    _workspace("board-games-settlers-catan", "index.html")
    link = preview.link_for(project, "http", "h:8500")
    assert link["url"].endswith("/board-games-settlers-catan/")


# --------------------------------------------------------------------------
# The preview server
# --------------------------------------------------------------------------

def test_it_serves_the_page(temp_data_dir, preview_client):
    _project()
    _workspace("manabase", "index.html")
    resp = preview_client.get("/manabase/")
    assert resp.status_code == 200
    assert "index.html" in resp.text


def test_it_serves_assets_beside_the_page(temp_data_dir, preview_client):
    _project()
    root = _workspace("manabase", "index.html")
    (root / "style.css").write_text("body{color:red}")
    resp = preview_client.get("/manabase/style.css")
    assert resp.status_code == 200
    assert "color:red" in resp.text


def test_it_serves_out_of_the_built_directory(temp_data_dir, preview_client):
    _workspace("sct", "dist/index.html")
    resp = preview_client.get("/sct/")
    assert resp.status_code == 200
    assert "dist/index.html" in resp.text


def test_a_slugless_path_redirects_so_relative_urls_resolve(temp_data_dir, preview_client):
    # Without this every relative <link>/<script> in the page resolves one
    # level too high and the app loads unstyled with no JS.
    _workspace("manabase", "index.html")
    resp = preview_client.get("/manabase", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/manabase/"


def test_a_project_with_no_page_is_a_readable_404(temp_data_dir, preview_client):
    _workspace("thermalproxy", "main.py")
    resp = preview_client.get("/thermalproxy/")
    assert resp.status_code == 404
    assert "no page to preview" in resp.text


def test_an_unknown_project_is_a_404(temp_data_dir, preview_client):
    assert preview_client.get("/nope/").status_code == 404


def test_the_root_explains_itself(temp_data_dir, preview_client):
    resp = preview_client.get("/")
    assert resp.status_code == 200
    assert "preview server" in resp.text


@pytest.mark.parametrize("path", [
    "/../../portal.db",
    "/..%2f..%2fportal.db",
    "/manabase/../../portal.db",
    "/MANABASE/",
    "/man abase/",
    "/.git/config",
])
def test_it_refuses_anything_that_is_not_a_slug_shaped_path(
    temp_data_dir, preview_client, path
):
    _workspace("manabase", "index.html")
    resp = preview_client.get(path)
    assert resp.status_code in (307, 404), resp.text
    assert "portal.db" not in resp.text


def test_it_cannot_be_walked_out_of_a_workspace(temp_data_dir, preview_client):
    _workspace("manabase", "index.html")
    (config.PROJECTS_DIR / "secret.txt").write_text("not yours")
    resp = preview_client.get("/manabase/../secret.txt")
    assert "not yours" not in resp.text


def test_a_symlink_out_of_the_workspace_is_not_followed(temp_data_dir, preview_client):
    root = _workspace("manabase", "index.html")
    (config.PROJECTS_DIR / "secret.txt").write_text("not yours")
    (root / "escape.txt").symlink_to(config.PROJECTS_DIR / "secret.txt")
    resp = preview_client.get("/manabase/escape.txt")
    assert "not yours" not in resp.text


# --------------------------------------------------------------------------
# What the preview server will not hand out
#
# A workspace is an agent's whole working directory, not a docroot, and this
# port has no login. On 2026-09-22 the commander-case-custom-lid agent measured
# its own `.secrets/makerworld-cookie.txt` - Wes's live signed-in MakerWorld
# session - answering 200 from the LAN, along with `.git/HEAD` and `server.ts`.
# These are the tests of the allowlist that closed it.
# --------------------------------------------------------------------------

def test_a_secret_in_the_workspace_is_not_served(temp_data_dir, preview_client):
    root = _workspace("lidmaker", "index.html")
    (root / ".secrets").mkdir()
    (root / ".secrets" / "makerworld-cookie.txt").write_text("bbl_device_id=live")
    resp = preview_client.get("/lidmaker/.secrets/makerworld-cookie.txt")
    assert resp.status_code == 404
    assert "bbl_device_id" not in resp.text


def test_the_git_history_inside_a_workspace_is_not_served(temp_data_dir, preview_client):
    root = _workspace("lidmaker", "index.html")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/master")
    assert preview_client.get("/lidmaker/.git/HEAD").status_code == 404


def test_the_projects_own_source_is_not_served(temp_data_dir, preview_client):
    root = _workspace("lidmaker", "index.html")
    (root / "server.ts").write_text("const TOKEN = 'hunter2'")
    resp = preview_client.get("/lidmaker/server.ts")
    assert resp.status_code == 404
    assert "hunter2" not in resp.text


@pytest.mark.parametrize("name", [
    "notes.md", "portal.db", "run.log", "key.pem", ".env",
    "requirements.txt", "Makefile", "backup.bak", "dump.sql", "app.py",
])
def test_nothing_a_browser_does_not_render_is_served(
    temp_data_dir, preview_client, name
):
    root = _workspace("lidmaker", "index.html")
    (root / name).write_text("private")
    resp = preview_client.get(f"/lidmaker/{name}")
    assert resp.status_code == 404, name
    assert "private" not in resp.text


def test_head_is_refused_through_the_same_door_as_get(temp_data_dir, preview_client):
    # The classic way to ship half a fix: guard do_GET and leave HEAD, which
    # confirms the file exists and its size to anybody who asks.
    root = _workspace("lidmaker", "index.html")
    (root / "server.ts").write_text("const TOKEN = 'hunter2'")
    assert preview_client.head("/lidmaker/server.ts").status_code == 404
    assert preview_client.head("/lidmaker/index.html").status_code == 200


def test_a_refused_file_is_indistinguishable_from_a_missing_one(
    temp_data_dir, preview_client
):
    # A refusal that says "403" is an existence oracle: it tells a stranger
    # which secrets this workspace holds even while refusing to send them.
    root = _workspace("lidmaker", "index.html")
    # Not a real key block: the tree's own leak scan would flag the literal,
    # and what is being proved is that these bytes never leave, not what
    # they say.
    (root / "key.pem").write_text("PRIVATE-KEY-BYTES")
    refused = preview_client.get("/lidmaker/key.pem")
    missing = preview_client.get("/lidmaker/nothing-here.pem")
    assert refused.status_code == missing.status_code == 404
    assert refused.text == missing.text


def test_a_dotted_directory_beats_an_allowed_suffix(temp_data_dir, preview_client):
    # `.json` is on the allowlist because Wes's apps fetch their rules as JSON.
    # A dotted segment refuses it anyway, which is why the allowlist can afford
    # to carry `.json` at all.
    root = _workspace("lidmaker", "index.html")
    (root / ".secrets").mkdir()
    (root / ".secrets" / "tokens.json").write_text('{"token": "live"}')
    resp = preview_client.get("/lidmaker/.secrets/tokens.json")
    assert resp.status_code == 404
    assert "live" not in resp.text


def test_a_symlink_inside_the_workspace_is_judged_by_what_it_points_at(
    temp_data_dir, preview_client
):
    # The check is on the RESOLVED path, so dressing a key up as a picture does
    # not get it past a suffix test on the request string.
    root = _workspace("lidmaker", "index.html")
    # Not a real key block: the tree's own leak scan would flag the literal,
    # and what is being proved is that these bytes never leave, not what
    # they say.
    (root / "key.pem").write_text("PRIVATE-KEY-BYTES")
    (root / "logo.png").symlink_to(root / "key.pem")
    resp = preview_client.get("/lidmaker/logo.png")
    assert resp.status_code == 404
    assert "PRIVATE-KEY-BYTES" not in resp.text


def test_the_page_and_its_assets_still_load(temp_data_dir, preview_client):
    root = _workspace("lidmaker", "index.html")
    (root / "app.js").write_text("console.log(1)")
    (root / "style.css").write_text("body{color:red}")
    (root / "rules.json").write_text('{"ok": true}')
    (root / "logo.svg").write_text("<svg/>")
    (root / "fira.woff2").write_bytes(b"wOF2")
    for rel in ("", "app.js", "style.css", "rules.json", "logo.svg", "fira.woff2"):
        assert preview_client.get(f"/lidmaker/{rel}").status_code == 200, rel


def test_a_page_one_directory_down_still_resolves_to_its_index(
    temp_data_dir, preview_client
):
    # Directories have to pass the allowlist untouched or html-mode never gets
    # the chance to turn `/about/` into `/about/index.html`.
    _workspace("lidmaker", "index.html", "about/index.html")
    resp = preview_client.get("/lidmaker/about/")
    assert resp.status_code == 200
    assert "about/index.html" in resp.text


def test_an_uppercase_suffix_is_matched_the_same_way(temp_data_dir):
    root = config.PROJECTS_DIR / "lidmaker"
    assert preview.is_servable(root / "LOGO.PNG", root) is True
    assert preview.is_servable(root / "KEY.PEM", root) is False


@pytest.mark.parametrize("rel,servable", [
    ("index.html", True),
    ("assets/app.js", True),
    ("sub/dir/pic.jpeg", True),
    ("server.ts", False),
    ("Makefile", False),
    (".env", False),
    (".git/HEAD", False),
    ("nested/.secrets/token.json", False),
    ("data/bundle.json", True),
])
def test_is_servable_is_the_whole_policy(temp_data_dir, rel, servable):
    root = config.PROJECTS_DIR / "lidmaker"
    assert preview.is_servable(root / rel, root) is servable


def test_a_path_outside_the_web_root_is_never_servable(temp_data_dir):
    root = config.PROJECTS_DIR / "lidmaker"
    assert preview.is_servable(config.PROJECTS_DIR / "other" / "x.png", root) is False


def test_a_project_that_gains_a_page_is_served_without_a_restart(
    temp_data_dir, preview_client
):
    # The mount set is resolved per request, so a project built five minutes
    # ago is viewable now rather than after the next deploy.
    root = config.PROJECTS_DIR / "manabase"
    root.mkdir(parents=True)
    assert preview_client.get("/manabase/").status_code == 404
    (root / "index.html").write_text("<!doctype html>built now")
    assert "built now" in preview_client.get("/manabase/").text


def test_a_web_root_that_moves_is_not_served_from_the_old_one(
    temp_data_dir, preview_client
):
    # The mount cache is keyed on the resolved directory, so a stale mount can
    # never outlive the directory it was made for.
    _workspace("sct", "dist/index.html")
    assert "dist/index.html" in preview_client.get("/sct/").text
    (config.PROJECTS_DIR / "sct" / "index.html").write_text("<!doctype html>moved to root")
    assert "moved to root" in preview_client.get("/sct/").text


# --------------------------------------------------------------------------
# The report field
# --------------------------------------------------------------------------

def test_an_agent_can_declare_the_address_it_serves_on(temp_data_dir):
    project = _project()
    assert preview.apply_report(project, {"preview_url": "http://testhost:3000"}) == (
        "http://testhost:3000"
    )
    assert db.get_project(project["id"])["preview_url"] == "http://testhost:3000"


@pytest.mark.parametrize("value", [
    None, "", "   ", 42, ["http://x"], "javascript:alert(1)", "file:///etc/passwd",
    "testhost:3000", "x" * 600,
])
def test_a_report_url_that_is_not_an_http_address_is_dropped(temp_data_dir, value):
    project = _project()
    assert preview.apply_report(project, {"preview_url": value}) is None
    assert db.get_project(project["id"])["preview_url"] == ""


def test_a_report_without_the_field_leaves_a_set_url_alone(temp_data_dir):
    # An agent that forgets to repeat the field must not silently unpublish the
    # button, so the report path only ever sets.
    project = _project(preview_url="http://testhost:3000")
    preview.apply_report(project, {"summary": ["did a thing"]})
    assert db.get_project(project["id"])["preview_url"] == "http://testhost:3000"


def test_the_worker_applies_it(temp_data_dir):
    from app import agent_runner, worker

    project = _project()
    worker._apply_report(
        project,
        agent_runner.RunResult(
            ok=True,
            report={"journal_entry_md": "ok", "preview_url": "http://testhost:3000"},
        ),
        task="build",
    )
    assert db.get_project(project["id"])["preview_url"] == "http://testhost:3000"


def test_the_contract_tells_agents_about_it():
    from app import agent_runner

    assert "preview_url" in agent_runner.AGENT_CONTRACT


# --------------------------------------------------------------------------
# The button
# --------------------------------------------------------------------------

def test_the_project_page_shows_open_it_when_there_is_a_page(temp_data_dir, client):
    _project()
    _workspace("manabase", "index.html")
    body = client.get("/project/manabase").text
    assert "preview-open" in body
    # Since the launcher landed, the button routes through /open so a down
    # server can be started before Wes lands on it; the static case is one
    # redirect through the same route (covered in test_open_launch.py).
    assert 'href="/open/manabase"' in body


def test_the_project_page_has_no_button_when_there_is_nothing_to_open(
    temp_data_dir, client
):
    _project()
    _workspace("manabase", "README.md")
    assert "preview-open" not in client.get("/project/manabase").text


def test_the_owner_can_type_the_address_on_the_details_form(temp_data_dir, client):
    project = _project()
    _workspace("manabase", "README.md")
    client.post("/project/manabase/details", data={
        "title": "Manabase", "description": "A life counter",
        "new_slug": "manabase", "preview_url": " http://testhost:3000 ",
    })
    assert db.get_project(project["id"])["preview_url"] == "http://testhost:3000"
    assert "preview-open" in client.get("/project/manabase").text


def test_emptying_the_box_takes_the_button_away(temp_data_dir, client):
    project = _project(preview_url="http://testhost:3000")
    _workspace("manabase", "README.md")
    client.post("/project/manabase/details", data={
        "title": "Manabase", "description": "A life counter",
        "new_slug": "manabase", "preview_url": "",
    })
    assert db.get_project(project["id"])["preview_url"] == ""
    assert "preview-open" not in client.get("/project/manabase").text


def test_the_button_opens_in_a_new_tab(temp_data_dir, client):
    # It is a different origin; navigating the portal away to it would lose the
    # page Wes clicked from.
    _project()
    _workspace("manabase", "index.html")
    body = client.get("/project/manabase").text
    assert 'target="_blank"' in body and 'rel="noopener"' in body


def test_a_busy_preview_port_does_not_take_the_portal_down(temp_data_dir, monkeypatch):
    """uvicorn answers a bind failure by logging it and calling sys.exit(1).
    SystemExit is a BaseException, so it walks straight past `except OSError`,
    out of the task, and asyncio stops the loop - meaning the whole portal would
    exit because the *preview* port was taken. Which is what deploy/preview.py
    does every time it boots a second portal against a copy of the database, and
    what the live portal does to the test suite.

    Bound with a real socket rather than a mock: the thing being asserted is how
    uvicorn behaves on a genuinely busy port.
    """
    import asyncio
    import socket

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.listen(1)
    monkeypatch.setattr(config, "PREVIEW_PORT", sock.getsockname()[1])
    try:
        asyncio.run(asyncio.wait_for(preview.serve_loop(), timeout=10))
    finally:
        sock.close()
