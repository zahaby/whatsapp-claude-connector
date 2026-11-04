"""
Local admin channel over a loopback TCP port (Windows default; available
everywhere with MCP_ADMIN_CHANNEL=tcp).

CPython on Windows has no AF_UNIX sockets, so instead the connector listens
on a second port, 127.0.0.1:MCP_ADMIN_PORT (default MCP_PORT + 1), that the
Cloudflare tunnel never forwards to (config/cloudflared.yml routes only to
MCP_PORT). Requests on it still need the token; only the IP allowlist is
skipped, exactly like the Unix socket.

Differences from the Unix socket, stated plainly:
  * any local user account can connect to a loopback port (a 0600 socket
    limits that to the owner). They still need the 256-bit token.
  * nothing is written to disk, so there is nothing to clean up or to be
    tricked into overwriting. Binding the port exclusively doubles as the
    single-instance lock: a second connector cannot bind it.
"""

from __future__ import annotations

import http.client
import socket
import sys

from admin_channel import AdminChannelError

HOST = "127.0.0.1"


class AdminPortError(AdminChannelError):
    pass


def bind_admin_port(port: int, public_port: int) -> socket.socket:
    if not (1 <= port <= 65535):
        raise AdminPortError(f"MCP_ADMIN_PORT {port} is not a valid port")
    if port == public_port:
        raise AdminPortError("MCP_ADMIN_PORT must differ from MCP_PORT (the port the tunnel forwards to)")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        # Without this, another process could bind the same port and
        # intercept the scripts' requests (and their token).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    # Deliberately no SO_REUSEADDR anywhere: a live listener must make bind fail.
    try:
        sock.bind((HOST, port))
    except OSError as e:
        sock.close()
        raise AdminPortError(f"cannot bind admin port {HOST}:{port} ({e}); "
                             "is another connector (or another program) using it?") from e
    return sock


class TcpAdminListener:
    kind = "tcp"

    def __init__(self, port: int, public_port: int):
        self.port = port
        self.sock = bind_admin_port(port, public_port)

    def describe(self) -> str:
        return f"{HOST}:{self.port}"

    def matches(self, scope) -> bool:
        # scope["server"] comes from the listening socket's own address
        # (getsockname), not from any request header, so it cannot be spoofed.
        server = scope.get("server") or ()
        return len(server) == 2 and server[1] == self.port and server[0] in (HOST, "localhost")

    def cleanup(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def connect(port: int, ctx, timeout: float = 5) -> http.client.HTTPConnection:
    # Certificate pinned by `ctx`; its IP SAN 127.0.0.1 is checked.
    return http.client.HTTPSConnection(HOST, port, context=ctx, timeout=timeout)
