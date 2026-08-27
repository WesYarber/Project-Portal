"""Whether a front-end edit changed anything a browser could render (inert.py).

The case that made the module: on 2026-08-16 a run corrected "pronouns" to
"gender" inside two CSS comments and the portal told Wes it had "shipped UI
blind". These tests hold the line in both directions, and the second direction
is the one that matters - inert.py exists to make the nudge quieter, and a bug
here would make it silent.
"""
from __future__ import annotations

import pytest

from app import inert

# --- the real case ----------------------------------------------------------


def test_a_css_comment_edit_changes_nothing():
    """The exact shape of the false alarm: a word swapped inside `/* ... */`."""
    before = (
        "/* The pronouns dropdown is sized through its `.sel` wrapper. */\n"
        '.person-head input[type="text"] { width: auto; }\n'
    )
    after = (
        "/* The gender dropdown is sized through its `.sel` wrapper. */\n"
        '.person-head input[type="text"] { width: auto; }\n'
    )
    assert inert.changes_the_look(before, after, "app/static/style.css") is False


def test_a_css_property_edit_changes_the_look():
    before = ".sel { width: auto; }\n"
    after = ".sel { width: 100%; }\n"
    assert inert.changes_the_look(before, after, "app/static/style.css") is True


def test_a_selector_edit_changes_the_look():
    assert inert.changes_the_look(".a { color: red }", ".b { color: red }", "x.css") is True


def test_identical_text_is_never_a_change():
    assert inert.changes_the_look("body{}", "body{}", "x.css") is False


def test_identical_text_is_not_a_change_even_with_no_verdict():
    """Byte-equal beats every other rule, including "an unknown suffix renders".
    Nothing can have moved on screen if nothing moved in the file."""
    assert inert.changes_the_look("x = 1\n", "x = 1\n", "app/worker.py") is False
    assert inert.changes_the_look("/* open", "/* open", "x.css") is False


# --- comments behave like whitespace, not like nothing -----------------------


def test_deleting_a_comment_between_tokens_leaves_the_space_behind():
    """`a/*c*/b` and `a b` are the same two tokens; calling them different would
    nag on every comment anyone ever deletes."""
    assert inert.changes_the_look("a/*c*/b", "a b", "x.css") is False


def test_deleting_a_comment_that_joins_two_tokens_does_change_it():
    """`ab` is one token where `a/*c*/b` was two - a real change, and the one
    direction where treating a comment as whitespace still speaks up."""
    assert inert.changes_the_look("a/*c*/b", "ab", "x.css") is True


def test_reindenting_changes_nothing():
    """The everyday reformat: same tokens, different amounts of the whitespace
    that was already between them."""
    before = ".a {\n    color: red;\n}\n\n\n.b { color: blue; }\n"
    after = ".a {\n  color: red;\n}\n.b {\n  color: blue;\n}\n"
    assert inert.changes_the_look(before, after, "x.css") is False


def test_unminifying_is_not_recognized_as_inert():
    """A documented limit. Whitespace that was never there cannot be collapsed
    away, so `.a{color:red}` and `.a { color: red }` do not compare equal -
    telling them apart needs a real CSS tokenizer, and rule 1 says an unsure
    scanner should nag rather than acquire one."""
    assert inert.changes_the_look(".a{color:red}", ".a { color: red }", "x.css") is True


def test_adding_a_whole_comment_block_changes_nothing():
    before = ".a { color: red }\n"
    after = "/* why this is red:\n   because Wes asked. */\n.a { color: red }\n"
    assert inert.changes_the_look(before, after, "x.css") is False


# --- CSS is not Sass: `//` is not a comment there ---------------------------


def test_a_protocol_relative_url_is_not_a_css_comment():
    """`url(//cdn/a.png)` is a real stylesheet URL. Reading `//` as a comment
    would swallow it and call swapping the CDN inert."""
    before = ".a { background: url(//cdn-one/x.png); }\n"
    after = ".a { background: url(//cdn-two/x.png); }\n"
    assert inert.changes_the_look(before, after, "x.css") is True


def test_a_line_comment_is_a_comment_in_scss():
    before = "// the old note\n.a { color: red }\n"
    after = "// a different note\n.a { color: red }\n"
    assert inert.changes_the_look(before, after, "x.scss") is False


def test_a_line_comment_is_a_comment_in_less():
    before = "// old\n.a { color: red }\n"
    after = "// new\n.a { color: red }\n"
    assert inert.changes_the_look(before, after, "x.less") is False


def test_scss_still_notices_a_real_change_beside_its_line_comment():
    before = "// note\n.a { color: red }\n"
    after = "// note\n.a { color: blue }\n"
    assert inert.changes_the_look(before, after, "x.scss") is True


