"""
The connector's local admin channel, independent of the operating system.

The server listens on its public TCP port (the one the Cloudflare tunnel
forwards to, with token + MCP_ALLOWED_IPS) and on a local admin channel
(token required, IP allowlist not applied) that the maintenance scripts use
through connector/verify.py. This module picks the implementation:

  * macOS / Linux: admin_unix, an owner-only Unix socket (MCP_ADMIN_SOCKET)

A listener provides:  .sock (a bound socket for uvicorn), .matches(scope)
(did this ASGI request arrive on the admin channel?), .cleanup(), .describe().
"""

from __future__ import annotations

import http.client
from typing import Optional


class AdminChannelError(RuntimeError):
    """The admin channel is unsafe or impossible to open; refuse to start."""


def open_listener(cfg):
    """Bind the admin channel for `cfg`, or return None if disabled."""
    if not cfg.admin_socket:
        return None
    from admin_unix import UnixAdminListener
    return UnixAdminListener(cfg.admin_socket)


def connect(cfg, timeout: float = 5) -> http.client.HTTPConnection:
    """An HTTP connection to the running connector's admin channel."""
    if not cfg.admin_socket:
        raise OSError("MCP_ADMIN_SOCKET not configured")
    from admin_unix import UnixHTTPConnection
    return UnixHTTPConnection(str(cfg.admin_socket), timeout=timeout)
