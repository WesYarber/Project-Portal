---
name: terminal-style
description: Apply $OWNERS dark terminal theme (dark CRT look - Fira Code, terminal window chrome, bracketed badges, ANSI accent colours) to a web page you are building. $OWNER likes this look and packaged it to reuse (the Project Portal follows it too); reach for it whenever a project needs a UI theme and none other has been asked for.
---

# terminal-style

$OWNERS dark terminal theme, packaged as a single reusable stylesheet so the
things $THEIR agents build can share one visual language: a dark CRT terminal
window, Fira Code, `[bracketed]` badges, ANSI accent colours. It is a theme
in its own right - the Project Portal just happens to follow it too.

## Getting the stylesheet

Copy the current template into your workspace (it is kept in sync, so fetch
it fresh rather than committing to memory):

```bash
cp $PORTAL_ROOT/app/static/terminal-theme.css .
```

It is also served at `$BASE_URL/static/terminal-theme.css`, and a live
gallery of every component - with the page skeleton to copy - is at
`$BASE_URL/style`. (Those are the URLs to hand over too: never give a
`localhost` or `127.0.0.1` address, which is always a dead link on the
device actually reading it.)

## Using it

One CSS file, no JS, no build step. Pair it with the Fira Code webfont link
(see the header of the CSS file, or the /style page's starter skeleton - it
falls back to system monospace without it). The skeleton the components
assume:

```html
<div class="screen">
  <div class="terminal-window">
    <div class="terminal-header"> ... window dots + title ... </div>
    <div class="terminal-body">   ... your content ...
      <div class="terminal-footer"> ... </div>
    </div>
  </div>
</div>
```

What you get: `h2` section headers with the `//` prefix, `.badge` with
`.accent-green` / `.accent-yellow` / etc, `.btn` (+ `.go`, `.danger`,
`.secondary`, `.small`), themed inputs/selects/textareas at one shared
height, `.panel`, `.banner-alert`, `.scroll-cap` for anything that can grow
without bound, `.dot.ok/.warn/.err` indicators, `.nav-tabs`, and an optional
`scan-all` body class for CRT scanlines.

Two rules of the house style worth keeping even if you diverge: no
hover-only controls (the reader is often on a phone), and cap any panel that can
grow without bound rather than letting it push the page down.