def test_indented_sass_gets_no_verdict():
    """Its whitespace IS its block structure, so collapsing it could make two
    different stylesheets compare equal - the one error this must not make."""
    assert inert.renderable("a\n  color: red\n", ".sass") is None
    assert inert.changes_the_look("a\n  color: red", "a\ncolor: red", "x.sass") is True


# --- JavaScript -------------------------------------------------------------


def test_a_js_line_comment_edit_changes_nothing():
    before = "// This proves the old thing about the field.\nblur();\n"
    after = "// This proves the new thing about the field.\nblur();\n"
    assert inert.changes_the_look(before, after, "app.js") is False


def test_a_js_code_edit_changes_the_look():
    before = "// note\nel.style.width = '10px';\n"
    after = "// note\nel.style.width = '20px';\n"
    assert inert.changes_the_look(before, after, "app.js") is True


def test_text_inside_a_string_is_kept_verbatim():
    """Whitespace inside a literal is content: it is words on the screen."""
    assert inert.changes_the_look('t("Add  a todo")', 't("Add a todo")', "app.js") is True


def test_changing_a_label_changes_the_look():
    assert inert.changes_the_look('t("Ask")', 't("Ask project")', "app.js") is True


def test_a_comment_marker_inside_a_string_is_not_a_comment():
    before = 'var u = "http://one/x"; var w = 1;\n'
    after = 'var u = "http://two/x"; var w = 1;\n'
    assert inert.changes_the_look(before, after, "app.js") is True


def test_a_regex_holding_slashes_does_not_start_a_comment():
    """`.replace(/\\/\\//g, "")` has a `//` inside a regex literal. Reading it as
    a line comment would swallow the rest of the line in both revisions and call
    a real edit there inert."""
    before = 'var a = s.replace(/\\/\\//g, "") + "one";\n'
    after = 'var a = s.replace(/\\/\\//g, "") + "two";\n'
    assert inert.changes_the_look(before, after, "app.js") is True


def test_a_regex_holding_backticks_does_not_start_a_template():
    """This is app/static/app.js's real MD_INLINE. If the backticks inside it
    opened a template literal the scanner would run to the next backtick in the
    file and lose everything between."""
    src = "var MD_INLINE = /(`+)([^`]+?)\\1|\\*\\*([^*\\s])\\*\\*/g;\nvar w = %s;\n"
    assert inert.changes_the_look(src % "1", src % "2", "app.js") is True
    assert inert.renderable(src % "1", ".js") is not None


def test_a_regex_after_return_is_a_regex():
    """`return /^#/` - prev char is a letter, so only the keyword tells us."""
    before = 'function f() { return /^#/.test(x) ? "a" : "b"; }\n'
    after = 'function f() { return /^#/.test(x) ? "c" : "b"; }\n'
    assert inert.changes_the_look(before, after, "app.js") is True


def test_a_slash_in_a_character_class_does_not_close_the_regex():
    """The spacing inside `[/ ]` is part of the pattern - it matches a space.
    End the literal at that slash and the rest becomes code, where collapsing
    whitespace quietly turns two different patterns into one."""
    before = "var a = /[/  ]/.test(s);\n"
    after = "var a = /[/ ]/.test(s);\n"
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is True


def test_division_is_not_read_as_a_regex():
    before = "var r = (a) / b; var c = 1;\n"
    after = "var r = (a) / b; var c = 2;\n"
    assert inert.changes_the_look(before, after, "app.js") is True


def test_an_escaped_quote_does_not_end_a_string():
    """Without the escape rule the scanner leaves the string at the `\\"` and
    opens a new one at the real closing quote, after which everything is inside
    out - here the second "string" runs off the line and the whole file loses
    its verdict, so a comment edit stops reading as inert."""
    before = 'var a = "x\\"" + y; // one\nvar b = 1;\n'
    after = 'var a = "x\\"" + y; // two\nvar b = 1;\n'
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is False


def test_a_string_unterminated_at_the_end_of_the_file_gets_no_verdict():
    assert inert.renderable('var a = "never closed', ".js") is None


def test_a_string_running_past_its_line_does_not_borrow_a_later_quote():
    """The newline rule earns its place only when there is a later quote to
    close on *and* the count comes out even - here the second quote is inside a
    comment. Without the rule the scanner swallows both lines into one literal
    and hands back a confident, wrong verdict instead of admitting it is lost.
    """
    assert inert.renderable('var a = "oops\nvar b = 2; // "\nvar c = 1;\n', ".js") is None


