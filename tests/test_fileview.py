"""The workspace file viewer: what each kind of file renders as, and what the
raw route is and is not allowed to serve inline.

Wes's 00:44 ask was "add support for viewing additional file types from the
workspace as reasonable. Like audio files, video files, images, pdfs.
Markdowns should be able to be viewed as rendered markdown, and text files
should be viewed with syntax highlighting."

The security half is the reason the inline route is a whitelist rather than a
mimetypes.guess_type() call: a workspace file is written by an agent, and the
portal's origin is an unauthenticated LAN page.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import config, db, fileview, main

# A one-pixel PNG and a minimal PDF, so the media routes have real bytes to
# serve rather than a file that only claims to be one by its extension.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.fixture
def client(temp_data_dir):
    return TestClient(main.app)


@pytest.fixture
def workspace(temp_data_dir) -> Path:
    db.create_project("Dice Tower", "A thing.", stage="active", build_approved=True, slug="dice-tower")
    ws = config.PROJECTS_DIR / "dice-tower"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "shot.png").write_bytes(PNG)
    (ws / "memo.m4a").write_bytes(b"\x00\x00\x00\x20ftypM4A ")
    (ws / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (ws / "spec.pdf").write_bytes(PDF)
    (ws / "README.md").write_text("# Heading\n\nSome *body* text.\n", encoding="utf-8")
    (ws / "app.py").write_text("import os\n\n\ndef go():\n    return os.getcwd()\n", encoding="utf-8")
    (ws / "notes.txt").write_text("just words\n", encoding="utf-8")
    (ws / "blob.bin").write_bytes(b"\x01\x02\x00\x03binary")
    return ws


# --- describe(): what kind of thing is this -------------------------------

@pytest.mark.parametrize(
    "name,kind",
    [
        ("shot.png", "image"),
        ("memo.m4a", "audio"),
        ("clip.mp4", "video"),
        ("spec.pdf", "pdf"),
        ("README.md", "markdown"),
        ("app.py", "text"),
        ("notes.txt", "text"),
        ("blob.bin", "binary"),
    ],
)
def test_each_file_is_described_as_its_own_kind(workspace, name, kind):
    assert fileview.describe(workspace / name).kind == kind


def test_a_media_file_is_never_read_into_memory(workspace, monkeypatch):
    """A 40MB video must cost the page render nothing - the browser fetches it
    from /raw. If describe() ever starts reading media bytes this fails."""
    def boom(self, *a, **kw):  # pragma: no cover - only runs on regression
        raise AssertionError("describe() read the bytes of a media file")

    monkeypatch.setattr(Path, "read_bytes", boom)
    for name in ("shot.png", "clip.mp4", "memo.m4a", "spec.pdf"):
        assert fileview.describe(workspace / name).is_media


def test_markdown_carries_its_source_and_no_html(workspace):
    """The rendering is done by the caller's markdown filter, so the whole app
    renders markdown one way. describe() just says "this is markdown"."""
    view = fileview.describe(workspace / "README.md")
    assert view.text.startswith("# Heading")
    assert view.html is None


def test_text_is_highlighted_and_names_its_language(workspace):
    view = fileview.describe(workspace / "app.py")
    assert view.language == "Python"
    assert 'class="highlight"' in view.html
    # The keyword is wrapped in a pygments span rather than sitting bare.
    assert "import" in view.html and "<span" in view.html


def test_an_unknown_extension_still_highlights_as_plain_text(workspace):
    (workspace / "weird.zzz").write_text("hello\n", encoding="utf-8")
    view = fileview.describe(workspace / "weird.zzz")
    assert view.kind == "text"
    assert view.language == "Text only"
    assert "hello" in view.html


def test_highlighting_escapes_html_in_the_source(workspace):
    """A workspace file is agent-written; a <script> in it must not reach the
    page as markup just because it went through a highlighter."""
    (workspace / "evil.txt").write_text("<script>alert(1)</script>\n", encoding="utf-8")
    view = fileview.describe(workspace / "evil.txt")
    assert "<script>" not in view.html
    assert "&lt;script&gt;" in view.html


def test_a_binary_gets_a_reason_not_an_exception(workspace):
    view = fileview.describe(workspace / "blob.bin")
    assert view.kind == "binary"
    assert "download" in view.reason


def test_invalid_utf8_reads_as_binary_with_its_own_reason(workspace):
    (workspace / "latin.txt").write_bytes(b"caf\xe9 time")
    view = fileview.describe(workspace / "latin.txt")
    assert view.kind == "binary"
    assert "UTF-8" in view.reason


def test_a_huge_text_file_is_refused_by_size_without_being_read(workspace, monkeypatch):
    big = workspace / "huge.log"
    big.write_text("x", encoding="utf-8")
    monkeypatch.setattr(fileview, "MAX_TEXT_BYTES", 0)
    view = fileview.describe(big)
    assert view.kind == "binary"
    assert "too large" in view.reason


def test_size_can_be_passed_in_rather_than_stat_ed(workspace):
    assert fileview.describe(workspace / "notes.txt", size=11).size == 11


# --- the inline whitelist --------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.gif", "image/gif"),
        ("a.webp", "image/webp"),
        ("a.mp3", "audio/mpeg"),
        ("a.wav", "audio/x-wav"),
        ("a.mp4", "video/mp4"),
        ("a.webm", "video/webm"),
        ("a.pdf", "application/pdf"),
    ],
)
def test_media_types_are_servable_inline(name, expected):
    assert fileview.inline_media_type(Path(name)) == expected


@pytest.mark.parametrize(
    "name",
    [
        "page.html",  # would script on the portal's origin
        "art.svg",  # an SVG can carry <script>
        "app.js",
        "style.css",
        "notes.txt",
        "README.md",
        "archive.zip",
        "unknown.zzz",
    ],
)
def test_everything_else_is_download_only(name):
    """None is not an error here - it means "the download route handles this",
    which is attachment-only under application/octet-stream."""
    assert fileview.inline_media_type(Path(name)) is None


def test_svg_is_excluded_on_purpose():
    """Stated separately because it is the one that looks safe and is not: it
    is an image type, and it is a document that can run script."""
    assert "image/svg+xml" not in fileview.INLINE_IMAGE_TYPES


# --- the routes ------------------------------------------------------------

def test_the_raw_route_serves_a_png_inline_and_sandboxed(client, workspace):
    resp = client.get("/raw/dice-tower/shot.png")
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert "inline" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in resp.headers["content-security-policy"]


def test_the_raw_route_refuses_a_text_file(client, workspace):
    """Not because text is dangerous - because a route that will serve any
    type inline is one mislabeled file away from serving HTML inline."""
    assert client.get("/raw/dice-tower/notes.txt").status_code == 415
    assert client.get("/raw/dice-tower/README.md").status_code == 415


def test_the_raw_route_cannot_escape_the_workspace(client, workspace):
    for path in ("../portal.db", "../../etc/passwd"):
        assert client.get(f"/raw/dice-tower/{path}").status_code in (400, 404)


def test_the_raw_route_404s_on_an_unknown_project(client, workspace):
    assert client.get("/raw/nope/shot.png").status_code == 404


def test_the_image_page_embeds_the_raw_url(client, workspace):
    resp = client.get("/file/dice-tower/shot.png")
    assert resp.status_code == 200
    assert '<img src="/raw/dice-tower/shot.png"' in resp.text


def test_the_audio_and_video_pages_use_real_players(client, workspace):
    assert "<audio controls" in client.get("/file/dice-tower/memo.m4a").text
    assert "<video controls" in client.get("/file/dice-tower/clip.mp4").text


def test_the_pdf_page_uses_a_sandboxed_iframe(client, workspace):
    """<iframe> rather than <embed> because the sandbox CSP on the raw
    response is what actually contains a PDF viewer, and it applies to a
    frame."""
    body = client.get("/file/dice-tower/spec.pdf").text
    assert '<iframe src="/raw/dice-tower/spec.pdf"' in body
    assert "<embed" not in body


def test_markdown_renders_and_keeps_its_source_one_fold_away(client, workspace):
    body = client.get("/file/dice-tower/README.md").text
    assert "<h1>Heading</h1>" in body
    assert "<em>body</em>" in body
    # The raw text is still reachable, folded.
    assert "the raw markdown" in body
    assert "# Heading" in body


def test_a_source_file_arrives_highlighted(client, workspace):
    body = client.get("/file/dice-tower/app.py").text
    assert 'class="highlight"' in body
    assert "Python" in body  # the language, named in the header line


def test_a_binary_gets_a_page_rather_than_a_415(client, workspace):
    """Clicking a name in the file list and landing on a raw error detail is a
    dead end. The page says what it is and offers the download."""
    resp = client.get("/file/dice-tower/blob.bin")
    assert resp.status_code == 200
    assert "binary file" in resp.text
    assert '/download/dice-tower/blob.bin' in resp.text


def test_the_viewer_still_404s_on_a_file_that_is_not_there(client, workspace):
    assert client.get("/file/dice-tower/ghost.txt").status_code == 404


def test_the_page_names_the_file_not_the_url(client, workspace):
    """base.html does `{% set path = request.url.path %}` at template level,
    which shadows a context variable called `path` inside every child block -
    so this page used to print the URL where the file name goes, and any link
    built from it came out as /download/<slug>//file/<slug>/<name>."""
    body = client.get("/file/dice-tower/README.md").text
    assert '<h1 class="file-title">README.md</h1>' in body
    assert "/download/dice-tower/README.md" in body
    # The exact shape of the old breakage: the request path pasted in where a
    # workspace-relative path belongs.
    assert "//file/dice-tower/" not in body


# --- the shipped stylesheet ------------------------------------------------

def test_the_checked_in_highlight_css_matches_pygments():
    """style.css carries pygments' rules as static text so the viewer is themed
    with the rest of the portal. This is what catches them drifting apart after
    a pygments upgrade."""
    css = (Path(main.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    rules = fileview.pygments_rules()
    assert len(rules) > 50
    missing = [r for r in rules if r not in css]
    assert not missing, f"{len(missing)} highlight rules missing from style.css, e.g. {missing[:2]}"


def test_only_the_scoped_rules_are_shipped():
    """get_style_defs() also emits a bare `pre { line-height: 125% }`, which
    would restyle the agent console and every other <pre> on the site."""
    assert all(r.startswith(".highlight") for r in fileview.pygments_rules())
    css = (Path(main.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
    assert "pre { line-height: 125%; }" not in css
