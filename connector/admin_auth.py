"""
Mutual challenge-response for the local admin channel.

Problem it solves: before talking to the admin channel, the client cannot
know who is listening. On Windows it is a loopback TCP port, so while the
connector is stopped another local account could bind that port and wait
for the maintenance scripts to send it the bearer token.

So the admin channel never carries the token. Instead, over ONE connection:

  1. client -> GET /admin/hello?c=<client nonce>
     server -> {"s": <server nonce>, "proof": HMAC(token, "wa-admin-server|c|s")}
  2. client checks the proof against the token in .env. An impersonator
     cannot compute it, so the client stops here having sent nothing secret.
  3. client -> the real request, with header
     X-WA-Admin: s=<server nonce>, mac=HMAC(token, "wa-admin-client|c|s|METHOD|path")
     The server nonce is single-use and expires after NONCE_TTL_S.

Domain-separation prefixes ("wa-admin-server" / "wa-admin-client") mean a
proof obtained from /admin/hello can never be reused as a request MAC.
The bearer token is still accepted on the admin channel for manual use
(curl), but verify.py never sends it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs

HELLO_PATH = "/admin/hello"
HEADER = "x-wa-admin"
NONCE_TTL_S = 30
MAX_OPEN = 64
_HEX = re.compile(r"^[0-9a-f]{32,128}$")


def _mac(token: str, *parts: str) -> str:
    return hmac.new(token.encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()


def server_proof(token: str, client_nonce: str, server_nonce: str) -> str:
    return _mac(token, "wa-admin-server", client_nonce, server_nonce)


def request_mac(token: str, client_nonce: str, server_nonce: str, method: str, path: str) -> str:
    return _mac(token, "wa-admin-client", client_nonce, server_nonce, method.upper(), path)


def parse_header(value: str) -> Optional[tuple[str, str]]:
    fields = dict(part.strip().split("=", 1) for part in value.split(",") if "=" in part)
    s, mac = fields.get("s", ""), fields.get("mac", "")
    if _HEX.match(s) and re.fullmatch(r"[0-9a-f]{64}", mac):
        return s, mac
    return None


class AdminAuth:
    """ASGI middleware in front of the LOCAL TokenGate. Answers the hello,
    and turns a valid X-WA-Admin request into an authorised one for the
    gate. Anything else passes through unchanged (and an invalid or replayed
    X-WA-Admin request reaches the gate with no credentials, so it is
    rejected and counted towards the lockout like any other failure)."""

    def __init__(self, app, token: str, clock=time.monotonic):
        self.app = app
        self.token = token
        self._clock = clock
        self._open: dict[str, tuple[str, float]] = {}  # server nonce -> (client nonce, expiry)

    def _purge(self) -> None:
        now = self._clock()
        for s in [s for s, (_, exp) in self._open.items() if exp <= now]:
            del self._open[s]
        while len(self._open) >= MAX_OPEN:
            self._open.pop(next(iter(self._open)))

    @staticmethod
    async def _json(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        if scope.get("path") == HELLO_PATH:
            if scope.get("method") != "GET":
                return await self._json(send, 405, {"error": "GET only"})
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            c = (query.get("c") or [""])[0]
            if not _HEX.match(c):
                return await self._json(send, 400, {"error": "c must be 32-128 lowercase hex chars"})
            self._purge()
            s = secrets.token_hex(16)
            self._open[s] = (c, self._clock() + NONCE_TTL_S)
            return await self._json(send, 200, {"s": s, "proof": server_proof(self.token, c, s)})

        headers = list(scope.get("headers", []))
        raw = next((v.decode("latin-1") for k, v in headers if k.lower() == HEADER.encode()), None)
        if raw is None:
            return await self.app(scope, receive, send)  # e.g. a bearer token typed by hand

        # Never forward client-supplied credentials alongside the admin header.
        headers = [(k, v) for k, v in headers if k.lower() not in (HEADER.encode(), b"authorization")]
        parsed = parse_header(raw)
        if parsed:
            s, mac = parsed
            entry = self._open.pop(s, None)  # single use, whatever the outcome
            if entry and entry[1] > self._clock():
                expected = request_mac(self.token, entry[0], s, scope.get("method", ""), scope.get("path", ""))
                if hmac.compare_digest(mac, expected):
                    headers.append((b"authorization", f"Bearer {self.token}".encode()))
        return await self.app(dict(scope, headers=headers), receive, send)
