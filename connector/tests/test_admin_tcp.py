"""
The loopback-TCP admin channel (Windows default). Runs on every OS:
MCP_ADMIN_CHANNEL=tcp selects it on macOS/Linux too.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import admin_channel
from admin_tcp import AdminPortError, TcpAdminListener, bind_admin_port
from server import Config, Store

SERVER = Path(__file__).resolve().parents[1] / "server.py"
TOKEN = "w" * 43


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_pair() -> tuple[int, int]:
    """Two distinct free ports (public, admin)."""
    while True:
        a, b = free_port(), free_port()
        if a != b:
            return a, b


def cfg(**kw) -> Config:
    base = dict(db_path=Path("x.db"), bridge_url="", allow_send=False, auth_token=TOKEN,
                public_hostname="", host="127.0.0.1", port=8765, allowed_ips=[],
                admin_socket=None, admin_channel="tcp", admin_port=8766)
    base.update(kw)
    return Config(**base)


# ---- binding ---------------------------------------------------------------

def test_admin_port_must_differ_from_public_port():
    with pytest.raises(AdminPortError, match="must differ"):
        bind_admin_port(8765, 8765)


@pytest.mark.parametrize("port", [0, 70000])
def test_invalid_admin_port_refused(port):
    with pytest.raises(AdminPortError, match="not a valid port"):
        bind_admin_port(port, 8765)


def test_second_instance_cannot_bind_admin_port():
    public, admin = free_pair()
    first = TcpAdminListener(admin, public)
    first.sock.listen(1)
    try:
        with pytest.raises(AdminPortError, match="cannot bind admin port"):
            TcpAdminListener(admin, public)
    finally:
        first.cleanup()


def test_admin_port_used_by_other_program_refused():
    public, admin = free_pair()
    other = socket.socket()
    other.bind(("127.0.0.1", admin))
    other.listen(1)
    try:
        with pytest.raises(AdminPortError):
            TcpAdminListener(admin, public)
    finally:
        other.close()


def test_listener_matches_only_its_own_port():
    listener = TcpAdminListener.__new__(TcpAdminListener)  # matching logic only
    listener.port = 9001
    assert listener.matches({"server": ("127.0.0.1", 9001)})
    assert not listener.matches({"server": ("127.0.0.1", 8765)})   # the public port
    assert not listener.matches({"server": ("/tmp/admin.sock", None)})
    assert not listener.matches({"server": ("10.0.0.5", 9001)})
    assert not listener.matches({})


# ---- channel selection -----------------------------------------------------

def test_unknown_channel_refused():
    with pytest.raises(admin_channel.AdminChannelError, match="must be one of"):
        admin_channel.open_listener(cfg(admin_channel="pipe"))


def test_channel_off_means_no_listener():
    assert admin_channel.open_listener(cfg(admin_channel="off")) is None
    with pytest.raises(OSError, match="off"):
        admin_channel.connect(cfg(admin_channel="off"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only rule")
def test_unix_channel_refused_on_windows():
    with pytest.raises(admin_channel.AdminChannelError, match="not available on Windows"):
        admin_channel.open_listener(cfg(admin_channel="unix"))


def test_default_channel_and_port(monkeypatch, tmp_path):
    for k in ("MCP_ADMIN_CHANNEL", "MCP_ADMIN_PORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WA_CONFIG", str(tmp_path / "none"))
    monkeypatch.setenv("MCP_PORT", "9100")
    c = Config.from_env()
    assert c.admin_channel == ("tcp" if sys.platform == "win32" else "unix")
    assert c.admin_port == 9101


# ---- end to end over the tcp channel ----------------------------------------

def _tools(port: int, token: str, cf_ip: str = "") -> tuple[int, list]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 **({"CF-Connecting-IP": cf_ip} if cf_ip else {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, [t["name"] for t in json.loads(r.read())["result"]["tools"]]
    except urllib.error.HTTPError as e:
        return e.code, []


@pytest.fixture
def tcp_server(db, tmp_path):
    public, admin = free_pair()
    log_file = tmp_path / "logs" / "connector.log"
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db),
           "MCP_AUTH_TOKEN": TOKEN, "MCP_PORT": str(public), "MCP_ADMIN_CHANNEL": "tcp",
           "MCP_ADMIN_PORT": str(admin), "MCP_ALLOWED_IPS": "160.79.104.0/21"}
    proc = subprocess.Popen([sys.executable, str(SERVER), "--transport", "http", "--log-file", str(log_file)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{public}/healthz", timeout=1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start: " + (log_file.read_text() if log_file.exists() else ""))
    yield public, admin, env, log_file
    proc.terminate()
    proc.wait(10)


def test_tcp_admin_channel_bypasses_allowlist_only_on_admin_port(tcp_server):
    public, admin, _, _ = tcp_server
    assert _tools(admin, TOKEN)[0] == 200                       # admin port: allowlist not applied
    assert _tools(admin, "x" * 43)[0] == 404                    # ...token still required
    assert _tools(public, TOKEN)[0] == 404                      # public port from localhost: allowlist applies
    assert _tools(public, TOKEN, cf_ip="203.0.113.9")[0] == 404
    assert _tools(public, TOKEN, cf_ip="160.79.105.10")[0] == 200


def test_verify_works_over_tcp_channel_with_allowlist(tcp_server):
    _, _, env, _ = tcp_server
    for args, code in ((["token"], 0), (["sending", "--expect", "off"], 0), (["sending", "--expect", "on"], 1)):
        r = subprocess.run([sys.executable, str(SERVER.parent / "verify.py"), *args], env=env,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == code, (args, r.stderr)


def test_second_server_refused_while_first_runs(tcp_server, tmp_path):
    public, admin, env, _ = tcp_server
    other_public = free_port()
    log2 = tmp_path / "second.log"
    r = subprocess.run([sys.executable, str(SERVER), "--transport", "http", "--log-file", str(log2)],
                       env={**env, "MCP_PORT": str(other_public)}, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "cannot bind admin port" in log2.read_text()


def test_log_file_receives_logs_and_never_the_token(tcp_server):
    public, admin, _, log_file = tcp_server
    _tools(admin, "y" * 43)  # one failure, logged
    time.sleep(0.3)
    text = log_file.read_text(encoding="utf-8")
    assert "auth failure" in text and "sending=disabled" in text
    assert TOKEN not in text and "y" * 43 not in text


# ---- read-only store on unusual paths -------------------------------------

def test_store_opens_paths_with_spaces_and_hash(db, tmp_path):
    odd = tmp_path / "My WhatsApp #1 (copy)" / "messages.db"
    odd.parent.mkdir()
    odd.write_bytes(db.read_bytes())
    chats = Store(odd).chats()
    assert {c["name"] for c in chats} >= {"Family", "Mum"}
    with pytest.raises(sqlite3.OperationalError):
        with Store(odd)._conn() as c:
            c.execute("DELETE FROM messages")
