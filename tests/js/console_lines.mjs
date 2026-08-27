// Runs the real transcript renderer out of app/static/app.js against a stub DOM
// and prints what it drew, as JSON.
//
// Wes, 2026-07-28: "For the agent console (or any of the live or non-live agent
// transcript views), please improve the readability of it. For example, make
// all the tool calls and '>' lines show as indented gray/dimmed text with the
// other lines where the agent is just talking or thinking show as
// non-indented text that is not dimmed."
//
// String-matching for "cl-tool" would only prove the file contains the word.
// This proves the behavior: which class each kind of line actually gets, that
// markdown prose is not mistaken for machinery, and - the part no static check
// could reach - that a chunk arriving split across two polls ends up as one
// correctly classified line rather than two wrong ones.
//
// Called by tests/test_console.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

function slice(from, to, what) {
  const start = appjs.indexOf(from);
  const end = to ? appjs.indexOf(to) : appjs.length;
  if (start < 0 || end < 0 || end <= start) {
    throw new Error("could not find the " + what + " section in app.js");
  }
  return appjs.slice(start, end);
}

const src = slice("var CONSOLE_KINDS", "function startConsolePoll", "console renderer");

// Just enough DOM for spans and buttons in a <pre>: a class, some text,
// children, and enough of a parent link for the fold to take a line back off
// again when a split line arrives whole.
function makeElement(tag = "span") {
  let ownText = "";
  const el = {
    tagName: tag.toUpperCase(),
    nodeType: 1,
    className: "",
    hidden: false,
    // Real-DOM semantics, which the markdown renderer now leans on: reading
    // textContent concatenates the children when there are any, and writing it
    // drops them.
    get textContent() {
      if (el.childNodes.length) {
        return el.childNodes.map((c) => c.textContent).join("");
      }
      return ownText;
    },
    set textContent(v) {
      ownText = v;
      el.childNodes.length = 0;
    },
    dataset: {},
    childNodes: [],
    parentNode: null,
    classList: {
      contains(name) {
        return (" " + el.className + " ").indexOf(" " + name + " ") !== -1;
      },
      add(name) {
        if (!this.contains(name)) el.className = (el.className + " " + name).trim();
      },
      remove(name) {
        el.className = (" " + el.className + " ")
          .replace(" " + name + " ", " ")
          .trim();
      },
      toggle(name, on) {
        if (on === undefined) on = !this.contains(name);
        if (on) this.add(name);
        else this.remove(name);
      },
    },
    setAttribute() {},
    get lastChild() {
      return this.childNodes[this.childNodes.length - 1] || null;
    },
    get nextSibling() {
      if (!el.parentNode) return null;
      const kids = el.parentNode.childNodes;
      return kids[kids.indexOf(el) + 1] || null;
    },
    appendChild(child) {
      child.parentNode = el;
      el.childNodes.push(child);
      return child;
    },
    removeChild(child) {
      const i = el.childNodes.indexOf(child);
      if (i < 0) throw new Error("removeChild: not a child");
      el.childNodes.splice(i, 1);
      child.parentNode = null;
      return child;
    },
    querySelector(sel) {
      const want = sel.replace(".", "");
      for (const kid of el.childNodes) {
        if (kid.classList && kid.classList.contains(want)) return kid;
      }
      return null;
    },
  };
  return el;
}

// The fold's on/off switch lives here, so a scenario can render the same
// transcript both ways.
const store = {};
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => {
    store[k] = String(v);
  },
};

globalThis.document = {
  createElement(tag) {
    return makeElement(tag);
  },
  createTextNode(text) {
    return { nodeType: 3, textContent: text, parentNode: null };
  },
  // The fold's open/shut click is delegated off document; nothing here fires
  // events, so registering one is all that has to work.
  addEventListener() {},
  getElementById: () => null,
};

const api = new Function(
  src +
    "; return { consoleKind, renderConsole, consoleFoldLabel, consoleText };",
)();

// Everything below the fold section renders with the machinery SHOWN unless a
// scenario says otherwise, so the pre-existing expectations still describe what
// they always described.
store["portal-console-tools"] = "0";

// A <pre> whose textContent = "" must also drop its children, the way a real
// one does - the renderer relies on that to start a transcript over.
function makeOut() {
  const el = makeElement();
  let text = "";
  Object.defineProperty(el, "textContent", {
    get: () => text,
    set: (v) => {
      text = v;
      el.childNodes.length = 0;
    },
  });
  return el;
}

function drawn(out) {
  return out.childNodes.map((c) => ({
    kind: c.className.replace("cl cl-", ""),
    text: c.textContent,
  }));
}

const result = {};

