"""Delete-the-fix sweep for the 2026-08-28 page-weight / seamless-note change.

Three features, three files: the journal window (app/journalwindow.py plus its
call site in main.py), the in-place multipart note post, and the staged-file
shelf (both in app/static/app.js), with the two template markers that make any
of it reachable.

The suite here is the subset of tests that can see those files rather than the
whole 4552 (222s). Rule 2 of docs/verifying-with-mutations.md is satisfied the
other way round: the FULL suite was run green immediately before this, so a
broad failure here is news about the mutation, not about the baseline.

Follows docs/verifying-with-mutations.md:
  1. restores however it dies (atexit + SIGTERM/SIGINT -> sys.exit)
  2. reports the tree state, and is meant to be run detached, waited on by the
     SWEEP COMPLETE marker rather than by a process name (rule 5)
  3. reads a crashed pytest as SKIPPED, never as "uncaught"
  4. mutates the wiring - the template markers and the call site - and not only
     the pure functions, because a call site is the easy thing to forget
  6. parses the FAILED lines properly (the first token is the word FAILED)
"""
from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
WINDOW = ROOT / "app" / "journalwindow.py"
MAIN = ROOT / "app" / "main.py"
PROJECT_HTML = ROOT / "app" / "templates" / "project.html"
STYLE = ROOT / "app" / "static" / "style.css"
PY = ROOT / "venv" / "bin" / "python"

SUITE = [
    str(ROOT / "tests" / name)
    for name in (
        "test_journal_window.py",
        "test_attach_shelf.py",
        "test_inplace_submit.py",
        "test_console_poll.py",
        "test_recorder.py",
        "test_attachments.py",
        "test_attachment_removal.py",
        "test_journal_quote.py",
        "test_jump_keys.py",
        "test_leakscan.py",
    )
    if (ROOT / "tests" / name).exists()
]

TARGETS = (APP_JS, WINDOW, MAIN, PROJECT_HTML, STYLE)

# Read - and the restore hook armed - before anything can go wrong, so a death
# anywhere below still puts every file back (rule 1).
ORIGINAL = {p: p.read_text() for p in TARGETS}


def restore() -> None:
    for path, text in ORIGINAL.items():
        if path.read_text() != text:
            path.write_text(text)


atexit.register(restore)
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda *_: sys.exit(1))


