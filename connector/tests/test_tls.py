"""
Loopback TLS (tls.py): the public port the Cloudflare tunnel forwards to
must not hand the token to a process that grabs the port while the
connector is stopped.

The "tunnel client" in these tests is configured the way cloudflared is by
config/cloudflared.yml: trust only the connector certificate (caPool) and
expect the name wa-connector.local (originServerName).
"""

import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import net
import tls

SERVER = Path(__file__).resolve().parents[1] / "server.py"
ROOT = Path(__file__).resolve().parents[2]
TOKEN = "tunnel" + "t" * 37
IS_WINDOWS = sys.platform == "win32"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tunnel_request(port: int, tls_dir: Path) -> bytes:
    """What cloudflared does for an iPhone request: TLS with the pinned CA and
    originServerName, then send the request whose URL carries the token."""
    ctx = tls.client_context(tls.files(tls_dir).cert)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=tls.SERVER_NAME) as s:
            s.sendall(f"POST /mcp/{TOKEN} HTTP/1.1\r\nHost: wa.example.com\r\nContent-Length: 2\r\n\r\n{{}}".encode())
            return s.recv(65536)


class RawRecorder:
    def __init__(self, server_ctx=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.ctx = server_ctx
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
                if self.ctx:
                    c = self.ctx.wrap_socket(c, server_side=True)
                while chunk := c.recv(65536):
                    self.data += chunk
            except (OSError, ssl.SSLError):
                pass
            finally:
                c.close()

    def close(self):
        self.sock.close()


def server_ctx(tls_dir):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    f = tls.files(tls_dir)
    ctx.load_cert_chain(str(f.cert), str(f.key))
    return ctx


# ---- the public-port squatter ---------------------------------------------

def test_plain_squatter_on_public_port_never_sees_the_request(tmp_path):
    tls.ensure(tmp_path / "tls")
    squatter = RawRecorder()
    try:
        with pytest.raises((ssl.SSLError, OSError)):
            tunnel_request(squatter.port, tmp_path / "tls")
        time.sleep(0.2)
        assert TOKEN.encode() not in squatter.data and b"POST" not in squatter.data
    finally:
        squatter.close()


def test_tls_squatter_with_own_certificate_never_sees_the_request(tmp_path):
    tls.ensure(tmp_path / "tls")
    tls.ensure(tmp_path / "attacker")          # same name, different key
    squatter = RawRecorder(server_ctx(tmp_path / "attacker"))
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            tunnel_request(squatter.port, tmp_path / "tls")
        time.sleep(0.2)
        assert TOKEN.encode() not in squatter.data
    finally:
        squatter.close()


@pytest.fixture
def server(db, tmp_path):
    port, admin = free_port(), free_port()
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db), "MCP_AUTH_TOKEN": TOKEN,
           "MCP_PORT": str(port), "MCP_ADMIN_CHANNEL": "tcp", "MCP_ADMIN_PORT": str(admin),
           "MCP_TLS_DIR": str(tmp_path / "tls"), "MCP_PUBLIC_HOSTNAME": "wa.example.com"}
    proc = subprocess.Popen([sys.executable, str(SERVER), "--transport", "http", "--log-file",
                             str(tmp_path / "c.log")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if net.healthy(port, tmp_path / "tls"):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield port, tmp_path / "tls"
    proc.terminate()
    proc.wait(10)


def test_real_connector_accepts_the_tunnel(server):
    port, tls_dir = server
    reply = tunnel_request(port, tls_dir)
    assert reply.startswith(b"HTTP/1.1 ") and b" 404 " not in reply.split(b"\r\n")[0]


def test_real_connector_refuses_plain_http(server):
    port, _ = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET /mcp/{TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        try:
            reply = s.recv(4096)
        except OSError:
            reply = b""
    assert not reply.startswith(b"HTTP/1.1 200")


# ---- certificate material ----------------------------------------------------

def test_ensure_creates_then_reuses(tmp_path):
    first = tls.ensure(tmp_path / "t")
    cert = first.cert.read_bytes()
    assert tls.ensure(tmp_path / "t").cert.read_bytes() == cert        # never overwritten
    if not IS_WINDOWS:
        assert stat.S_IMODE(os.stat(first.key).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(tmp_path / "t").st_mode) == 0o700


def test_certificate_names_and_constraints(tmp_path):
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(tls.ensure(tmp_path / "t").cert.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [tls.SERVER_NAME]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]


def test_mismatched_key_refused(tmp_path):
    tls.ensure(tmp_path / "a")
    tls.ensure(tmp_path / "b")
    shutil.copy(tmp_path / "b" / "connector.key", tmp_path / "a" / "connector.key")
    if not IS_WINDOWS:
        os.chmod(tmp_path / "a" / "connector.key", 0o600)
    with pytest.raises(tls.TlsError, match="does not match"):
        tls.ensure(tmp_path / "a")


def test_half_present_material_refused_not_overwritten(tmp_path):
    f = tls.ensure(tmp_path / "t")
    f.key.unlink()
    with pytest.raises(tls.TlsError, match="only one of"):
        tls.ensure(tmp_path / "t")
    assert not f.key.exists()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX permission bits")
def test_readable_key_refused(tmp_path):
    f = tls.ensure(tmp_path / "t")
    os.chmod(f.key, 0o640)
    with pytest.raises(tls.TlsError, match="owner-only"):
        tls.ensure(tmp_path / "t")


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX permission bits")
def test_world_writable_tls_dir_refused(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    os.chmod(d, 0o777)
    with pytest.raises(tls.TlsError, match="not group/world-writable"):
        tls.ensure(d)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX symlinks")
def test_symlinked_key_refused(tmp_path):
    f = tls.ensure(tmp_path / "t")
    real = tmp_path / "elsewhere.key"
    f.key.rename(real)
    f.key.symlink_to(real)
    with pytest.raises(tls.TlsError, match="not a regular file"):
        tls.ensure(tmp_path / "t")


def test_server_refuses_to_start_with_bad_tls(db, tmp_path):
    f = tls.ensure(tmp_path / "tls")
    f.key.unlink()
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db), "MCP_AUTH_TOKEN": TOKEN,
           "MCP_PORT": str(free_port()), "MCP_ADMIN_CHANNEL": "off", "MCP_TLS_DIR": str(tmp_path / "tls")}
    r = subprocess.run([sys.executable, str(SERVER), "--transport", "http"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "Refusing to start" in r.stderr


# ---- the tunnel config the setup scripts write --------------------------------

def _check_rendered(cfg_text: str, port: int, cert: Path):
    assert f"service: https://127.0.0.1:{port}" in cfg_text
    assert "http://127.0.0.1" not in cfg_text
    assert f"caPool: '{cert}'" in cfg_text
    assert f"originServerName: {tls.SERVER_NAME}" in cfg_text
    assert "noTLSVerify: false" in cfg_text
    assert "path: ^/(mcp(/.*)?|daily(/.*)?|healthz)$" in cfg_text   # daily-summary endpoint forwarded
    for placeholder in ("__TUNNEL_ID__", "__HOME__", "__HOSTNAME__", "__PORT__", "__CA_POOL__"):
        assert placeholder not in cfg_text
    assert "tunnel: 8f3569a9-6bc5-4355-924d-ba2d2cae182b" in cfg_text


@pytest.mark.skipif(IS_WINDOWS or shutil.which("bash") is None, reason="macOS script")
def test_macos_setup_tunnel_pins_the_certificate(tmp_path, db):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(); state.mkdir()
    env_file = tmp_path / "c.env"
    port = free_port()
    env_file.write_text(f"MCP_PORT={port}\nMCP_TLS_DIR={tmp_path / 'tls'}\nMCP_AUTH_TOKEN={TOKEN}\n")
    # MCP_TLS_DIR empty in the environment: this test covers the value from .env.
    env = {**os.environ, "HOME": str(home), "STUB_STATE": str(state), "WA_ENV_FILE": str(env_file),
           "WA_PYTHON": sys.executable, "MCP_TLS_DIR": "",
           "PATH": f"{Path(__file__).parent / 'stubs'}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run([os.environ.get("WA_TEST_BASH", "bash"), str(ROOT / "macos/setup-tunnel.sh"), "wa.example.com"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    _check_rendered((home / ".cloudflared/wa-connector.yml").read_text(), port, tls.files(tmp_path / "tls").cert)
    assert "MCP_PUBLIC_HOSTNAME=wa.example.com" in env_file.read_text()


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows script")
def test_windows_setup_tunnel_pins_the_certificate(tmp_path, db):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(); state.mkdir()
    env_file = tmp_path / "c.env"
    port = free_port()
    env_file.write_text(f"MCP_PORT={port}\nMCP_TLS_DIR={tmp_path / 'tls'}\nMCP_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "STUB_STATE": str(state),
           "WA_ENV_FILE": str(env_file), "WA_PYTHON": sys.executable, "MCP_TLS_DIR": "",
           "PATH": f"{Path(__file__).parent / 'stubs_windows'}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run([os.environ.get("WA_TEST_POWERSHELL", "powershell.exe"), "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "windows/setup-tunnel.ps1"), "wa.example.com"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    rendered = (home / ".cloudflared" / "wa-connector.yml").read_text(encoding="utf-8")
    _check_rendered(rendered, port, tls.files(tmp_path / "tls").cert)
