"""
Check what the RUNNING connector actually enforces, over its local HTTP port.
Used by macos/rotate-token.sh and macos/sending.sh so they only report
success after observing the new behaviour, not just after editing .env.

  verify.py wait-healthy                       wait until /healthz answers
  verify.py token                              token in .env accepted AND the old
                                               one (env WA_OLD_TOKEN) rejected
  verify.py sending --expect on|off            send tool present / absent

Exit status 0 = verified, 1 = not verified (reason on stderr).
Token and tool checks go over the connector's local admin channel
(admin_channel.py; on macOS/Linux the Unix socket MCP_ADMIN_SOCKET), so they work even when MCP_ALLOWED_IPS only admits
Anthropic's addresses on the TCP port. wait-healthy uses the TCP port, so
it also proves the listener cloudflared forwards to is up.
The token is NEVER transmitted: the server first proves it holds the token
(HMAC challenge-response, admin_auth.py), so a program impersonating the
admin channel learns nothing. Nothing here prints a token.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import secrets
import ssl
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin_channel  # noqa: E402
import tls  # noqa: E402
from admin_auth import HELLO_PATH, request_mac, server_proof  # noqa: E402
from server import Config  # noqa: E402  (loads .env the same way the server does)


def _base(cfg: Config) -> str:
    return f"https://127.0.0.1:{cfg.port}"


def _status(req: urllib.request.Request, ctx, timeout: float = 5) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class NotOurConnector(Exception):
    """The admin channel could not prove it holds the token in .env."""


class AdminSession:
    """One connection to the admin channel, authenticated both ways without
    ever sending the token (see admin_auth.py)."""

    def __init__(self, cfg: Config, token: str):
        self.cfg, self.token = cfg, token
        self.conn = admin_channel.connect(cfg)

    def close(self) -> None:
        self.conn.close()

    def _hello(self) -> tuple[str, str]:
        c = secrets.token_hex(16)
        self.conn.request("GET", f"{HELLO_PATH}?c={c}")
        r = self.conn.getresponse()
        raw = r.read()
        try:
            data = json.loads(raw) if r.status == 200 else {}
            s, proof = str(data["s"]), str(data["proof"])
        except (ValueError, KeyError, TypeError):
            raise NotOurConnector(f"no valid hello (HTTP {r.status})")
        if not hmac.compare_digest(proof, server_proof(self.token, c, s)):
            raise NotOurConnector("proof does not match the token in .env")
        return c, s

    def post(self, path: str, body: str) -> tuple[int, bytes]:
        c, s = self._hello()  # server proven; only now send a request
        mac = request_mac(self.token, c, s, "POST", path)
        self.conn.request("POST", path, body=body, headers={
            "X-WA-Admin": f"s={s}, mac={mac}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        r = self.conn.getresponse()
        return r.status, r.read()


def tools_list(cfg: Config, token: str) -> tuple[int, list[str]]:
    """tools/list over the admin channel, after the server proved it holds
    `token`. Raises NotOurConnector otherwise; `token` is never transmitted."""
    session = AdminSession(cfg, token)
    try:
        status, raw = session.post("/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    finally:
        session.close()
    if status != 200:
        return status, []
    return status, [t["name"] for t in json.loads(raw)["result"]["tools"]]


def healthy(cfg: Config) -> bool:
    """One /healthz check over TLS, trusting only the connector's certificate.
    A listener without our key fails the handshake, so this is never fooled."""
    try:
        ctx = tls.client_context(admin_channel.tls_cert(cfg))
        return _status(urllib.request.Request(_base(cfg) + "/healthz"), ctx, timeout=2)[0] == 200
    except (OSError, ssl.SSLError):
        return False


def wait_healthy(cfg: Config, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if healthy(cfg):
            return True
        time.sleep(0.3)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")  # one check; exit 0 if this connector answers
    w = sub.add_parser("wait-healthy")
    w.add_argument("--timeout", type=float, default=float(os.environ.get("WA_RESTART_TIMEOUT", "20")))
    sub.add_parser("token")  # tokens come from .env / env vars, never argv (visible in ps)
    s = sub.add_parser("sending")
    s.add_argument("--expect", choices=["on", "off"], required=True)
    args = ap.parse_args(argv)
    cfg = Config.from_env()

    try:
        if args.cmd == "health":
            return 0 if healthy(cfg) else 1

        if args.cmd == "wait-healthy":
            if wait_healthy(cfg, args.timeout):
                return 0
            print(f"connector not answering on 127.0.0.1:{cfg.port} after {args.timeout:.0f}s", file=sys.stderr)
            return 1

        if args.cmd == "token":
            # The server proves it holds the NEW token (it can hold only one),
            # so the old one is necessarily rejected. Neither is ever sent.
            old = os.environ.get("WA_OLD_TOKEN", "")
            try:
                status, _ = tools_list(cfg, cfg.auth_token)
            except NotOurConnector:
                if old and old != cfg.auth_token:
                    try:
                        tools_list(cfg, old)
                        print("old token STILL ACTIVE: the running connector holds the previous token",
                              file=sys.stderr)
                        return 1
                    except NotOurConnector:
                        pass
                raise
            if status != 200:
                print(f"new token NOT accepted (HTTP {status})", file=sys.stderr)
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
    except NotOurConnector as e:
        print(f"admin channel did NOT prove it is this connector ({e}); nothing secret was sent. "
              "Another program may be listening on it.", file=sys.stderr)
        return 1
    except ssl.SSLError as e:
        print(f"admin channel failed TLS verification ({e.reason or e}); it is not this connector, "
              "nothing was sent.", file=sys.stderr)
        return 1
    except (OSError, http.client.HTTPException) as e:
        print(f"cannot reach connector's admin channel: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
