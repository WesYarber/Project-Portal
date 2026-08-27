#!/usr/bin/env bash
# Render a portal page and bring the PNG back here to look at.
#
# The portal is usually built by agents running on a headless server, so for a
# long time every UI change was verified with curl and grep. If you have a
# second machine with a browser, set `render_host` in portal.toml and an agent
# can *see* the page instead of inferring it.
#
# This is the recipe that works, and each flag below is here because something
# else failed:
#
#   - Firefox is often installed but `--headless --screenshot` hangs and times
#     out. Chromium (snap) works.
#   - Snap chromium is confined to $HOME, so a screenshot written to /tmp
#     silently never appears. Write it under ~ and scp it back.
#   - `--headless=new` waits for the load event, and the dashboard holds an
#     SSE stream open forever, so it never fires. Old headless plus
#     --virtual-time-budget/--timeout captures regardless.
#   - The render machine may not be able to reach the portal at the same
#     address everybody else uses - a tailnet ACL or a VLAN can admit one
#     machine and drop another, and the failure looks like a blank page rather
#     than an error. `render_portal_url` in portal.toml is that second route;
#     Settings > access shows what reaches what. See app/netinfo.py.
#
# Usage: deploy/screenshot.sh /settings [height] [out.png] [width]
#
# The second argument is the HEIGHT. The width used to be hardcoded at 1280 with
# nothing saying so, and three separate runs passed 390 there expecting a phone
# and got a 1280x390 letterbox they then reported as a phone layout. It is the
# fourth argument now, and WIDTH= in the environment works too:
#
#   deploy/screenshot.sh /project/x 900 /tmp/phone.png 390     # a real phone
#   WIDTH=1920 deploy/screenshot.sh / 1200 /tmp/wide.png       # a wide desktop

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# The site config is the one place that knows about this installation, so ask
# it rather than baking a machine into the script. Env vars still win, which is
# what makes a one-off "screenshot that other box" possible without editing
# anything.
site() { "$ROOT/venv/bin/python" -c "import sys; sys.path.insert(0, '$ROOT'); from app import config; print(getattr(config.SITE, '$1') or '')" 2>/dev/null; }

BOX=${BOX:-$(site render_host)}
PORTAL=${PORTAL:-$(site render_portal_url)}
PORTAL=${PORTAL:-$(site base_url)}

if [ -z "$BOX" ]; then
  echo "No render machine configured." >&2
  echo "Set render_host = \"user@host\" in portal.toml (a machine with chromium" >&2
  echo "and key-based SSH from here), or run with BOX=user@host." >&2
  exit 2
fi
PATH_=${1:-/}
HEIGHT=${2:-1900}
OUT=${3:-/tmp/portal-shot.png}
WIDTH=${4:-${WIDTH:-1280}}
REMOTE_NAME="portal-shot-$$.png"

ssh -o BatchMode=yes "$BOX" bash -s <<EOF
set -e
timeout 90 /snap/bin/chromium --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --no-first-run --user-data-dir="\$HOME/.portal-shot-profile" \
  --run-all-compositor-stages-before-draw --virtual-time-budget=6000 \
  --window-size=$WIDTH,$HEIGHT --screenshot="\$HOME/$REMOTE_NAME" --timeout=25000 \
  "$PORTAL$PATH_" >/dev/null 2>&1
EOF

scp -o BatchMode=yes "$BOX:~/$REMOTE_NAME" "$OUT"
ssh -o BatchMode=yes "$BOX" "rm -f ~/$REMOTE_NAME"
echo "$OUT"
