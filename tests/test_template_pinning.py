"""A running process serves the templates it booted with.

Wes, 2026-07-28: "The settings page on here now just shows 'internal server
error' and is broken."

He was right, and the cause is specific to this project: the portal's source
tree IS the live install, so an agent editing a template is editing the running
site. `main.py` already set `auto_reload = False` and claimed that kept
"template and code versions in lockstep until an explicit restart". It did not.

`auto_reload` only stops Jinja RE-checking a template it has already loaded. A
template is loaded the first time it is rendered - so settings.html, which
nobody had opened since the 05:35 restart, was read fresh off disk at 14:45,
after an agent had edited it. The new markup met the old handler's context,
raised `UndefinedError` on a variable the new route passes and the old one did
not, and 500'd. Then `auto_reload = False` made it permanent: the broken
compile was cached until the next restart.

So the fix is to load every template at import (`_precompile_templates`), and
these are the tests that keep it fixed. Two independent things are checked,
because either alone would let this back in:

- every template really is in the cache after import, and
- the cache is what a later render uses, even when the file on disk has changed.
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
from starlette.testclient import TestClient

from app import config

TEMPLATES = Path(config.APP_ROOT) / "app" / "templates"


@pytest.fixture
def client(temp_data_dir):
    from app import main

    return TestClient(main.app)


def test_every_template_is_compiled_at_import():
    from app import main

    on_disk = set(main.templates.env.list_templates())
    assert on_disk, "no templates found at all - the loader is looking in the wrong place"
    cached = set(main.templates.env.cache or {})
    # The cache is keyed by (weakref-to-loader, name); the name is what matters.
    cached_names = {key[1] if isinstance(key, tuple) else key for key in cached}
    missing = on_disk - cached_names
    assert not missing, f"loaded lazily, so an edit mid-run reaches the live site: {sorted(missing)}"
    assert main._TEMPLATES_LOADED == len(on_disk)


def test_auto_reload_is_off_so_the_cache_is_not_rechecked():
    from app import main

    assert main.templates.env.auto_reload is False


def test_a_template_edited_after_boot_does_not_reach_a_render(tmp_path):
    """The behaviour, in a FRESH interpreter - the only way to see it.

    Within one process this is untestable, and the first version of this test
    proved nothing because of it: rendering the page to get a "before" loads
    and caches the template, after which every later fetch comes from memory
    whether the fix is there or not. It passed with the precompile removed.

    The hole was always the template nobody had opened SINCE the restart, so
    the test has to start a process, edit a template it has not rendered, and
    render it for the first time. That is the incident, reproduced.
    """
    script = f'''
import sys, pathlib
sys.path.insert(0, {str(config.APP_ROOT)!r})
from app import config, db
tmp = pathlib.Path({str(tmp_path)!r})
for name in ("DATA_DIR", "MEMORY_DIR", "PROJECTS_DIR", "RUNS_DIR", "TASKS_DIR"):
    setattr(config, name, tmp if name == "DATA_DIR" else tmp / name.lower().replace("_dir", ""))
config.DB_PATH = tmp / "portal.db"
db.init_db()

# base.html, and the marker goes INSIDE the document. The first attempt
# appended to questions.html, which `extends` base - and content outside a
# block in a child template is never rendered, so the marker could not appear
# whether the fix was there or not. That version passed under sabotage for
# that reason alone.
target = pathlib.Path({str(TEMPLATES / "base.html")!r})
original = target.read_text()
assert "</body>" in original
try:
    from app import main            # <- precompile happens here, before the edit
    target.write_text(original.replace("</body>", "<!-- EDITED BY A RUN --></body>"))
    from starlette.testclient import TestClient
    page = TestClient(main.app).get("/questions").text
finally:
    target.write_text(original)
print("SAW_EDIT" if "EDITED BY A RUN" in page else "SERVED_BOOT_COPY")
'''
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=str(config.APP_ROOT), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("SERVED_BOOT_COPY"), out.stdout + out.stderr[-2000:]


def test_a_template_referencing_a_missing_context_key_is_the_failure_mode():
    """Pins WHY this matters, so the next person does not weaken the fix.

    A bare `{% if x > 1 %}` against a context with no `x` raises rather than
    reading as false - which is how one added template variable took a whole
    page down rather than degrading.
    """
    env = jinja2.Environment()
    with pytest.raises(jinja2.UndefinedError):
        env.from_string("{% if people_count > 1 %}x{% endif %}").render({})
    # ...whereas a plain truth test on the same missing name is harmless, which
    # is why only some template edits cause an outage and the rest look safe.
    assert env.from_string("{% if my_look %}x{% endif %}").render({}) == ""


def test_the_settings_page_renders(client):
    """The page that broke. Kept as its own named test rather than folded into
    a loop, because this is the one Wes reported."""
    assert client.get("/settings").status_code == 200


def test_every_page_a_template_backs_still_renders(client, temp_data_dir):
    from app import db

    project = db.create_project("Fridge", stage="active", slug="fridge")
    for path in ("/", "/settings", "/activity", "/questions", "/memory", "/tasks",
                 "/style", f"/project/{project['slug']}"):
        assert client.get(path).status_code == 200, path
