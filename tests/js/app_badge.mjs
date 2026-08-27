// Runs the real syncAppBadge() out of app/static/app.js against a stub
// navigator, and prints what it did with the Badging API, as JSON.
//
// String-matching for "setAppBadge" would only prove app.js contains the word.
// What matters is which of the two calls it makes and with what: a count of 0
// must CLEAR the icon rather than set it to zero (setAppBadge(0) is specified
// to show a dot with no number on some platforms, which would leave a mark on
// the icon with nothing waiting), a missing attribute must do nothing at all
// rather than clear a badge a push had legitimately set, and none of it may
// throw in a browser that has no Badging API - which is every desktop Safari
// and every non-installed tab.
//
// Called by tests/test_push_badge_js.py.
import { readFileSync } from "node:fs";

const appjs = readFileSync(process.argv[2], "utf8");

const start = appjs.indexOf("function syncAppBadge()");
if (start < 0) throw new Error("syncAppBadge is not in app.js");
const end = appjs.indexOf("\n}", start) + 2;
const src = appjs.slice(start, end);

function run(attr, hasApi) {
  const calls = [];
  const navigator = hasApi
    ? {
        setAppBadge: (n) => {
          calls.push(["set", n]);
          return Promise.resolve();
        },
        clearAppBadge: () => {
          calls.push(["clear", null]);
          return Promise.resolve();
        },
      }
    : {};
  const document = {
    body: { getAttribute: (name) => (name === "data-open-questions" ? attr : null) },
  };
  let threw = null;
  try {
    // The real function, given only the two globals it touches.
    new Function("navigator", "document", src + "; return syncAppBadge();")(
      navigator,
      document
    );
  } catch (err) {
    threw = String(err && err.message);
  }
  return { calls, threw };
}

console.log(
  JSON.stringify({
    three: run("3", true),
    zero: run("0", true),
    missing: run(null, true),
    junk: run("not a number", true),
    negative: run("-2", true),
    noApi: run("3", false),
  })
);
