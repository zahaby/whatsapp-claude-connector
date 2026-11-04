"""
The connector's local admin channel, independent of the operating system.

The server listens on its public TCP port (the one the Cloudflare tunnel
forwards to, with token + MCP_ALLOWED_IPS) and on a local admin channel
(token required, IP allowlist not applied) that the maintenance scripts use
through connector/verify.py. This module picks the implementation:

  * "unix" (macOS / Linux default): admin_unix, an owner-only Unix socket
    at MCP_ADMIN_SOCKET
  * "tcp" (Windows default): admin_tcp, loopback port MCP_ADMIN_PORT that the
    tunnel never forwards to
  * "off": no admin channel (the scripts then cannot verify the live server)

Set MCP_ADMIN_CHANNEL to override the default.

A listener provides:  .sock (a bound socket for uvicorn), .matches(scope)
(did this ASGI request arrive on the admin channel?), .cleanup(), .describe().
"""

from __future__ import annotations

import http.client
import sys


class AdminChannelError(RuntimeError):
    """The admin channel is unsafe or impossible to open; refuse to start."""


CHANNELS = ("unix", "tcp", "off")


def _channel(cfg) -> str:
    ch = getattr(cfg, "admin_channel", "unix") or "unix"
    if ch not in CHANNELS:
        raise AdminChannelError(f"MCP_ADMIN_CHANNEL must be one of {', '.join(CHANNELS)}, not {ch!r}")
    if ch == "unix" and sys.platform == "win32":
        raise AdminChannelError("MCP_ADMIN_CHANNEL=unix is not available on Windows; use tcp")
    return ch


def open_listener(cfg):
    """Bind the admin channel for `cfg`, or return None if it is off."""
    ch = _channel(cfg)
    if ch == "off":
        return None
    if ch == "tcp":
        from admin_tcp import TcpAdminListener
        return TcpAdminListener(cfg.admin_port, cfg.port)
    if not cfg.admin_socket:
        return None
    from admin_unix import UnixAdminListener
    return UnixAdminListener(cfg.admin_socket)


def tls_cert(cfg) -> "Path":
    import tls
    return tls.files(cfg.tls_dir or tls.default_dir()).cert


def connect(cfg, timeout: float = 5) -> http.client.HTTPConnection:
    """A TLS connection to the running connector's admin channel that
    trusts only the connector's own certificate."""
    import tls
    ch = _channel(cfg)
    if ch == "off":
        raise OSError("admin channel is off (MCP_ADMIN_CHANNEL=off)")
    ctx = tls.client_context(tls_cert(cfg))
    if ch == "tcp":
        from admin_tcp import connect as tcp_connect
        return tcp_connect(cfg.admin_port, ctx, timeout=timeout)
    if not cfg.admin_socket:
        raise OSError("MCP_ADMIN_SOCKET not configured")
    from admin_unix import UnixHTTPSConnection
    return UnixHTTPSConnection(str(cfg.admin_socket), ctx, timeout=timeout)
