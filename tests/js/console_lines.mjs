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

// Just enough DOM for spans in a <pre>: a class, some text, and children.
function makeElement() {
  return {
    className: "",
    textContent: "",
    dataset: {},
    childNodes: [],
    get lastChild() {
      return this.childNodes[this.childNodes.length - 1] || null;
    },
    appendChild(child) {
      this.childNodes.push(child);
      return child;
    },
    removeChild(child) {
      const i = this.childNodes.indexOf(child);
      if (i < 0) throw new Error("removeChild: not a child");
      this.childNodes.splice(i, 1);
      return child;
    },
  };
}

globalThis.document = {
  createElement() {
    return makeElement();
  },
};

const api = new Function(src + "; return { consoleKind, renderConsole };")();

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

console.log(JSON.stringify(result, null, 2));
