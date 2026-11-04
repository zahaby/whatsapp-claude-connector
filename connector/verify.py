"""
Check what the RUNNING connector actually enforces, over its local HTTP port.
Used by scripts/rotate-token.sh and scripts/sending.sh so they only report
success after observing the new behaviour, not just after editing .env.

  verify.py wait-healthy                       wait until /healthz answers
  verify.py token                              token in .env accepted AND the old
                                               one (env WA_OLD_TOKEN) rejected
  verify.py sending --expect on|off            send tool present / absent

Exit status 0 = verified, 1 = not verified (reason on stderr).
Each check sends at most ONE request with a wrong token, so it never trips
the connector's failed-attempt lockout. Nothing here prints a token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import Config  # noqa: E402  (loads .env the same way the server does)


def _base(cfg: Config) -> str:
    return f"http://127.0.0.1:{cfg.port}"


def _status(req: urllib.request.Request, timeout: float = 5) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def tools_list(cfg: Config, token: str) -> tuple[int, list[str]]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = urllib.request.Request(
        _base(cfg) + "/mcp", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    status, raw = _status(req)
    if status != 200:
        return status, []
    return status, [t["name"] for t in json.loads(raw)["result"]["tools"]]


def wait_healthy(cfg: Config, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if _status(urllib.request.Request(_base(cfg) + "/healthz"), timeout=2)[0] == 200:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wait-healthy")
    w.add_argument("--timeout", type=float, default=float(os.environ.get("WA_RESTART_TIMEOUT", "20")))
    sub.add_parser("token")  # tokens come from .env / env vars, never argv (visible in ps)
    s = sub.add_parser("sending")
    s.add_argument("--expect", choices=["on", "off"], required=True)
    args = ap.parse_args(argv)
    cfg = Config.from_env()

    try:
        if args.cmd == "wait-healthy":
            if wait_healthy(cfg, args.timeout):
                return 0
            print(f"connector not answering on 127.0.0.1:{cfg.port} after {args.timeout:.0f}s", file=sys.stderr)
            return 1

        if args.cmd == "token":
            old = os.environ.get("WA_OLD_TOKEN", "")
            new_status, _ = tools_list(cfg, cfg.auth_token)
            if new_status != 200:
                print(f"new token NOT accepted (HTTP {new_status})", file=sys.stderr)
                return 1
            if old and old != cfg.auth_token:
                old_status, _ = tools_list(cfg, old)
                if old_status != 404:
                    print(f"old token STILL ACCEPTED (HTTP {old_status})", file=sys.stderr)
                    return 1
            return 0

        if args.cmd == "sending":
            status, names = tools_list(cfg, cfg.auth_token)
            if status != 200:
                print(f"could not list tools (HTTP {status})", file=sys.stderr)
                return 1
            present = "send_message" in names
            if present != (args.expect == "on"):
                print(f"running connector has sending {'ON' if present else 'OFF'}, expected {args.expect.upper()}",
                      file=sys.stderr)
                return 1
            return 0
    except OSError as e:
        print(f"cannot reach connector on 127.0.0.1:{cfg.port}: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
