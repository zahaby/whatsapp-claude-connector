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


@pytest.mark.parametrize("size", [2, 64 * 1024, 900 * 1024])
def test_rejected_post_gets_complete_response(server, size):
    # Regression (Windows CI): rejecting a POST without reading its body made
    # the socket close with unread data, and the TCP reset cut the 404 short.
    base, _ = server
    port = int(base.rsplit(":", 1)[1])
    for _ in range(3):  # several in a row: none may be cut off
        c = net.connection(port, TLS_DIR["current"])
        c.request("POST", "/mcp", body=b"x" * size, headers={"Authorization": "Bearer wrong",
                                                            "Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 404 and r.getheader("Content-Length") == "0"
        assert r.read() == b""
        c.close()


# ---- slow or missing request bodies (connection exhaustion) -----------------

DEADLINE_S = 2.5   # drain deadline (1 s) plus generous slack for slow CI runners


def _tls_socket(port):
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    return net.ctx(TLS_DIR["current"]).wrap_socket(raw, server_hostname="127.0.0.1")


def _read_reply(s, timeout):
    """Read until the server closes the connection or `timeout` passes."""
    s.settimeout(timeout)
    data = b""
    try:
        while chunk := s.recv(65536):
            data += chunk
    except (socket.timeout, TimeoutError):
        pass
    except OSError:
        pass  # closed by the server: that is what we want
    return data


def _stalled_post(port, declared, sent=b""):
    s = _tls_socket(port)
    s.sendall(f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer wrong\r\n"
              f"Content-Type: application/json\r\nContent-Length: {declared}\r\n\r\n".encode() + sent)
    return s


@pytest.mark.parametrize("sent", [b"", b"x" * 100])
def test_rejection_does_not_wait_for_withheld_body(server, sent):
    # Regression (Codex): the gate awaited a body that never came, forever.
    base, _ = server
    port = int(base.rsplit(":", 1)[1])
    s = _stalled_post(port, declared=100_000, sent=sent)
    start = time.monotonic()
    reply = _read_reply(s, timeout=DEADLINE_S + 3)
    elapsed = time.monotonic() - start
    s.close()
    assert reply.startswith(b"HTTP/1.1 404"), reply[:80]
    assert elapsed < DEADLINE_S, f"rejection took {elapsed:.1f}s"
    assert b"connection: close" in reply.lower()


def test_trickled_body_is_cut_off(server):
    base, _ = server
    port = int(base.rsplit(":", 1)[1])
    s = _stalled_post(port, declared=10_000)
    start = time.monotonic()
    s.settimeout(0.3)
    reply = b""
    while time.monotonic() - start < DEADLINE_S + 3 and not reply:
        try:
            s.sendall(b"x")          # one byte every ~0.3 s keeps a naive drain waiting
            reply = s.recv(65536)
        except (socket.timeout, TimeoutError):
            continue
        except OSError:
            break
    elapsed = time.monotonic() - start
    s.close()
    assert reply.startswith(b"HTTP/1.1 404"), reply[:80]
    assert elapsed < DEADLINE_S, f"rejection took {elapsed:.1f}s"


def test_many_stalled_requests_do_not_exhaust_the_server(server):
    base, _ = server
    port = int(base.rsplit(":", 1)[1])
    stalled = [_stalled_post(port, declared=100_000) for _ in range(30)]
    start = time.monotonic()
    replies = [_read_reply(s, timeout=DEADLINE_S + 3) for s in stalled]
    for s in stalled:
        s.close()
    # 404 for the first failures, 429 once the lockout kicks in: all prompt.
    assert all(r.startswith((b"HTTP/1.1 404", b"HTTP/1.1 429")) for r in replies), [r[:20] for r in replies]
    assert time.monotonic() - start < DEADLINE_S * 2
    assert net.healthy(port, TLS_DIR["current"])     # still serving


@pytest.mark.skipif(IS_WINDOWS, reason="Windows scripts stop the connector with a hard kill")
@pytest.mark.parametrize("client", ["idle-tcp", "stalled-body"])
def test_held_connection_cannot_block_shutdown(server, client):
    # Restarts (rotation, sending switch) send SIGTERM; a client holding a
    # socket open must not make the old process linger.
    base, proc = server
    port = int(base.rsplit(":", 1)[1])
    if client == "idle-tcp":
        held = socket.create_connection(("127.0.0.1", port), timeout=10)   # never even handshakes
    else:
        held = _tls_socket(port)
        held.sendall(b"POST /mcp/" + TOKEN.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n{")
        time.sleep(0.3)  # authorised: the app is now waiting for the rest of the body
    start = time.monotonic()
    proc.terminate()
    try:
        proc.wait(timeout=10)
    finally:
        held.close()
    assert time.monotonic() - start < 6, "graceful shutdown waited on a held connection"
