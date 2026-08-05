#!/usr/bin/env python3
"""A fake mothership, for testing instance→mothership telemetry on a dev box.

The dev box has no real mothership credentials (and per `~/dev/CLAUDE.md` it must
never get any), so `MothershipClient.enabled` is False and every phone-home is
dropped before it reaches the network. That makes the whole telemetry path —
report_error, agent friction, frontend errors, lease renewal, update checks —
impossible to watch actually happen.

This stands in for llamapress.ai: it accepts any `/api/leonardo/*` POST, prints the
bearer token and the pretty-printed body, and answers with a plausible response so
the caller's success path runs. Nothing is stored and nothing leaves the box.

    # on the host
    python3 scripts/mothership_stub.py                 # listens on 0.0.0.0:9001

Then point the instance at it — `~/dev/Leonardo/.leonardo/instance.json`:

    {
      "instance_name": "llamapress-dev",
      "mothership_url": "http://172.19.0.1:9001",
      "mothership_api_token": "dev-stub-token",
      "lease_duration_seconds": 300
    }

`enabled` only checks that url / instance_name / token are all non-empty, so any
placeholder token works. The IP is the compose network's gateway (the container's
route to the host) — the stub prints the right one on startup, and it changes if
the docker network is ever recreated. Restart llamabot afterwards:

    cd ~/dev/Leonardo && docker compose -f docker-compose-dev.yml \\
        up -d --force-recreate llamabot

REMEMBER to put instance.json back (empty token) when you're done, or the box will
keep trying to phone a stub that isn't running. That's harmless — every call is
best-effort and fails soft — but it's noise in the logs.
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

# Canned responses, keyed by path suffix. Shapes match what the real mothership
# returns (see docs/handoff_mothership_check_updates.md and mothership_client.py).
RESPONSES = {
    "report_error": {"success": True},
    "report_message": {"success": True},
    "report_disconnect": {"success": True},
    "lease_renew": {"success": True, "lease_expires_at": "2099-01-01T00:00:00Z"},
    "check_updates": {"updates_available": False, "latest_versions": {}},
    "check_paywall": {"paywall_active": False},
}
DEFAULT_RESPONSE = {"success": True}

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        try:
            body = json.loads(raw)
            pretty = json.dumps(body, indent=2, sort_keys=True)
        except Exception:
            body, pretty = None, raw.decode("utf-8", "replace")

        auth = self.headers.get("Authorization", "(none)")
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        print(f"\n{BOLD}{GREEN}━━━ {stamp}  POST {self.path}{RESET}")
        print(f"{DIM}Authorization: {auth}{RESET}")
        # The one field most likely to be wrong on a new integration.
        if isinstance(body, dict) and "source" in body:
            print(f"{YELLOW}source: {body['source']}{RESET}")
        print(pretty, flush=True)

        payload = DEFAULT_RESPONSE
        for suffix, canned in RESPONSES.items():
            if self.path.rstrip("/").endswith(suffix):
                payload = canned
                break

        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"mothership stub: alive\n")

    def log_message(self, *args):
        pass  # the do_POST print above is the log


def container_gateway() -> str:
    """The address the llamabot container uses to reach this host, best effort."""
    try:
        net = subprocess.run(
            ["docker", "inspect", "leonardo-llamabot-1", "-f",
             "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return subprocess.run(
            ["docker", "network", "inspect", net, "-f",
             "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "172.17.0.1"
    except Exception:
        return "172.17.0.1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    gateway = container_gateway()
    print(f"{BOLD}Mothership stub listening on {args.host}:{args.port}{RESET}")
    print(f"Set mothership_url to: {BOLD}http://{gateway}:{args.port}{RESET}")
    print(f"{DIM}(that's the llamabot container's route to this host){RESET}")
    print(f"{DIM}Nothing is stored; nothing leaves this box. Ctrl-C to stop.{RESET}")

    try:
        HTTPServer((args.host, args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
