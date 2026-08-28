"""The README's screenshots, and the script that produces them.

A broken image in a README is only visible on github.com, which is the one
place nobody developing this looks - so the link between the markdown and the
files on disk is pinned here instead. The same goes for `deploy/demo_data.py`:
it is not imported by anything, so a rename in `app/db.py` would break it
silently and stay broken until somebody went to retake a shot.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DEMO = ROOT / "deploy" / "demo_data.py"

# Both markdown `![alt](path)` and the one raw <img> the phone shot uses, since
# a width attribute is the only way to stop a 414px-wide capture rendering at
# full column width.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\((docs/images/[^)]+)\)")
_HTML_IMAGE = re.compile(r'<img src="(docs/images/[^"]+)"')


def readme_images() -> list[str]:
    text = README.read_text(encoding="utf-8")
    return _MD_IMAGE.findall(text) + _HTML_IMAGE.findall(text)


def test_the_readme_shows_screenshots_at_all():
    assert len(readme_images()) >= 3


def test_every_readme_image_is_actually_in_the_repo():
    missing = [p for p in readme_images() if not (ROOT / p).is_file()]
    assert missing == []


def test_every_image_in_the_repo_is_used_by_the_readme():
    """The other direction, so a retaken shot under a new name leaves no
    orphan behind - these are committed binaries and they add up."""
    used = set(readme_images())
    on_disk = {f"docs/images/{p.name}" for p in (ROOT / "docs" / "images").iterdir()}
    assert on_disk - used == set()


def test_the_screenshots_stay_small_enough_to_clone():
    """A README's images are downloaded by everybody who clones, forever, and
    git keeps every version. 500 KB each is generous for a flat-color terminal
    UI and still catches somebody committing a raw retina capture."""
    too_big = {
        p.name: p.stat().st_size
        for p in (ROOT / "docs" / "images").iterdir()
        if p.stat().st_size > 500_000
    }
    assert too_big == {}


def test_every_readme_image_has_alt_text():
    """Read on a phone, on a slow connection, or by a screen reader - an image
    with no alt text is a blank space in all three."""
    text = README.read_text(encoding="utf-8")
    assert re.search(r"!\[\]\(docs/images/", text) is None
    for tag in re.findall(r"<img [^>]*docs/images/[^>]*>", text):
        assert 'alt="' in tag, tag


def test_the_demo_script_still_matches_the_code_it_calls():
    """Names it uses out of app/, checked against what those modules export.

    Not an import: importing it wipes /tmp/portal-readme-demo, rebinds
    `config.DATA_DIR` process-wide and starts uvicorn. This reads the source
    instead, which is enough to catch the failure that actually happens - a
    function renamed or removed underneath a script nothing else references.
    """
    from app import db, limits, people

    tree = ast.parse(DEMO.read_text(encoding="utf-8"))
    modules = {"db": db, "people": people, "limits": limits}
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        module = modules.get(node.value.id)
        if module is None:
            continue
        checked += 1
        assert hasattr(module, node.attr), (
            f"deploy/demo_data.py calls {node.value.id}.{node.attr}, "
            f"which app/{node.value.id}.py no longer has"
        )
    # The count itself matters: a walk that found nothing would pass this test
    # while checking not one thing.
    assert checked >= 10


def test_the_demo_script_cannot_touch_the_real_board():
    """Its whole safety argument is three lines, and all three are one edit
    away from being deleted by somebody tidying up."""
    source = DEMO.read_text(encoding="utf-8")
    assert 'config.DB_PATH = TMP / "portal.db"' in source
    assert 'db.set_setting("worker_enabled", "1")' in source
    # ...but only because every background loop is unhooked first. Without this
    # the demo would spawn real runs against invented projects.
    assert "_main.app.router.on_startup = [" in source
    # And the usage endpoint is stubbed, so no shot can carry somebody's real
    # subscription percentages.
    assert "limits.cached = lambda" in source
