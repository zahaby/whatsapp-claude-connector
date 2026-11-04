"""
HTTP integration: the real server process, real MCP client, real TokenGate.
"""

import asyncio
import os
import socket
import subprocess
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

import net
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "k" * 43


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TLS_DIR = {"current": None}   # the running test server's certificate directory


def _get(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    return net.request(req, TLS_DIR["current"])


IS_WINDOWS = sys.platform == "win32"


def _start(db, tmp_path, **extra):
    port = _free_port()
    # Short dir: Unix socket paths are limited to ~104 bytes on macOS.
    sock_dir = Path(tempfile.mkdtemp(prefix="wa", dir=None if IS_WINDOWS else "/tmp"))
    admin = {"MCP_ADMIN_CHANNEL": "tcp", "MCP_ADMIN_PORT": str(_free_port())} if IS_WINDOWS else \
            {"MCP_ADMIN_SOCKET": str(sock_dir / "admin.sock")}
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db),
           "MCP_AUTH_TOKEN": TOKEN, "MCP_PORT": str(port), "MCP_PUBLIC_HOSTNAME": "wa.example.com",
           "WHATSAPP_ALLOW_SEND": "false", "MCP_TLS_DIR": str(sock_dir / "tls"), **admin, **extra}
    TLS_DIR["current"] = sock_dir / "tls"
    proc = subprocess.Popen([sys.executable, str(SERVER), "--transport", "http"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = net.url(port)
    for _ in range(100):
        try:
            if _get(base + "/healthz")[0] == 200:
                break
        except OSError:  # includes "no certificate yet" and connection refused
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start: " + proc.stderr.read())
    return base, proc, sock_dir


@pytest.fixture
def server(db, tmp_path):
    base, proc, sock_dir = _start(db, tmp_path)
    yield base, proc
    proc.terminate()
    proc.wait(5)
    shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.mark.skipif(IS_WINDOWS, reason="Unix socket; the TCP channel is covered in test_admin_tcp.py")
def test_admin_socket_bypasses_allowlist_only_locally(db, tmp_path):
    base, proc, sock_dir = _start(db, tmp_path, MCP_ALLOWED_IPS="160.79.104.0/21")
    try:
        sys.path.insert(0, str(SERVER.parent))
        import verify
        from server import Config
        cfg = Config(db_path=db, bridge_url="", allow_send=False, auth_token=TOKEN, public_hostname="",
                     host="127.0.0.1", port=0, allowed_ips=[], admin_socket=sock_dir / "admin.sock",
                     tls_dir=sock_dir / "tls")
        assert verify.tools_list(cfg, TOKEN)[0] == 200          # local socket: allowlist not applied
        with pytest.raises(verify.NotOurConnector):             # client refuses a server that
            verify.tools_list(cfg, "x" * 43)                      # cannot prove the expected token
        from admin_unix import UnixHTTPSConnection                # server still needs the token:
        c = UnixHTTPSConnection(str(sock_dir / "admin.sock"), net.ctx(sock_dir / "tls"))
        c.request("POST", "/mcp", body="{}", headers={"Authorization": "Bearer " + "x" * 43})
        assert c.getresponse().status == 404
        c.close()
        hdrs = {"Authorization": f"Bearer {TOKEN}"}
        assert _get(base + "/mcp", headers=hdrs, data=b"{}")[0] == 404   # TCP: allowlist applies
        assert stat.S_IMODE((sock_dir / "admin.sock").stat().st_mode) == 0o600
    finally:
        proc.terminate()
        proc.wait(5)
    assert not (sock_dir / "admin.sock").exists()               # cleaned up on shutdown
    shutil.rmtree(sock_dir, ignore_errors=True)


def test_unauthenticated_requests_get_404(server):
    base, _ = server
    assert _get(base + "/mcp", data=b"{}")[0] == 404
    assert _get(base + "/mcp/wrong-token", data=b"{}")[0] == 404
    assert _get(base + "/mcp", headers={"Authorization": "Bearer nope"}, data=b"{}")[0] == 404
    assert _get(base + "/anything")[0] == 404


def test_mcp_client_over_token_url(server):
    base, _ = server

    async def go():
        def pinned_client(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30), auth=auth,
                                     verify=net.ctx(TLS_DIR["current"]), follow_redirects=True)

        async with streamablehttp_client(f"{base}/mcp/{TOKEN}", httpx_client_factory=pinned_client) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                text = (await s.call_tool("get_chat_transcript", {"chat": "family"})).content[0].text
                return names, text

    names, text = asyncio.run(go())
    assert "get_chat_transcript" in names and "send_message" not in names
    assert "Mum: Dinner Friday at 8?" in text


def test_host_header_checked(server):
    base, _ = server
    body = (b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
            b'"capabilities":{},"clientInfo":{"name":"t","version":"1"}}}')
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    assert _get(f"{base}/mcp/{TOKEN}", headers={**hdrs, "Host": "wa.example.com"}, data=body)[0] == 200
    assert _get(f"{base}/mcp/{TOKEN}", headers={**hdrs, "Host": "evil.example"}, data=body)[0] == 421


def test_token_never_logged(server):
    base, proc = server
    _get(f"{base}/mcp/{TOKEN}", data=b"{}")
    _get(base + "/mcp/guess-" + "x" * 30, data=b"{}")
    proc.terminate()
    _, err = proc.communicate(timeout=5)
    assert TOKEN not in err and "guess-" not in err