def test_a_regex_unterminated_at_the_end_of_the_file_gets_no_verdict():
    assert inert.renderable("var a = (/never closed", ".js") is None


def test_a_slash_after_a_string_is_division_not_a_regex():
    """A closing quote ends a value, so the next slash divides. If the scanner
    remembered `/` itself as the preceding character it would arm that slash to
    open a regex - which then closes on the first `/` of the `//` below, and the
    comment stops being a comment."""
    before = 'var a = "x" / 2; // one\n'
    after = 'var a = "x" / 2; // two\n'
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is False


def test_a_slash_after_a_regex_is_division_not_a_new_regex():
    before = 'var a = x.replace(/b/g, "") / 2; // one\n'
    after = 'var a = x.replace(/b/g, "") / 2; // two\n'
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is False


def test_a_nested_template_inside_a_substitution_is_walked():
    """The inner backticks must not be read as closing the outer literal.

    The two sources differ only in spacing *inside* the nested template, which
    is content and must survive. Lose the substitution walk and the outer
    literal ends at the inner backtick, that spacing becomes ordinary code
    whitespace, and collapsing it makes two different files look identical.
    """
    before = "var a = `p${ x + `  r  ` }s`;\n"
    after = "var a = `p${ x + ` r ` }s`;\n"
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is True


def test_a_brace_inside_a_substitutions_string_does_not_unbalance_it():
    """`"}"` is a string, not the end of the substitution - counting it would
    close the walk early and, again, spill the nested template's spacing out
    into code."""
    before = 'var a = `p${ f("}") + `  r  ` }s`;\n'
    after = 'var a = `p${ f("}") + ` r ` }s`;\n'
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is True


def test_a_nested_object_literal_inside_a_substitution_is_walked():
    """Same failure from the other direction: without counting depth, the `}`
    of `{q:1}` closes the substitution instead of the object."""
    before = "var a = `p${ {q:1} + `  r  ` }s`;\n"
    after = "var a = `p${ {q:1} + ` r ` }s`;\n"
    assert inert.renderable(before, ".js") is not None
    assert inert.changes_the_look(before, after, "app.js") is True


def test_a_brace_inside_a_nested_template_does_not_unbalance_it():
    """And skipping the nested template whole is what stops its `}` from being
    counted as the one that closes the substitution."""
    src = "var a = `x${ f(`a}b`) }y`; var z = %s;\n"
    assert inert.renderable(src % "1", ".js") is not None
    assert inert.changes_the_look(src % "1", src % "2", "app.js") is True


def test_a_template_literal_keeps_its_spacing():
    assert inert.changes_the_look("var a = `x  y`;", "var a = `x y`;", "app.js") is True


def test_a_template_substitution_with_braces_and_strings_is_walked():
    src = 'var a = `n=${ {k: "}"}.k }` + %s;\nvar z = 1;\n'
    assert inert.renderable(src % "1", ".js") is not None
    assert inert.changes_the_look(src % "1", src % "2", "app.js") is True


def test_a_comment_inside_a_template_substitution_is_content_not_a_comment():
    """Everything between the backticks is the literal, so nothing in it is
    stripped - including something that merely looks like a comment."""
    before = "var a = `${x} // one`;"
    after = "var a = `${x} // two`;"
    assert inert.changes_the_look(before, after, "app.js") is True


@pytest.mark.parametrize("suffix", [".ts", ".tsx", ".jsx", ".mjs", ".cjs"])
def test_every_js_family_suffix_strips_comments(suffix):
    assert inert.changes_the_look("// a\nx();", "// b\nx();", f"f{suffix}") is False


# --- HTML and Jinja ---------------------------------------------------------


def test_an_html_comment_edit_changes_nothing():
    before = "<!-- the old note -->\n<h1>hi</h1>\n"
    after = "<!-- a new note -->\n<h1>hi</h1>\n"
    assert inert.changes_the_look(before, after, "index.html") is False


def test_a_jinja_comment_edit_changes_nothing():
    """base.html alone carries thirty of these, and a spelling sweep across the
    tree edited three templates' comments and nothing else."""
    before = "{# the two O glyphs are tinted independently #}\n<div class='banner'></div>\n"
    after = "{# the two O glyphs are colored independently #}\n<div class='banner'></div>\n"
    assert inert.changes_the_look(before, after, "app/templates/_banner.html") is False


def test_visible_template_text_still_changes_the_look():
    """The other half of that same spelling sweep: the word inside the <span> is
    on the screen, not in a comment. (The real pair was a British spelling and
    its correction; this file cannot spell it the old way without tripping
    tests/test_american_english.py, which is working as intended.)"""
    before = "<span class='stat'>{{ n }}stopped</span>\n"
    after = "<span class='stat'>{{ n }}canceled</span>\n"
    assert inert.changes_the_look(before, after, "app/templates/activity.html") is True


