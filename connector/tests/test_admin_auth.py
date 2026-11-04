"""
Admin-channel authentication (admin_auth.py): the maintenance client must
never send the token to whoever happens to hold the admin channel.

The impersonation tests run a fake server on the admin port (and, on Unix,
on the admin socket) that records everything it receives and answers
"success" to everything, as an attacker's listener would.
"""

import asyncio
import http.server
import json
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import net
import tls

import admin_auth
from admin_auth import HELLO_PATH, AdminAuth, request_mac, server_proof

VERIFY = Path(__file__).resolve().parents[1] / "verify.py"
SERVER = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "T0k3n" + "s" * 38   # synthetic, 43 chars
IS_WINDOWS = sys.platform == "win32"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- the impersonator -------------------------------------------------------

class Recorder(http.server.BaseHTTPRequestHandler):
    """Answers everything with 200 and plausible JSON; records all it sees."""
    seen: list = []

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("latin-1") if length else ""
        Recorder.seen.append({"method": self.command, "path": self.path,
                              "headers": dict(self.headers), "body": body})

    def _reply(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._record()
        # A fake hello: the impersonator does not know the token, so the best
        # it can do is a well-formed but wrong proof.
        self._reply({"s": "ab" * 16, "proof": "00" * 32})

    def do_POST(self):
        self._record()
        self._reply({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "list_chats"}]}})

    def log_message(self, *a):
        pass


def everything_seen() -> str:
    return json.dumps(Recorder.seen)