// 1. Every kind of line the log can hold, classified.
result.kinds = {};
for (const line of [
  "> Bash(git status)",
  "< ok (12 lines)",
  "! error: something broke",
  "* session start  model=claude-opus-5  tools=19",
  "~ I should check the tests first",
  "Let me look at the settings page.",
  "",
  "    def foo():", // an indented code line inside prose
  " > a markdown quote, escaped by runlog.escape_prose",
  " * a markdown bullet, escaped the same way",
  "*", // a bare marker with nothing after it
  "*not a status line, no space after the star",
  "![a screenshot](shots/x.png)", // markdown image: '!' then '[', not a marker
]) {
  result.kinds[JSON.stringify(line)] = api.consoleKind(line);
}

// 2. A whole transcript drawn in one go.
{
  const out = makeOut();
  api.renderConsole(out, "* session start\nLet me look.\n> Read(app/main.py)\n< ok (3 lines)\n", true);
  result.wholeTranscript = drawn(out);
}

// 3. The same transcript arriving in two polls, split mid-line. The tail is
//    drawn immediately (so the console never lags its run) and then redrawn.
{
  const out = makeOut();
  api.renderConsole(out, "Let me look.\n> Read(app/ma", true);
  result.afterFirstPoll = drawn(out);
  api.renderConsole(out, "in.py)\n< ok (3 lines)\n", false);
  result.afterSecondPoll = drawn(out);
}

// 4. A split that lands between a marker and its space, which is the case that
//    would misclassify if the tail were committed rather than redrawn.
{
  const out = makeOut();
  api.renderConsole(out, "*", true);
  api.renderConsole(out, " run complete  (7 turns)\n", false);
  result.afterMarkerSplit = drawn(out);
}

// 5. `replace` starts over rather than appending to what was there.
{
  const out = makeOut();
  api.renderConsole(out, "> Read(a)\n", true);
  api.renderConsole(out, "> Read(b)\n", true);
  result.afterReplace = drawn(out);
}

// 6. A paragraph of reasoning: runlog writes one "~ " per SOURCE line, so a
//    wrapped thought arrives as several marked lines in a row. Drawn, they
//    must not leave a column of tildes down the left - while the machinery
//    around them keeps the markers that say which way a call went.
{
  const out = makeOut();
  api.renderConsole(
    out,
    "~ The settings page is 500ing. That smells like a template\n" +
      "~ meeting a handler that has moved on, so the first thing to\n" +
      "~ check is whether it was rendered since the last restart.\n" +
      "> Read(app/main.py)\n" +
      "< ok (3 lines)\n",
    true,
  );
  result.thinkingParagraph = drawn(out);
}

// ---------------------------------------------------------------------------
// Folding the machinery. Wes, 2026-08-01: "have an option that compressed
// down/hides and prints a sort of summary (but not using AI to summarize) of
// what commands have been run and whatnot in between the white text it sends."
// ---------------------------------------------------------------------------

function folded(out) {
  return out.childNodes.map((c) =>
    c.classList.contains("cl-fold")
      ? {
          kind: "fold",
          text: c.childNodes[0].textContent,
          hidden: c.childNodes[1].hidden,
          lines: c.childNodes[1].childNodes.map((l) => l.textContent),
        }
      : { kind: c.className.replace("cl cl-", ""), text: c.textContent },
  );
}

store["portal-console-tools"] = "1";

// 7. The shape of a folded transcript: prose in the clear, the calls between
//    two paragraphs collapsed to one line that counts them.
{
  const out = makeOut();
  api.renderConsole(
    out,
    "Let me look at the settings page.\n" +
      "> Read(app/main.py)\n" +
      "< ok (40 lines)\n" +
      "> Bash(pytest -q)\n" +
      "< ok (3 lines)\n" +
      "> Bash(git status)\n" +
      "< ok (1 line)\n" +
      "Three tests fail.\n",
    true,
  );
  result.foldedTranscript = folded(out);
}

// 8. What never folds, whatever the setting: status lines (a fact about the
//    run) and prose. Errors DO fold as of Wes, 2026-08-06 ("hide these errored
//    lines ... inside the tool call collapsed sections") - they ride the same
//    group as the calls around them, and the head counts them.
{
  const out = makeOut();
  api.renderConsole(
    out,
    "* session start  model=claude-opus-5\n" +
      "> Bash(pytest -q)\n" +
      "! error: pytest: command not found\n" +
      "> Read(a)\n" +
      "~ that is not right\n",
    true,
  );
  result.neverFolded = folded(out);
}

