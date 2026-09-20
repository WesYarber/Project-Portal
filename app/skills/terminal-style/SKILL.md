---
name: terminal-style
description: Apply $OWNERS dark terminal theme (dark CRT look - Fira Code, terminal window chrome, bracketed badges, ANSI accent colors) to a web page you are building. $OWNER likes this look and packaged it to reuse (the Project Portal follows it too); reach for it whenever a project needs a UI theme and none other has been asked for.
---

# terminal-style

$OWNERS dark terminal theme, packaged as a single reusable stylesheet so the
things $THEIR agents build can share one visual language: a dark CRT terminal
window, Fira Code, `[bracketed]` badges, ANSI accent colors. It is a theme
in its own right - the Project Portal just happens to follow it too.

## Getting the stylesheet

Copy the current template into your workspace (it is kept in sync, so fetch
it fresh rather than committing to memory):

```bash
cp $PORTAL_ROOT/app/static/terminal-theme.css .
cp -r $PORTAL_ROOT/app/static/vendor/fira-code-v27 vendor/
```

The second line is the webfont, and it matters - see below.

It is also served at `$BASE_URL/static/terminal-theme.css`, and a live
gallery of every component - with the page skeleton to copy - is at
`$BASE_URL/style`. (Those are the URLs to hand over too: never give a
`localhost` or `127.0.0.1` address, which is always a dead link on the
device actually reading it.)

## Using it

One CSS file, no JS, no build step. Pair it with the Fira Code webfont,
which your own app serves:

```html
<link rel="stylesheet" href="vendor/fira-code-v27/font.css">
<link rel="stylesheet" href="terminal-theme.css">
```

**Never link `fonts.googleapis.com` for this, even though that is what the
one-line recipe everywhere on the web says.** A webfont `<link>` to Google
hands them the visitor's IP, their user agent and - in the `Referer` - the
exact page they opened, before anything draws; a stylesheet is render-blocking,
so their outage is your page's outage; and it forces
`style-src https://fonts.googleapis.com` and `font-src https://fonts.gstatic.com`
into your CSP. Delete any `<link rel="preconnect">` to either host too: a
preconnect opens a DNS lookup, a TCP connection and a TLS handshake to Google
on every page load whether or not anything is fetched over it, so leaving one
keeps announcing every visit after the font has gone.

Where the bytes are: `$PORTAL_ROOT/app/static/vendor/fira-code-v27/`, and
served at `$BASE_URL/static/vendor/fira-code-v27/font.css`. Copy the whole
directory, not just the Latin `.woff2`: the `url()`s inside `font.css` are
relative so it is one self-contained unit, and `unicode-range` means a browser
downloads only the subsets it actually renders - the other six cost a visitor
nothing, and dropping them turns a non-Latin character into tofu with no error
anywhere. Keep the version in the directory name; that is what lets you serve
`/vendor/` with a year-long `immutable` cache header, because an upgrade
becomes a new directory rather than an edit to a path caches are entitled never
to re-fetch. Without any font file the theme falls back to the system monospace
and still reads correctly.

The skeleton the components assume:

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

Three rules of the house style worth keeping even if you diverge: no
hover-only controls (the reader is often on a phone), cap any panel that can
grow without bound rather than letting it push the page down, and **never make
a hover state a solid fill with an inverted label**.

That last one has bitten twice, both times reported by $OWNER rather than caught
by a test. "Solid background plus flipped label" is a pair, and any rule with
more specificity that sets a hover *color* and no *background* splits it: the
control keeps its own label color and drops it onto the fill, so the word
disappears. A tab, a foldable heading, any large custom target shaped like a
button. The theme's own `button:hover` therefore deepens the control's existing
tint and lights up its border instead - the label never moves, and a
translucent wash over the dark body cannot swallow it whatever color it is. If
you add a control with its own `:hover`, answer *every* property the generic
rule sets, not just the one that looks wrong.

Hover rules live inside `@media (hover: hover)`, because iOS leaves `:hover`
stuck on whatever was tapped until you tap elsewhere. Feedback for a finger is
`:active`. Two consequences when you go to verify it: headless chromium reports
`(hover: none)` and so does any touch-emulated page, so a hover check silently
measures rest state and passes having tested nothing. Launch chromium with
`--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4`,
turn touch emulation off and *reload* (the media state is latched at document
load), and assert `matchMedia('(hover: hover)').matches` before trusting a
single measurement. `Emulation.setEmulatedMedia` cannot fake hover whatever its
`features` list says - measured, not assumed.
