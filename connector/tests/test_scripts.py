"""
Regression tests for the shell scripts (findings 1 and 3).

launchctl, go, uv, cloudflared, git and pbcopy are replaced by stubs in
tests/stubs. The launchctl stub runs the REAL connector server, so token
rotation and the send switch are checked against a live HTTP process.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
STUBS = Path(__file__).resolve().parent / "stubs"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# CI on macOS sets WA_TEST_BASH=/bin/bash to test the stock bash 3.2 users actually have.
BASH = os.environ.get("WA_TEST_BASH", "bash")
pytestmark = pytest.mark.skipif(shutil.which(BASH) is None, reason="needs bash")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self, tmp: Path, db: Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        self.home = tmp / "home"
        self.home.mkdir()
        self.env_file = tmp / "connector.env"
        # Short path: Unix socket paths are limited to ~104 bytes on macOS.
        self.sock_dir = Path(tempfile.mkdtemp(prefix="wa", dir="/tmp"))
        self.admin_socket = self.sock_dir / "run" / "admin.sock"
        self.port = free_port()
        self.db = db
        self.vars = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{STUBS}{os.pathsep}{os.environ['PATH']}",
            "STUB_STATE": str(self.state),
            "STUB_REPO": str(ROOT),
            "WA_ENV_FILE": str(self.env_file),
            "WA_PYTHON": sys.executable,
            "WA_RESTART_TIMEOUT": "8",
            "UPSTREAM": str(tmp / "upstream"),
        }

    def write_env(self, **extra):
        values = {
            "WA_DB_PATH": str(self.db), "WHATSAPP_ALLOW_SEND": "false",
            "MCP_AUTH_TOKEN": "A" * 43, "MCP_PUBLIC_HOSTNAME": "", "MCP_PORT": str(self.port),
            "MCP_ADMIN_SOCKET": str(self.admin_socket),
            **extra,
        }
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        self.env_file.chmod(0o600)

    def get(self, key: str) -> str:
        for line in self.env_file.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return ""

    def run(self, script: str, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, str(SCRIPTS / script), *args], env={**self.vars, **env},
                              capture_output=True, text=True, timeout=60)

    def mcp_status(self, token: str, cf_ip: str = "") -> int:
        """tools/list over the TCP port (the one cloudflared forwards to).
        cf_ip simulates the CF-Connecting-IP header the tunnel adds."""
        extra = {"CF-Connecting-IP": cf_ip} if cf_ip else {}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/mcp", method="POST",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", **extra})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                self.last_tools = [t["name"] for t in json.loads(r.read())["result"]["tools"]]
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    allowlist = False

    def token_ok(self, token: str) -> int:
        """Status a remote caller would get: via the tunnel, from an allowed IP."""
        return self.mcp_status(token, cf_ip="160.79.105.10" if self.allowlist else "")

    def stop(self):
        pid_file = self.state / "com.wa.connector.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 15)
            except (ProcessLookupError, ValueError):
                pass
        shutil.rmtree(self.sock_dir, ignore_errors=True)


@pytest.fixture
def env(tmp_path, db):
    e = Env(tmp_path, db)
    yield e
    e.stop()


ANTHROPIC_LIKE = "160.79.104.0/21"   # any CIDR that excludes 127.0.0.1


@pytest.fixture(params=["no-allowlist", "allowlist"])
def running(env, request):
    """Connector loaded and serving, like after scripts/start.sh. Every test
    using it runs twice: without and with MCP_ALLOWED_IPS (finding: verify
    must still work when the allowlist excludes localhost)."""
    env.allowlist = request.param == "allowlist"
    env.write_env(**({"MCP_ALLOWED_IPS": ANTHROPIC_LIKE} if env.allowlist else {}))
    (env.home / "Library/LaunchAgents").mkdir(parents=True)
    for label in ("com.wa.bridge", "com.wa.connector", "com.wa.tunnel"):
        (env.home / f"Library/LaunchAgents/{label}.plist").write_text("<plist/>")
    r = env.run("start.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.token_ok(env.get("MCP_AUTH_TOKEN")) == 200
    return env


# ---- Finding 1: fresh install must not exit before creating services -------

def fake_upstream(env: Env, main_go: str):
    bridge = Path(env.vars["UPSTREAM"]) / "whatsapp-bridge"
    bridge.mkdir(parents=True)
    (Path(env.vars["UPSTREAM"]) / ".git").mkdir()
    (bridge / "main.go").write_text(main_go)
    return bridge / "main.go"


def test_install_fresh_without_hostname_completes(env):
    main_go = fake_upstream(env, (FIXTURES / "upstream_main.go.txt").read_text())
    r = env.run("install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install done" in r.stdout

    plists = sorted(p.name for p in (env.home / "Library/LaunchAgents").glob("*.plist"))
    assert plists == ["com.wa.bridge.plist", "com.wa.connector.plist", "com.wa.tunnel.plist"]
    for p in (env.home / "Library/LaunchAgents").glob("*.plist"):
        assert "__" not in p.read_text(), f"unrendered placeholder in {p.name}"

    assert len(env.get("MCP_AUTH_TOKEN")) == 43
    assert env.get("MCP_PUBLIC_HOSTNAME") == ""
    assert stat.S_IMODE(env.env_file.stat().st_mode) == 0o600

    patched = main_go.read_text()
    assert 'fmt.Sprintf("127.0.0.1:%d", port)' in patched
    assert "sender" not in patched.split("// Start the server")[0].split("Log based on message type")[1]
    assert not list(main_go.parent.glob("*.bak"))


def test_install_rerun_keeps_token(env):
    fake_upstream(env, (FIXTURES / "upstream_main.go.txt").read_text())
    assert env.run("install.sh").returncode == 0
    token = env.get("MCP_AUTH_TOKEN")
    assert env.run("install.sh").returncode == 0
    assert env.get("MCP_AUTH_TOKEN") == token


def test_install_stops_if_upstream_changed(env):
    fake_upstream(env, "package main\nfunc main() {}\n")
    r = env.run("install.sh")
    assert r.returncode != 0 and "Patch failed" in r.stdout


# ---- Finding 3: restart failures must propagate; rotation must be verified ---

def test_rotate_when_not_running(env):
    env.write_env()
    old = env.get("MCP_AUTH_TOKEN")
    r = env.run("rotate-token.sh")
    assert r.returncode == 0, r.stderr
    assert env.get("MCP_AUTH_TOKEN") != old
    assert "not running" in r.stdout
    assert env.get("MCP_AUTH_TOKEN") not in r.stdout  # never echoed to a shared screen


def test_rotate_verified_against_running_connector(running):
    old = running.get("MCP_AUTH_TOKEN")
    r = running.run("rotate-token.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verified" in r.stdout
    new = running.get("MCP_AUTH_TOKEN")
    assert running.token_ok(new) == 200
    assert running.token_ok(old) == 404


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_rotate_reports_failure_when_restart_fails(running, mode):
    old = running.get("MCP_AUTH_TOKEN")
    r = running.run("rotate-token.sh", STUB_KICKSTART=mode)
    assert r.returncode != 0
    assert "Verified" not in r.stdout
    assert "OLD connector URL may STILL WORK" in r.stderr
    # ...and that warning is true: the old process still accepts the old token.
    assert running.token_ok(old) == 200


def test_sending_switch_verified(running):
    r = running.run("sending.sh", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verified: running remote connector has sending ON" in r.stdout
    running.token_ok(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" in running.last_tools

    r = running.run("sending.sh", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    running.token_ok(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" not in running.last_tools

    r = running.run("sending.sh", "status")
    assert "running remote connector: sending OFF" in r.stdout


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_sending_off_failure_is_loud(running, mode):
    assert running.run("sending.sh", "on").returncode == 0
    r = running.run("sending.sh", "off", STUB_KICKSTART=mode)
    assert r.returncode != 0
    assert "Sending may STILL BE ENABLED" in r.stderr
    running.token_ok(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" in running.last_tools  # warning is accurate


def test_sending_when_not_running(env):
    env.write_env()
    r = env.run("sending.sh", "on")
    assert r.returncode == 0 and "applies on scripts/start.sh" in r.stdout
    assert env.get("WHATSAPP_ALLOW_SEND") == "true"


def test_start_fails_when_bootstrap_fails(env):
    env.write_env()
    (env.home / "Library/LaunchAgents").mkdir(parents=True)
    for label in ("com.wa.bridge", "com.wa.connector", "com.wa.tunnel"):
        (env.home / f"Library/LaunchAgents/{label}.plist").write_text("<plist/>")
    r = env.run("start.sh", STUB_BOOTSTRAP="fail")
    assert r.returncode != 0 and "could not start" in r.stderr


# ---- Allowlist: local verification works, remote restriction unchanged -----

def test_allowlist_still_enforced_on_tcp(env):
    env.allowlist = True
    env.write_env(MCP_ALLOWED_IPS=ANTHROPIC_LIKE)
    (env.home / "Library/LaunchAgents").mkdir(parents=True)
    for label in ("com.wa.bridge", "com.wa.connector", "com.wa.tunnel"):
        (env.home / f"Library/LaunchAgents/{label}.plist").write_text("<plist/>")
    assert env.run("start.sh").returncode == 0
    token = env.get("MCP_AUTH_TOKEN")
    assert env.mcp_status(token, cf_ip="160.79.105.10") == 200   # allowed remote
    assert env.mcp_status(token, cf_ip="203.0.113.9") == 404     # other remote
    assert env.mcp_status(token) == 404                          # TCP from localhost too
    # The local socket is owner-only and inside an owner-only directory.
    assert stat.S_IMODE(env.admin_socket.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.admin_socket.parent.stat().st_mode) == 0o700
    # And it still needs the token.
    r = subprocess.run([sys.executable, str(ROOT / "connector/verify.py"), "token"],
                       env={**env.vars, "WA_CONFIG": str(env.env_file), "MCP_AUTH_TOKEN": "B" * 43},
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "NOT accepted (HTTP 404)" in r.stderr


# ---- launchctl stub: bounded waits, zombies handled (test infrastructure) ---

def _load_fake_connector(env, pid: int):
    (env.state / "com.wa.connector.loaded").touch()
    (env.state / "com.wa.connector.pid").write_text(str(pid))


def test_stub_does_not_hang_on_zombie(env):
    child = subprocess.Popen(["true"])       # exits at once; not reaped yet -> zombie
    time.sleep(0.3)
    _load_fake_connector(env, child.pid)
    start = time.monotonic()
    r = subprocess.run([BASH, str(STUBS / "launchctl"), "bootout", "gui/0/com.wa.connector"],
                       env=env.vars, capture_output=True, text=True, timeout=15)
    assert r.returncode == 0 and time.monotonic() - start < 5
    child.wait()


def test_stub_kills_process_ignoring_sigterm(env):
    child = subprocess.Popen(["bash", "-c", "trap '' TERM; sleep 60"])
    time.sleep(0.3)
    _load_fake_connector(env, child.pid)
    start = time.monotonic()
    r = subprocess.run([BASH, str(STUBS / "launchctl"), "bootout", "gui/0/com.wa.connector"],
                       env=env.vars, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and time.monotonic() - start < 12
    assert "SIGKILL" in r.stderr
    assert child.wait(timeout=5) == -9