def test_a_jinja_tag_is_not_a_jinja_comment():
    before = "{% if a %}<b>x</b>{% endif %}"
    after = "{% if b %}<b>x</b>{% endif %}"
    assert inert.changes_the_look(before, after, "x.html") is True


def test_an_attribute_edit_changes_the_look():
    before = '<div class="card"></div>'
    after = '<div class="card wide"></div>'
    assert inert.changes_the_look(before, after, "x.html") is True


def test_html_whitespace_between_tags_is_collapsed():
    assert inert.changes_the_look("<b>x</b>\n<i>y</i>", "<b>x</b>   <i>y</i>", "x.html") is False


@pytest.mark.parametrize("suffix", [".htm", ".vue", ".svelte"])
def test_every_html_family_suffix_strips_html_comments(suffix):
    assert inert.changes_the_look("<!-- a -->\n<b>x</b>", "<!-- b -->\n<b>x</b>", f"f{suffix}") is False


def test_a_js_comment_inside_an_html_script_block_is_not_stripped():
    """Documented limit: only HTML and Jinja comments come out of an HTML file,
    so an inline-script comment edit still asks for its screenshot. Erring
    towards speaking is the whole design."""
    before = "<script>\n// one\nx();\n</script>"
    after = "<script>\n// two\nx();\n</script>"
    assert inert.changes_the_look(before, after, "x.html") is True


# --- no verdict means "it renders" ------------------------------------------


def test_an_unterminated_block_comment_gets_no_verdict():
    assert inert.renderable("/* never closed\n.a{}", ".css") is None
    assert inert.changes_the_look("/* one\n.a{}", "/* two\n.a{}", "x.css") is True


def test_an_unterminated_html_comment_gets_no_verdict():
    assert inert.renderable("<!-- never closed\n<b>x</b>", ".html") is None


def test_an_unterminated_jinja_comment_gets_no_verdict():
    assert inert.renderable("{# never closed\n<b>x</b>", ".html") is None


def test_a_string_running_off_its_line_gets_no_verdict():
    assert inert.renderable('var a = "never closed\nvar b = 1;\n', ".js") is None


def test_an_unterminated_template_gets_no_verdict():
    assert inert.renderable("var a = `never closed;\n", ".js") is None


def test_an_unterminated_substitution_gets_no_verdict():
    assert inert.renderable("var a = `${ never closed`;\n", ".js") is None


def test_an_edit_that_makes_a_file_unscannable_changes_the_look():
    """Only the *after* side loses its verdict here, so this is the one case
    that proves both sides are actually checked."""
    before = ".a { color: red }\n"
    after = "/* unterminated\n.a { color: red }\n"
    assert inert.renderable(before, ".css") is not None
    assert inert.renderable(after, ".css") is None
    assert inert.changes_the_look(before, after, "x.css") is True


def test_an_unknown_suffix_gets_no_verdict():
    assert inert.renderable("# a comment\nx = 1\n", ".py") is None
    assert inert.changes_the_look("# a\nx=1", "# b\nx=1", "app/worker.py") is True


def test_a_file_too_large_to_scan_gets_no_verdict():
    """A bundle this size is a build artifact, and scanning one buys nothing."""
    big = "/* c */" + "a" * inert.MAX_SCAN_BYTES
    assert inert.renderable(big, ".css") is None
    assert inert.changes_the_look(big + "1", big + "2", "bundle.css") is True


def test_a_file_just_under_the_cap_is_still_scanned():
    body = "/* c */\n.a { color: red }\n"
    pad = "/* %s */\n" % ("x" * (inert.MAX_SCAN_BYTES - len(body) - 100))
    assert inert.renderable(pad + body, ".css") is not None


def test_the_suffix_is_matched_case_insensitively():
    assert inert.changes_the_look("/* a */\n.x{}", "/* b */\n.x{}", "STYLE.CSS") is False


def test_a_file_with_no_extension_gets_no_verdict():
    assert inert.renderable("/* a */", "") is None


# --- the normalized form itself ---------------------------------------------


def test_renderable_drops_comments_and_collapses_code_whitespace():
    got = inert.renderable("/* note */\n.a  {\n  color:   red\n}\n", ".css")
    assert got == ".a { color: red }"


def test_renderable_of_a_comment_only_file_is_empty():
    assert inert.renderable("/* just a note */\n", ".css") == ""
    assert inert.renderable("// just a note\n", ".js") == ""
    assert inert.renderable("<!-- just a note -->\n", ".html") == ""
