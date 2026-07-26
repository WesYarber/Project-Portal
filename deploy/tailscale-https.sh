#!/usr/bin/env bash
# Put the portal behind HTTPS on the tailnet.
#
# Why: the browser only exposes the microphone in a secure context, so voice
# memos are invisible over plain http. `tailscale serve` terminates
# TLS on port 443 of this node's tailnet address using a real Let's Encrypt
# certificate that Tailscale fetches and renews itself. Nothing is exposed to
# the public internet - only machines on the tailnet can connect, and the ACL
# still applies on top.
#
# Prerequisites, both one-time and both needing the tailnet owner:
#   * HTTPS Certificates enabled for the tailnet (admin console > DNS).
#   * `sudo tailscale set --operator=wes`, so this runs without root. Without
#     it every tailscale subcommand here answers "Access denied".
#
# Idempotent: re-running it re-applies the same serve config. The config is
# stored in tailscaled's own state, so it survives a reboot and a portal
# restart without any systemd unit of its own.
set -euo pipefail

PORT="${PORT:-8500}"
# The preview server (app/preview.py), which serves what projects build. It
# gets its own https port rather than a path under 443, because the whole
# point of it being a separate port locally is that it is a separate origin
# from the portal - a path on 443 would hand that back.
PREVIEW_PORT="${PREVIEW_PORT:-8501}"
PREVIEW_HTTPS_PORT="${PREVIEW_HTTPS_PORT:-8443}"

if ! command -v tailscale >/dev/null; then
  echo "tailscale is not installed on this machine" >&2
  exit 1
fi

dns="$(tailscale status --json | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo "node: ${dns}"

# Fetch the cert first, as its own step. `tailscale serve` would fetch it
# lazily on the first request, which turns a misconfigured tailnet into a
# mysterious timeout in a browser instead of an error message here.
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
if ! tailscale cert --cert-file "${tmp}/c" --key-file "${tmp}/k" "${dns}"; then
  echo "could not get a certificate - check HTTPS Certificates is enabled for the tailnet" >&2
  exit 1
fi

tailscale serve --bg --https=443 "http://127.0.0.1:${PORT}"
tailscale serve --bg --https="${PREVIEW_HTTPS_PORT}" "http://127.0.0.1:${PREVIEW_PORT}"
tailscale serve status

echo
echo "checking it answers..."
curl -fsS -o /dev/null -w "https://%{host} -> %{http_code}\n" "https://${dns}/"
curl -fsS -o /dev/null -w "https://%{host}:%{remote_port} -> %{http_code}\n" \
  "https://${dns}:${PREVIEW_HTTPS_PORT}/"
