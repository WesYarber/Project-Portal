"""Did a front-end edit change anything a browser could actually render?

This exists because of a false alarm on Wes's own board. On 2026-08-16 a run
corrected the word "pronouns" to "gender" in two CSS *comments* - no selector,
no property, nothing a pixel could notice - and the portal answered with

    note: this run changed 1 front-end file but its report showed no screenshot.

which is untrue in the only sense that matters: nothing about the look changed,
so there was nothing to show. proof.py's own docstring names that failure -
"a nag that fires on almost every run stops being read" - and a nag that fires
on a comment typo is exactly how it gets there.

So this module answers one question about one file: **would a browser render
this differently?** It strips comments, collapses the whitespace between code
tokens, and hands back what is left. proof.py compares that for the two
revisions of a file; equal means the edit was inert and the nag stays quiet.

Three rules keep it honest, in rough order of importance:

1. **Uncertainty means "it renders."** Every scanner returns None the moment it
   loses the thread - an unterminated comment, a string running off the end of
   its line, a suffix it has no business guessing about. proof.py reads None as
   "assume it renders" and nags. Being wrong that way costs a note Wes can
   ignore; being wrong the other way silently drops the nudge the module exists
   to deliver, which is how a discipline decays. The whole cost model here is
   *a courtesy note appears or does not appear* - so every tie goes to speaking.

2. **String and template literals are kept verbatim.** They are the one place
   inside code where whitespace is content: `"Add  a todo"` and `"Add a todo"`
   are different words on the screen. Everything outside a literal is collapsed;
   nothing inside one is touched.

3. **Comments count as whitespace, not as nothing.** `a/*c*/b` normalizes to
   `a b`, the same as deleting the comment and leaving the space behind. The C
   preprocessor has done it this way for fifty years and for the same reason:
   otherwise removing a comment looks like joining two tokens.

Two limits worth knowing before trusting a verdict, both deliberate:

- **Newlines collapse like any other whitespace**, so a JS edit that changes
  only the line break after a bare `return` - genuinely different code, thanks
  to automatic semicolon insertion - reads as inert here. Keeping newlines
  significant would fix that and cost far more: deleting a comment *line* is the
  commonest inert edit there is, and it changes the line count by definition.
  A contrived semicolon-insertion edit losing its screenshot nudge is the
  cheaper mistake.
- **Inline `<script>` and `<style>` in an HTML file get no comment stripping.**
  Only HTML and Jinja comments are removed there. Sniffing which language is
  inside which tag is a second scanner's worth of ways to be wrong, and rule 1
  says an unsure scanner should nag.
- **Whitespace is collapsed, never invented**, so an everyday reindent reads as
  inert but un-minifying `.a{color:red}` into `.a { color: red }` does not.
  Closing that gap means knowing where a token may be split, which is a real
  tokenizer per language; nobody reformats that way by hand, and rule 1 prefers
  the extra note to the extra machinery.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# Suffixes whose comment syntax this module knows. Anything outside these three
# families - including a UI suffix proof.py recognizes but this does not, such
# as `.sass` below - has no verdict, and no verdict means "it renders".
_HTML_SUFFIXES = frozenset({".html", ".htm", ".vue", ".svelte"})
_CSS_SUFFIXES = frozenset({".css", ".scss", ".less"})
_JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})

# `//` is a comment in Sass and Less; in plain CSS it is not a comment at all,
# and `background: url(//cdn/x.png)` is a protocol-relative URL that stripping
# it would eat. Only the dialects that actually have the syntax get it.
_CSS_LINE_COMMENT_SUFFIXES = frozenset({".scss", ".less"})

# `.sass` - the indented syntax - is deliberately absent from _CSS_SUFFIXES.
# Its whitespace IS its block structure, so collapsing it would make two
# genuinely different stylesheets normalize to the same string, which is the one
# error this module must never make (rule 1). Nobody here writes it; if that
# changes, it needs a scanner that keeps indentation significant, not a line on
# the frozenset above.
INDENTED_SUFFIXES = frozenset({".sass", ".styl"})

# Keywords after which a `/` opens a regular expression rather than dividing.
# `return /^#/` is real code in app/static/app.js, and reading that slash as
# division walks the scanner into a string it never leaves.
_REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    }
)

# Code characters after which a `/` opens a regular expression. The complement -
# an identifier character, `)`, `]` or `}` - ends a value, so a slash after one
# of those is division.
_REGEX_PRECEDING_CHARS = frozenset("(,=:[!&|?{};+-*%^~<>")

# Stands in for "a literal just closed" when tracking the previous significant
# character. Its whole contract is to be a character _REGEX_PRECEDING_CHARS does
# NOT contain, so that a slash following a string, template or regex reads as
# division. `"x" / 2` is ordinary arithmetic; let one of `(`, `=` or `,` stand in
# here instead and that slash opens a regex which runs to the next `/` in the
# file - usually the start of a comment - and takes everything between into a
# literal. `)` is the honest choice because a value really did just end.
_VALUE_ENDED = ")"

# Past this many characters a file is a build artifact or a vendored bundle, not
# something a person edited a comment in. Scanning one buys nothing and a
# pathological minified line could hold the git call open.
MAX_SCAN_BYTES = 2_000_000


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _end_of_quoted(text: str, start: int, quote: str) -> Optional[int]:
    """Index just past the closing quote of the literal opening at `start`.

    None if it runs off the end of the file or off the end of its line. Neither
    CSS nor JS allows a raw newline inside a `'` or `"` string, so hitting one
    means the scanner is not where it thinks it is - most likely it read a
    division slash as opening a regex - and rule 1 says stop guessing.
    """
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        if ch == "\n":
            return None
        i += 1
    return None


def _end_of_template(text: str, start: int) -> Optional[int]:
    """Index just past the backtick closing the template literal at `start`.

    `${...}` substitutions are walked with a brace counter, and quoted strings
    and nested templates inside one are skipped whole, so a brace inside a
    string cannot unbalance the count.
    """
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return i + 1
        if ch == "$" and i + 1 < n and text[i + 1] == "{":
            end = _end_of_substitution(text, i + 1)
            if end is None:
                return None
            i = end
            continue
        i += 1
    return None


def _end_of_substitution(text: str, brace: int) -> Optional[int]:
    """Index just past the `}` closing the `${` whose brace is at `brace`."""
    i = brace + 1
    n = len(text)
    depth = 1
    while i < n:
        ch = text[i]
        if ch in "\"'":
            end = _end_of_quoted(text, i, ch)
            if end is None:
                return None
            i = end
            continue
        if ch == "`":
            end = _end_of_template(text, i)
            if end is None:
                return None
            i = end
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _end_of_regex(text: str, start: int) -> Optional[int]:
    """Index just past the flags of the regex literal opening at `start`.

    A `/` inside a `[...]` character class does not close the literal, which is
    the rule that gets hand-rolled scanners wrong. A raw newline does not close
    it either - it means this was never a regex.

    The trailing flags are deliberately NOT consumed here: they fall through as
    ordinary code characters, which lands them in the output right where they
    already were, and no combination of the eight legal flag letters spells any
    of `_REGEX_PRECEDING_KEYWORDS` (all of those need a letter that is not a
    flag), so they cannot arm a following slash by accident either. A loop
    consuming them made no observable difference to anything - a mutation sweep
    could not find a single test that noticed its absence - so it is gone.
    """
    i = start + 1
    n = len(text)
    in_class = False
    closed = False
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "\n":
            return None
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "/":
            i += 1
            closed = True
            break
        i += 1
    if not closed:
        return None
    return i


class _Collector:
    """Accumulates the renderable text: code with its whitespace collapsed, and
    literals appended untouched (rule 2)."""

    def __init__(self) -> None:
        self._pieces: list[str] = []
        self._pending_space = False

    def gap(self) -> None:
        """Whitespace, or a comment - which counts as whitespace (rule 3).

        Recorded rather than written, so that runs of it collapse and a gap at
        the very end of the file never lands in the result.
        """
        if self._pieces:
            self._pending_space = True

    def add(self, piece: str) -> None:
        if self._pending_space:
            self._pieces.append(" ")
            self._pending_space = False
        self._pieces.append(piece)

    def text(self) -> str:
        return "".join(self._pieces)


def _scan_c_like(text: str, *, line_comments: bool, js: bool) -> Optional[str]:
    """Renderable text of a CSS- or JS-family file, or None if unscannable."""
    out = _Collector()
    i = 0
    n = len(text)
    prev = ""  # last significant code character, for the regex/division call
    word = ""  # identifier ending at `prev`, for `return /re/`
    while i < n:
        ch = text[i]
        if ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            out.gap()
            i = end + 2
            continue
        if line_comments and text.startswith("//", i):
            end = text.find("\n", i)
            out.gap()
            i = n if end < 0 else end
            continue
        if ch in "\"'":
            end = _end_of_quoted(text, i, ch)
            if end is None:
                return None
            out.add(text[i:end])
            prev, word = _VALUE_ENDED, ""
            i = end
            continue
        if js and ch == "`":
            end = _end_of_template(text, i)
            if end is None:
                return None
            out.add(text[i:end])
            prev, word = _VALUE_ENDED, ""
            i = end
            continue
        if js and ch == "/" and (not prev or prev in _REGEX_PRECEDING_CHARS or word in _REGEX_PRECEDING_KEYWORDS):
            end = _end_of_regex(text, i)
            if end is None:
                return None
            out.add(text[i:end])
            prev, word = _VALUE_ENDED, ""
            i = end
            continue
        if ch.isspace():
            out.gap()
            i += 1
            continue
        out.add(ch)
        prev = ch
        word = word + ch if _is_word_char(ch) else ""
        i += 1
    return out.text()


def _scan_html(text: str) -> Optional[str]:
    """Renderable text of an HTML-family file, or None if unscannable.

    Strips HTML comments and Jinja's `{# ... #}` - the portal's own templates
    carry thirty of the latter in `base.html` alone - and collapses whitespace,
    which is what a browser does to it anyway outside `<pre>`.
    """
    out = _Collector()
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end < 0:
                return None
            out.gap()
            i = end + 3
            continue
        if text.startswith("{#", i):
            end = text.find("#}", i + 2)
            if end < 0:
                return None
            out.gap()
            i = end + 2
            continue
        ch = text[i]
        if ch.isspace():
            out.gap()
        else:
            out.add(ch)
        i += 1
    return out.text()


def renderable(text: str, suffix: str) -> Optional[str]:
    """What a browser could render from `text`, normalized for comparison.

    None means "no verdict" - an unknown suffix, a file too large to be worth
    scanning, or a scanner that lost the thread. Callers must read None as
    "assume it renders" (rule 1), never as "nothing renders".
    """
    suffix = suffix.lower()
    if len(text) > MAX_SCAN_BYTES:
        return None
    if suffix in _HTML_SUFFIXES:
        return _scan_html(text)
    if suffix in _CSS_SUFFIXES:
        return _scan_c_like(
            text,
            line_comments=suffix in _CSS_LINE_COMMENT_SUFFIXES,
            js=False,
        )
    if suffix in _JS_SUFFIXES:
        return _scan_c_like(text, line_comments=True, js=True)
    return None


def changes_the_look(before: str, after: str, path: str) -> bool:
    """True if editing `path` from `before` to `after` changed what renders.

    True whenever there is no verdict, so a caller can use this directly without
    a third state to handle: an unknown suffix, an unscannable file or a real
    change all mean the same thing to the proof-shot nudge - speak up.
    """
    if before == after:
        return False
    suffix = Path(path).suffix
    before_render = renderable(before, suffix)
    if before_render is None:
        return True
    after_render = renderable(after, suffix)
    if after_render is None:
        return True
    return before_render != after_render