// 8b. A fold holding ONLY an error - a failure between two paragraphs of
//     prose, no calls beside it. The head must say "1 error", not claim a
//     line of tool output that is really the failure.
{
  const out = makeOut();
  api.renderConsole(
    out,
    "trying something\n" +
      "! error: the workspace fence refused the write\n" +
      "adjusting\n",
    true,
  );
  result.errorOnlyFold = folded(out);
}

// 9. A chunk split mid-line, folded. The partial line is drawn (and counted),
//    then taken back off and redrawn whole - so the count must not double.
{
  const out = makeOut();
  api.renderConsole(out, "> Bash(pyte", true);
  result.foldMidSplit = folded(out);
  api.renderConsole(out, "st -q)\n< ok (3 lines)\n", false);
  result.foldAfterSplit = folded(out);
}

// 10. A split that empties the fold again: the group must go with its last
//     line rather than leaving an empty "0 tools called" heading.
{
  const out = makeOut();
  api.renderConsole(out, "hello\n> Bas", true);
  const mid = folded(out).length;
  api.renderConsole(out, "h(x)\n", false);
  result.foldEmptied = { midCount: mid, after: folded(out) };
}

// 10b. The same, where what arrives is NOT another tool line. This is the case
//      that actually proves the group is removed: in scenario 10 the redraw
//      puts a tool line straight back into the same group, so leaving the empty
//      wrapper in the DOM produces an identical result and deleting the removal
//      broke no test (found by the delete-the-fix sweep, 2026-08-01).
//
//      "<" alone is a result marker, so it opens a fold; completed as
//      "<not a marker" it is ordinary prose, and the fold it opened has to go
//      with it rather than sit there heading nothing.
{
  const out = makeOut();
  api.renderConsole(out, "hello\n<", true);
  const mid = folded(out);
  api.renderConsole(out, "not a marker after all\n", false);
  result.foldEmptiedByProse = { mid: mid, after: folded(out) };
}

// 11. The label itself, which is the whole of what Wes asked to see.
result.labels = [
  api.consoleFoldLabel({ bash: 5, tool: 0, lines: 5 }),
  api.consoleFoldLabel({ bash: 0, tool: 10, lines: 10 }),
  api.consoleFoldLabel({ bash: 1, tool: 1, lines: 2 }),
  api.consoleFoldLabel({ bash: 3, tool: 7, lines: 10 }),
  api.consoleFoldLabel({ bash: 0, tool: 0, lines: 4 }),
  // Errors are the head painter's span, not the label's text - the label only
  // has to keep them out of the "lines of tool output" arithmetic.
  api.consoleFoldLabel({ bash: 0, tool: 0, error: 2, lines: 5 }),
  api.consoleFoldLabel({ bash: 0, tool: 0, error: 2, lines: 2 }),
];

// 12. Reading the transcript back out of a folded console, which is how the
//     toggle redraws it without re-fetching. The hidden lines have to come
//     back, and the headings must not.
{
  const out = makeOut();
  api.renderConsole(out, "hello\n> Bash(x)\n< ok (1 line)\nbye\n", true);
  result.foldedTextBack = api.consoleText(out);
}

// 13. ...including thinking, which is DRAWN without its "~ " marker (one per
//     source line would be a column of tildes down the page). Read back off the
//     drawn text, a paragraph of reasoning would come home as prose and be
//     redrawn as something the agent said out loud.
{
  const out = makeOut();
  api.renderConsole(out, "~ let me check the tests\n> Bash(x)\nhello\n", true);
  result.thinkingTextBack = api.consoleText(out);
}

// 14. Inline markdown in prose. Wes, 2026-08-04: "These are some lines from
//     the agent console that just render in plain text and not in the sort of
//     markdown that is intended" - pasting a **bold** lead sentence and a
//     numbered list of **bold** items. Serialized as the child nodes, because
//     what is being asserted is exactly which spans of text got which styling.
function inline(el) {
  return el.childNodes.map((c) =>
    c.nodeType === 3
      ? { t: c.textContent }
      : { cls: c.className, t: c.textContent, kids: inline(c) },
  );
}

store["portal-console-tools"] = "0"; // machinery shown: this is about prose

{
  const out = makeOut();
  api.renderConsole(
    out,
    "**My own 540→550 change was wrong.** `terminal-theme.css` is reusable.\n" +
      "## The fix\n" +
      "2. **Two gaps** in the guard.\n" +
      " * a bullet with **bold** in it\n" +
      "a ** b that is not bold ** c\n" +
      "**`style.css:199` inside bold**\n" +
      "> Bash(echo **not markdown**)\n",
    true,
  );
  result.markdown = out.childNodes.map((c) => ({
    kind: c.className,
    text: c.textContent,
    kids: inline(c),
  }));
}

console.log(JSON.stringify(result, null, 2));
