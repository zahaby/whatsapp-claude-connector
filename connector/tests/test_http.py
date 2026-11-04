"""
HTTP integration: the real server process, real MCP client, real TokenGate.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "k" * 43


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def server(db, tmp_path):
    port = _free_port()
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db),
           "MCP_AUTH_TOKEN": TOKEN, "MCP_PORT": str(port), "MCP_PUBLIC_HOSTNAME": "wa.example.com",
           "WHATSAPP_ALLOW_SEND": "false"}
    proc = subprocess.Popen([sys.executable, str(SERVER), "--transport", "http"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if _get(base + "/healthz")[0] == 200:
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start: " + proc.stderr.read())
    yield base, proc
    proc.terminate()
    proc.wait(5)


def test_unauthenticated_requests_get_404(server):
    base, _ = server
    assert _get(base + "/mcp", data=b"{}")[0] == 404
    assert _get(base + "/mcp/wrong-token", data=b"{}")[0] == 404
    assert _get(base + "/mcp", headers={"Authorization": "Bearer nope"}, data=b"{}")[0] == 404
    assert _get(base + "/anything")[0] == 404


def test_mcp_client_over_token_url(server):
    base, _ = server

    async def go():
        async with streamablehttp_client(f"{base}/mcp/{TOKEN}") as (r, w, _):
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