def tls_server_ctx(tls_dir: Path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    f = tls.files(tls_dir)
    ctx.load_cert_chain(str(f.cert), str(f.key))
    return ctx


def attacker_tls_dir(tmp_path: Path) -> Path:
    """An attacker's OWN self-signed certificate for the same name."""
    d = tmp_path / "attacker-tls"
    tls.ensure(d)
    return d


def start_http(handler, tls_dir=None, unix_path=None):
    """A threaded fake server, optionally TLS, on TCP or a Unix socket."""
    if unix_path:
        class Srv(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        class H(handler):
            def address_string(self):
                return "unix"
        srv = Srv(str(unix_path), H)
    else:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if tls_dir:
        srv.socket = tls_server_ctx(tls_dir).wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class RawRecorder:
    """Plain TCP listener that just records the bytes it receives."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.data = b""
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            c.settimeout(2)
            try:
                while chunk := c.recv(65536):
                    self.data += chunk
            except OSError:
                pass
            c.close()

    def close(self):
        self.sock.close()


def run_verify(env, *args):
    return subprocess.run([sys.executable, str(VERIFY), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def tcp_env(tmp_path, admin_port, **extra):
    TLS_DIR["current"] = tmp_path / "tls"
    return {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "MCP_AUTH_TOKEN": TOKEN,
            "MCP_PORT": str(free_port()), "MCP_ADMIN_CHANNEL": "tcp", "MCP_ADMIN_PORT": str(admin_port),
            "MCP_TLS_DIR": str(tmp_path / "tls"), **extra}


VERIFY_ARGS = [["token"], ["sending", "--expect", "off"], ["sending", "--expect", "on"]]
OLD = "0ld" + "o" * 40


def assert_learned_nothing(seen_text: str):
    assert TOKEN not in seen_text and OLD not in seen_text
    assert "authorization" not in seen_text.lower() and "x-wa-admin" not in seen_text.lower()


@pytest.mark.parametrize("args", VERIFY_ARGS)
def test_plain_http_squatter_on_admin_port_gets_no_request(tmp_path, args):
    # Tier 1. Regression (Codex): verify.py used to send the bearer token to
    # whoever held the port. Now TLS fails first: only handshake bytes arrive.
    tls.ensure(tmp_path / "tls")             # the real connector's certificate exists
    raw = RawRecorder()
    try:
        r = run_verify(tcp_env(tmp_path, raw.port, WA_OLD_TOKEN=OLD), *args)
        time.sleep(0.2)
        assert r.returncode == 1, r.stdout + r.stderr
        assert b"HTTP/1.1" not in raw.data and b"/admin/hello" not in raw.data and b"/mcp" not in raw.data
        assert TOKEN.encode() not in raw.data and OLD.encode() not in raw.data
    finally:
        raw.close()


@pytest.mark.parametrize("args", VERIFY_ARGS)
def test_tls_squatter_with_own_certificate_rejected(tmp_path, args):
    # Tier 2: a TLS listener with a valid-looking certificate for the same
    # name, but not OUR certificate. Pinning rejects it before any request.
    tls.ensure(tmp_path / "tls")
    Recorder.seen = []
    srv = start_http(Recorder, tls_dir=attacker_tls_dir(tmp_path))
    try:
        r = run_verify(tcp_env(tmp_path, srv.server_address[1], WA_OLD_TOKEN=OLD), *args)
        assert r.returncode == 1 and "failed TLS verification" in r.stderr, r.stderr
        assert Recorder.seen == []
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("args", VERIFY_ARGS)
def test_squatter_with_stolen_tls_key_still_learns_nothing(tmp_path, args):
    # Tier 3: even with our certificate AND private key (key theft), the
    # HMAC challenge stops it: it sees only GET /admin/hello.
    tls.ensure(tmp_path / "tls")
    Recorder.seen = []
    srv = start_http(Recorder, tls_dir=tmp_path / "tls")
    try:
        r = run_verify(tcp_env(tmp_path, srv.server_address[1], WA_OLD_TOKEN=OLD), *args)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "did NOT prove" in r.stderr and "nothing secret was sent" in r.stderr
        assert all(req["method"] == "GET" and req["path"].startswith(HELLO_PATH) for req in Recorder.seen)
        assert_learned_nothing(everything_seen())
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(IS_WINDOWS, reason="Unix socket channel")
@pytest.mark.parametrize("tier", ["own-cert", "stolen-key"])
def test_squatter_on_admin_socket_learns_nothing(tmp_path, tier):
    tls.ensure(tmp_path / "tls")
    Recorder.seen = []
    sock_dir = Path(tempfile.mkdtemp(prefix="wa", dir="/tmp"))
    path = sock_dir / "admin.sock"
    srv = start_http(Recorder, tls_dir=attacker_tls_dir(tmp_path) if tier == "own-cert" else tmp_path / "tls",
                     unix_path=path)
    try:
        env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "MCP_AUTH_TOKEN": TOKEN,
               "MCP_ADMIN_CHANNEL": "unix", "MCP_ADMIN_SOCKET": str(path), "MCP_TLS_DIR": str(tmp_path / "tls")}
        r = run_verify(env, "sending", "--expect", "off")
        assert r.returncode == 1
        if tier == "own-cert":
            assert "failed TLS verification" in r.stderr and Recorder.seen == []
        else:
            assert "did NOT prove" in r.stderr
            assert all(req["method"] == "GET" for req in Recorder.seen)
        assert_learned_nothing(everything_seen())
    finally:
        srv.shutdown()
        srv.server_close()
        path.unlink(missing_ok=True)
        sock_dir.rmdir()


def test_relay_of_proof_for_another_token_rejected(tmp_path):
    """A listener with our TLS key that knows a DIFFERENT token (e.g. an old
    connector) produces a real but wrong proof: still refused, nothing sent."""
    other = "other" + "z" * 38

    class OtherTokenServer(Recorder):
        def do_GET(self):
            self._record()
            c = self.path.split("c=", 1)[1]
            s = "cd" * 16
            self._reply({"s": s, "proof": server_proof(other, c, s)})

    tls.ensure(tmp_path / "tls")
    Recorder.seen = []
    srv = start_http(OtherTokenServer, tls_dir=tmp_path / "tls")
    try:
        r = run_verify(tcp_env(tmp_path, srv.server_address[1]), "sending", "--expect", "off")
        assert r.returncode == 1 and "proof does not match" in r.stderr
        assert all(req["method"] == "GET" for req in Recorder.seen)
    finally:
        srv.shutdown()
        srv.server_close()


# ---- the real server ---------------------------------------------------------

@pytest.fixture
def real_server(db, tmp_path):
    public, admin = free_port(), free_port()
    while admin == public:
        admin = free_port()
    env = tcp_env(tmp_path, admin, MCP_PORT=str(public), WA_DB_PATH=str(db))
    proc = subprocess.Popen([sys.executable, str(SERVER), "--transport", "http",
                             "--log-file", str(tmp_path / "c.log")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if net.healthy(public, tmp_path / "tls"):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield env, admin
    proc.terminate()
    proc.wait(10)


def test_verify_against_real_server(real_server):
    env, _ = real_server
    assert run_verify(env, "token").returncode == 0
    assert run_verify(env, "sending", "--expect", "off").returncode == 0


def test_rotation_not_applied_is_detected(real_server):
    # .env has a NEW token, but the running server still holds the old one.
    env, _ = real_server
    r = run_verify({**env, "MCP_AUTH_TOKEN": "new" + "n" * 40, "WA_OLD_TOKEN": TOKEN}, "token")
    assert r.returncode == 1 and "old token STILL ACTIVE" in r.stderr


TLS_DIR = {"current": None}


def _raw(port, method, path, headers=None, body=None):
    c = net.connection(port, TLS_DIR["current"])
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return c, r.status, r.read()


def test_request_mac_single_use_and_bound_to_path(real_server):
    _, admin = real_server
    c_nonce = "aa" * 16
    conn, status, raw = _raw(admin, "GET", f"{HELLO_PATH}?c={c_nonce}")
    assert status == 200
    hello = json.loads(raw)
    assert hello["proof"] == server_proof(TOKEN, c_nonce, hello["s"])
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    good = f"s={hello['s']}, mac={request_mac(TOKEN, c_nonce, hello['s'], 'POST', '/mcp')}"
    wrong_path = f"s={hello['s']}, mac={request_mac(TOKEN, c_nonce, hello['s'], 'POST', '/other')}"
    conn.request("POST", "/mcp", body=body, headers={**hdr, "X-WA-Admin": wrong_path})
    r = conn.getresponse(); r.read()
    assert r.status == 404                       # MAC for another path; nonce now burnt
    conn.request("POST", "/mcp", body=body, headers={**hdr, "X-WA-Admin": good})
    r = conn.getresponse(); r.read()
    assert r.status == 404                       # single use, even after a failed attempt
    conn.close()


def test_hello_proof_cannot_be_used_as_request_mac(real_server):
    _, admin = real_server
    c_nonce = "bb" * 16
    conn, _, raw = _raw(admin, "GET", f"{HELLO_PATH}?c={c_nonce}")
    hello = json.loads(raw)
    conn.request("POST", "/mcp", body="{}", headers={"X-WA-Admin": f"s={hello['s']}, mac={hello['proof']}"})
    r = conn.getresponse(); r.read()
    assert r.status == 404
    conn.close()


@pytest.mark.parametrize("q", ["", "c=", "c=short", "c=" + "G" * 32, "c=" + "a" * 200])
def test_hello_rejects_bad_client_nonce(real_server, q):
    _, admin = real_server
    conn, status, _ = _raw(admin, "GET", f"{HELLO_PATH}?{q}")
    conn.close()
    assert status == 400


def test_hello_not_served_on_public_port(real_server):
    env, _ = real_server
    conn, status, _ = _raw(int(env["MCP_PORT"]), "GET", f"{HELLO_PATH}?c={'aa' * 16}")
    conn.close()
    assert status == 404


# ---- unit: expiry and bounded state -----------------------------------------

def _call(app, method, path, headers=(), query=b""):
    out = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        else:
            out["body"] = msg.get("body", b"")

    async def recv():
        return {"type": "http.request", "body": b""}

    asyncio.run(app({"type": "http", "method": method, "path": path, "headers": list(headers),
                     "query_string": query}, recv, send))
    return out


def test_expired_server_nonce_not_accepted():
    seen = []

    async def inner(scope, receive, send):
        seen.append(dict(scope["headers"]).get(b"authorization"))

    now = [100.0]
    auth = AdminAuth(inner, TOKEN, clock=lambda: now[0])
    hello = json.loads(_call(auth, "GET", HELLO_PATH, query=b"c=" + b"cc" * 16)["body"])
    now[0] += admin_auth.NONCE_TTL_S + 1
    mac = request_mac(TOKEN, "cc" * 16, hello["s"], "POST", "/mcp")
    _call(auth, "POST", "/mcp", headers=[(b"x-wa-admin", f"s={hello['s']}, mac={mac}".encode())])
    assert seen == [None]          # reached the gate without credentials


def test_valid_mac_injects_token_and_strips_client_credentials():
    seen = []

    async def inner(scope, receive, send):
        seen.append(dict(scope["headers"]))

    auth = AdminAuth(inner, TOKEN)
    hello = json.loads(_call(auth, "GET", HELLO_PATH, query=b"c=" + b"dd" * 16)["body"])
    mac = request_mac(TOKEN, "dd" * 16, hello["s"], "POST", "/mcp")
    _call(auth, "POST", "/mcp", headers=[(b"x-wa-admin", f"s={hello['s']}, mac={mac}".encode()),
                                         (b"authorization", b"Bearer attacker")])
    assert seen[0][b"authorization"] == f"Bearer {TOKEN}".encode()
    assert b"x-wa-admin" not in seen[0]


def test_open_nonces_bounded():
    async def inner(scope, receive, send):
        pass

    auth = AdminAuth(inner, TOKEN)
    for _ in range(admin_auth.MAX_OPEN * 3):
        _call(auth, "GET", HELLO_PATH, query=b"c=" + b"ee" * 16)
    assert len(auth._open) <= admin_auth.MAX_OPEN