# (file, name, find, replace_with, owning test)
#
# Each one deletes or reverts a line this change ADDED. The owner is the test
# that should be the one to notice.
MUTATIONS = [
    # --- the journal window: both caps, the floor, and the bypass ------------
    (
        WINDOW,
        "no entry cap at all",
        "        if len(shown) >= max_entries:\n            break\n",
        "",
        "test_the_entry_cap_holds_at_its_own_boundary",
    ),
    (
        WINDOW,
        "entry cap off by one (> instead of >=)",
        "        if len(shown) >= max_entries:",
        "        if len(shown) > max_entries:",
        "test_the_entry_cap_holds_at_its_own_boundary",
    ),
    (
        WINDOW,
        "no character budget, so one long report is the whole page",
        "        if len(shown) >= min_entries and used + size > max_chars:\n            break\n",
        "",
        "test_the_character_cap_stops_a_page_full_of_long_reports",
    ),
    (
        WINDOW,
        "character budget off by one (>= instead of >)",
        "        if len(shown) >= min_entries and used + size > max_chars:",
        "        if len(shown) >= min_entries and used + size >= max_chars:",
        "test_the_character_cap_holds_at_its_own_boundary",
    ),
    (
        WINDOW,
        "the floor does not beat the budget, so a long report opens an empty journal",
        "        if len(shown) >= min_entries and used + size > max_chars:",
        "        if used + size > max_chars:",
        "test_the_minimum_beats_the_character_budget",
    ),
    (
        WINDOW,
        "skip the entry that busts the budget instead of ending the window",
        "            break\n        shown.append(row)",
        "            continue\n        shown.append(row)",
        "test_the_window_is_contiguous_rather_than_skipping_big_entries",
    ),
    (
        WINDOW,
        "nothing accumulates, so the budget is never reached",
        "        used += size",
        "        used += 0",
        "test_the_character_cap_stops_a_page_full_of_long_reports",
    ),
    (
        WINDOW,
        "show_all is ignored - expanding the journal does nothing",
        "    if show_all:\n        return rows, 0\n",
        "",
        "test_show_all_bypasses_both_caps",
    ),
    (
        WINDOW,
        "the window is taken from the oldest end",
        "    rows = list(entries)",
        "    rows = list(entries)[::-1]",
        "test_the_window_is_taken_from_the_newest_end",
    ),
    (
        WINDOW,
        "hidden is miscounted, so the page lies about how much is left",
        "    return shown, len(rows) - len(shown)",
        "    return shown, len(rows)",
        "test_a_short_journal_is_shown_whole_and_hides_nothing",
    ),
    (
        WINDOW,
        "an unreadable entry raises instead of costing nothing",
        "    except (KeyError, IndexError, TypeError):\n        return 0",
        "    except ():\n        return 0",
        "test_an_entry_with_no_readable_content_costs_nothing_rather_than_raising",
    ),
    # --- the wiring for it (rule 4) -----------------------------------------
    (
        MAIN,
        "the route renders every entry again, window or no window",
        "    journal, journal_hidden = journalwindow.window(journal_rows, show_all=journal_all)",
        "    journal, journal_hidden = journal_rows, 0",
        "test_the_page_renders_a_window_and_offers_the_rest",
    ),
    (
        MAIN,
        "?journal=all is never read, so the rest is unreachable",
        '    journal_all = request.query_params.get("journal") == "all"',
        "    journal_all = False",
        "test_asking_for_all_of_it_gets_all_of_it",
    ),
    (
        PROJECT_HTML,
        "no 'show older' link, so the hidden entries are simply gone",
        "  {% if journal_hidden %}",
        "  {% if False %}",
        "test_the_page_renders_a_window_and_offers_the_rest",
    ),
    (
        PROJECT_HTML,
        "the link is offered even when nothing is hidden",
        "  {% if journal_hidden %}",
        "  {% if True %}",
        "test_a_short_journal_offers_no_link_at_all",
    ),
    (
        PROJECT_HTML,
        "'show 1 older entries'",
        "entr{{ 'y' if journal_hidden == 1 else 'ies' }}",
        "entries",
        "test_one_hidden_entry_is_named_in_the_singular",
    ),
    (
        PROJECT_HTML,
        "no way back from the expanded journal",
        "  {% elif journal_all %}",
        "  {% elif False %}",
        "test_asking_for_all_of_it_gets_all_of_it",
    ),
    # --- the note posted in place -------------------------------------------
    (
        PROJECT_HTML,
        "the note form navigates again (the whole complaint)",
        "        data-inplace data-compose>",
        "        data-compose>",
        "test_the_note_form_actually_carries_both_markers",
    ),
    (
        PROJECT_HTML,
        "in place, but the box is never emptied",
        "        data-inplace data-compose>",
        "        data-inplace>",
        "test_the_note_form_actually_carries_both_markers",
    ),
    (
        APP_JS,
        "a form with files posts urlencoded, so every upload arrives empty",
        "  var posted = isMultipartForm(form)\n"
        "    ? postMultipart(action, data, done)\n"
        "    : postForm(action, formFields(data), done);",
        "  var posted = postForm(action, formFields(data), done);",
        "test_a_note_posts_as_multipart_with_no_content_type_header",
    ),
    (
        APP_JS,
        "enctype is ignored when deciding multipart",
        '  if ((form.getAttribute("enctype") || "").toLowerCase() === "multipart/form-data") return true;\n',
        "",
        # NOT the note-form test: that fixture has a file input too, which
        # answers for the deleted line and makes the mutation a no-op. Only a
        # form carrying the declaration WITHOUT a file can see this branch.
        "test_a_declared_multipart_form_posts_multipart_with_no_file_in_it",
    ),
    (
        APP_JS,
        "a Content-Type is set by hand, with no boundary the server can split on",
        "function postMultipart(action, formData, onDone) {\n  return postBody(action, formData, null, onDone);",
        "function postMultipart(action, formData, onDone) {\n"
        '  return postBody(action, formData, { "Content-Type": "multipart/form-data" }, onDone);',
        "test_a_note_posts_as_multipart_with_no_content_type_header",
    ),
    (
        APP_JS,
        "every in-place form posts multipart now, not only the ones with files",
        "  var posted = isMultipartForm(form)",
        "  var posted = true || isMultipartForm(form)",
        "test_an_in_place_form_with_no_files_still_posts_urlencoded",
    ),
    (
        APP_JS,
        "the compose box is never emptied, so a sent note sits in it looking unsent",
        "    if (compose) clearComposeForm(form);\n",
        "",
        "test_a_sent_note_leaves_the_box_empty",
    ),
    (
        APP_JS,
        "the box is emptied whether or not the server took the note",
        "  var done = function () {\n    if (compose) clearComposeForm(form);",
        "  if (compose) clearComposeForm(form);\n  var done = function () {",
        "test_a_refused_note_keeps_every_word_of_it",
    ),
    (
        APP_JS,
        "every in-place form has its fields blanked, not only [data-compose] ones",
        '  var compose = !!(form.matches && form.matches("[data-compose]"));',
        "  var compose = true;",
        "test_clearing_is_the_compose_markers_doing_and_nothing_elses",
    ),
    (
        APP_JS,
        "the staged files are left in the input after a send",
        "  form.querySelectorAll('input[type=\"file\"]').forEach(function (input) {\n"
        "    if (typeof DataTransfer !== \"undefined\") input.files = new DataTransfer().files;\n"
        "    else input.value = \"\";\n"
        "  });\n",
        "",
        "test_a_sent_note_leaves_the_box_empty",
    ),
    (
        APP_JS,
        "the quote chip rides along on the next note too",
        '  form.querySelectorAll(".quote-chip").forEach(function (chip) { chip.remove(); });\n',
        "",
        "test_a_sent_note_leaves_the_box_empty",
    ),
    (
        APP_JS,
        "the pressed button is dropped, so 'queue note' starts a run",
        "  if (ev.submitter && ev.submitter.name) data.set(ev.submitter.name, ev.submitter.value);",
        "",
        "test_which_note_button_was_pressed_rides_along",
    ),
    (
        APP_JS,
        "the note form still stashes a scroll position nothing consumes",
        "  if (inPlaceAction(ev)) return;",
        "",
        "test_sending_a_note_stashes_no_scroll_position",
    ),
    # --- the staged-file shelf ----------------------------------------------
    (
        APP_JS,
        "remove drops every file, not the one asked for",
        "    if (existing[i].name !== name) dt.items.add(existing[i]);",
        "    if (false) dt.items.add(existing[i]);",
        "test_removing_one_file_keeps_the_others_and_their_order",
    ),
    (
        APP_JS,
        "the shelf is not redrawn after a remove",
        '    removeFile(input, file.name);\n    if (input._afterFiles) input._afterFiles();',
        "    removeFile(input, file.name);",
        "test_removing_one_file_keeps_the_others_and_their_order",
    ),
    (
        APP_JS,
        "no default redraw hook, so a row's own buttons change nothing visible",
        "  if (!input._afterFiles) {\n"
        "    input._afterFiles = function () { renderAttachShelf(input, shelf); };\n"
        "  }\n",
        "",
        "test_removing_one_file_keeps_the_others_and_their_order",
    ),
    (
        APP_JS,
        "rename is cosmetic - the row changes, the posted file does not",
        "    if (renameFile(input, current, want) && input._afterFiles) input._afterFiles();",
        "    nameEl.textContent = want;",
        "test_renaming_rewrites_the_file_that_will_actually_be_posted",
    ),
    (
        APP_JS,
        "a renamed file loses its bytes",
        "      dt.items.add(new File([f], want, { type: f.type, lastModified: f.lastModified }));",
        "      dt.items.add(new File([], want, { type: f.type, lastModified: f.lastModified }));",
        "test_a_renamed_file_is_the_same_file",
    ),
    (
        APP_JS,
        "rename by remove-then-re-add, which moves the file to the end",
        "    if (f.name === from) {",
        "    if (false) {",
        "test_renaming_rewrites_the_file_that_will_actually_be_posted",
    ),
    (
        APP_JS,
        "a rename onto another staged file's name silently overwrites it",
        "    if (existing[i].name === want) return \"\";",
        "    if (false) return \"\";",
        "test_a_rename_onto_another_staged_files_name_is_refused",
    ),
    # VERIFIED NO-OP, kept as the record of that rather than deleted (doc
    # section 8: a stubborn MISSED may be a no-op mutation, not a weak test).
    # Deleting this early return changes no observable behavior, because both
    # cases it names are already caught downstream: an empty name leaves
    # `renamed` empty and falls out at `if (!renamed) return ""`, and an
    # unchanged name is found by the duplicate scan. It is a fast path, not the
    # enforcement, and no test can tell a tree with it from one without.
    (
        APP_JS,
        "an empty name is accepted (KNOWN NO-OP, see above)",
        '  if (!want || want === from) return "";',
        "",
        "test_an_empty_or_unchanged_name_is_refused",
    ),
    (
        APP_JS,
        "Escape applies the rename instead of abandoning it",
        '    } else if (ev.key === "Escape") {',
        "    } else if (false) {",
        "test_escape_abandons_a_rename",
    ),
    (
        APP_JS,
        "Escape also reaches the page-wide close-everything handler",
        "      ev.preventDefault();\n      ev.stopPropagation();\n      finish(false);",
        "      ev.preventDefault();\n      finish(false);",
        "test_escape_does_not_also_reach_the_page_wide_handler",
    ),
    (
        APP_JS,
        "voice memos are listed twice, with two delete buttons",
        "    if (recorded[files[i].name]) continue;\n",
        "",
        "test_a_voice_memo_is_left_to_the_recorders_own_shelf",
    ),
    (
        APP_JS,
        "recordings are guessed from the name, hiding a file the user chose",
        "    if (recorded[files[i].name]) continue;",
        '    if (files[i].name.indexOf("voice-memo-") === 0) continue;',
        "test_a_file_merely_named_like_a_recording_is_still_listed",
    ),
    (
        APP_JS,
        "an oversized file is not marked on its own row",
        '  if (oversize) row.classList.add("oversize");\n',
        "",
        "test_a_file_too_big_to_send_says_so_on_its_own_row",
    ),
    (
        APP_JS,
        "thumbnails are never revoked, pinning every dropped image in memory",
        "    if (old[i]._objectUrl && window.URL && URL.revokeObjectURL) URL.revokeObjectURL(old[i]._objectUrl);\n",
        "",
        "test_redrawing_the_shelf_revokes_the_thumbnails_it_replaced",
    ),
    (
        APP_JS,
        "the shelf appends on redraw instead of replacing",
        '  shelf.textContent = "";\n',
        "",
        "test_redrawing_the_shelf_revokes_the_thumbnails_it_replaced",
    ),
    (
        APP_JS,
        "no picture for a picture - every file is a text row",
        '  if (file.type && file.type.indexOf("image/") === 0 && window.URL && URL.createObjectURL) {',
        "  if (false) {",
        "test_a_picture_is_shown_as_the_picture",
    ),
    (
        APP_JS,
        "a staged file is thrown away by a background patch",
        # Anchored on the CLOSING quote and semicolon. MORPH_KEEP is built by
        # concatenation, so its opening quote sits back at "#sel-actions - and
        # the naive pattern `".rec-row, .attach-row-item"` therefore does not
        # appear in it at all. It matched clearComposeForm instead, which is a
        # different (also real) defect and was caught by a different test. A
        # mutation that lands somewhere other than where you aimed it reads as
        # a weak test for a line the sweep never touched.
        '.rec-row, .attach-row-item";',
        '.rec-row";',
        "test_a_staged_row_survives_a_background_patch",
    ),
    (
        APP_JS,
        "a sent note leaves its staged rows on the shelf",
        'form.querySelectorAll(".rec-row, .attach-row-item")',
        'form.querySelectorAll(".rec-row")',
        "test_a_sent_note_leaves_the_box_empty",
    ),
    (
        APP_JS,
        "the recorder never claims its own file, so the memo is listed twice",
        "    recordedNames(input)[name] = true;\n",
        "",
        "test_the_recorder_claims_the_file_it_just_attached",
    ),
    (
        APP_JS,
        "deleting a take leaves its claim behind",
        "      delete recordedNames(input)[file.name];\n",
        "",
        "test_deleting_a_take_releases_its_claim",
    ),
    (
        STYLE,
        "the rename field zooms an iPhone in and never back out",
        ".attach-row-rename { width: 100%; font-size: 16px; }",
        ".attach-row-rename { width: 100%; font-size: 0.9rem; }",
        "test_the_rename_field_cannot_zoom_an_iphone_in",
    ),
]

