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

    def mcp_status(self, token: str) -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/mcp", method="POST",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                self.last_tools = [t["name"] for t in json.loads(r.read())["result"]["tools"]]
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def stop(self):
        pid_file = self.state / "com.wa.connector.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 15)
            except (ProcessLookupError, ValueError):
                pass


@pytest.fixture
def env(tmp_path, db):
    e = Env(tmp_path, db)
    yield e
    e.stop()


@pytest.fixture
def running(env):
    """Connector loaded and serving, like after scripts/start.sh."""
    env.write_env()
    (env.home / "Library/LaunchAgents").mkdir(parents=True)
    for label in ("com.wa.bridge", "com.wa.connector", "com.wa.tunnel"):
        (env.home / f"Library/LaunchAgents/{label}.plist").write_text("<plist/>")
    r = env.run("start.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.mcp_status(env.get("MCP_AUTH_TOKEN")) == 200
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
    assert running.mcp_status(new) == 200
    assert running.mcp_status(old) == 404


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_rotate_reports_failure_when_restart_fails(running, mode):
    old = running.get("MCP_AUTH_TOKEN")
    r = running.run("rotate-token.sh", STUB_KICKSTART=mode)
    assert r.returncode != 0
    assert "Verified" not in r.stdout
    assert "OLD connector URL may STILL WORK" in r.stderr
    # ...and that warning is true: the old process still accepts the old token.
    assert running.mcp_status(old) == 200


def test_sending_switch_verified(running):
    r = running.run("sending.sh", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verified: running remote connector has sending ON" in r.stdout
    running.mcp_status(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" in running.last_tools

    r = running.run("sending.sh", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    running.mcp_status(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" not in running.last_tools

    r = running.run("sending.sh", "status")
    assert "running remote connector: sending OFF" in r.stdout


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_sending_off_failure_is_loud(running, mode):
    assert running.run("sending.sh", "on").returncode == 0
    r = running.run("sending.sh", "off", STUB_KICKSTART=mode)
    assert r.returncode != 0
    assert "Sending may STILL BE ENABLED" in r.stderr
    running.mcp_status(running.get("MCP_AUTH_TOKEN"))
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