# ERROR as well as FAILED, and this is rule 6 of the sweep doc in a new
# costume. Three of these mutations are seen only by tests behind a
# MODULE-SCOPED fixture that shells out to bun. When a mutation makes the bun
# harness itself throw, the fixture raises - and pytest reports a fixture that
# raised as `ERROR tests/x.py::test_y`, never as `FAILED`. Parsing only FAILED
# lines therefore read a mutation its tests DO catch as an uncaught one, which
# is exactly the lie the doc warns about, arriving through a door it does not
# list. (The harness has also been fixed not to throw; both halves are needed,
# because the next mutation that crashes a harness will be a different one.)
FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
SUMMARY = re.compile(r"^\d+ (passed|failed|error)|=+ .*(passed|failed|error)", re.M)


def run_suite() -> tuple[set[str], int, bool]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *SUITE, "-q", "-p", "no:randomly", "--no-header", "-rf"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    out = proc.stdout + proc.stderr
    names = {m.split("::")[-1] for m in FAILED.findall(out)}
    # Rule 3: a crashed pytest is a SKIPPED data point, not a passing one.
    finished = bool(SUMMARY.search(out))
    return names, proc.returncode, finished


def main() -> int:
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *[str(p) for p in TARGETS]],
        capture_output=True, text=True, cwd=str(ROOT),
    ).stdout
    print(f"worktree state: {dirty.strip() or '(clean)'}", flush=True)

    caught, missed, skipped = [], [], []
    for i, (path, name, find, repl, owner) in enumerate(MUTATIONS, 1):
        tag = f"[{i:2}/{len(MUTATIONS)}]"
        src = ORIGINAL[path]
        if find not in src:
            print(f"{tag} SKIP (pattern missing): {name}", flush=True)
            skipped.append(name)
            continue
        if src.count(find) != 1:
            print(f"{tag} SKIP (pattern not unique, {src.count(find)}x): {name}", flush=True)
            skipped.append(name)
            continue
        path.write_text(src.replace(find, repl))
        names, rc, finished = run_suite()
        restore()
        if not finished:
            print(f"{tag} CRASHED (rc={rc}): {name}", flush=True)
            skipped.append(name)
        elif owner in names:
            extra = sorted(names - {owner})
            note = f"  (+{len(extra)} more: {', '.join(extra[:3])})" if extra else ""
            print(f"{tag} caught by {owner}{note}", flush=True)
            caught.append(name)
        elif names:
            print(
                f"{tag} WRONG OWNER: {name}\n"
                f"          expected {owner}, got {sorted(names)}",
                flush=True,
            )
            missed.append(name)
        else:
            print(f"{tag} MISSED (nothing failed): {name}", flush=True)
            missed.append(name)

    print(f"\ncaught {len(caught)}/{len(MUTATIONS)}  missed {len(missed)}  skipped {len(skipped)}")
    for n in missed:
        print(f"  MISSED: {n}")
    for n in skipped:
        print(f"  SKIPPED: {n}")
    print("SWEEP COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
