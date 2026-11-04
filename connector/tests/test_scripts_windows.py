"""
Windows platform layer (windows\\*.ps1), run with Windows PowerShell 5.1,
the version every Windows 10/11 machine has.

Only the external tools the installer calls (git, go, gcc, uv, cloudflared)
are stubbed (tests/stubs_windows/*.cmd). Starting, stopping, supervising and
verifying the connector are REAL: real hidden supervisor processes running
the real server, checked over HTTP.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import net

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell scripts")

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "windows"
STUBS = Path(__file__).resolve().parent / "stubs_windows"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
POWERSHELL = os.environ.get("WA_TEST_POWERSHELL", "powershell.exe")
ANTHROPIC_LIKE = "160.79.104.0/21"


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
        self.logs = tmp / "logs"
        self.startup = tmp / "startup"
        self.tls_dir = tmp / "tls"
        self.db = db
        self.port = free_port()
        self.admin_port = free_port()
        while self.admin_port == self.port:
            self.admin_port = free_port()
        self.allowlist = False
        self.vars = {
            **os.environ,
            "PATH": f"{STUBS}{os.pathsep}{os.environ['PATH']}",
            "STUB_STATE": str(self.state),
            "WA_ENV_FILE": str(self.env_file),
            "WA_PYTHON": sys.executable,
            "WA_LOG_DIR": str(self.logs),
            "WA_STARTUP_DIR": str(self.startup),
            "WA_SERVICES": "connector",      # bridge/tunnel need the real WhatsApp/Cloudflare binaries
            "WA_RESTART_TIMEOUT": "30",
            "WA_RESTART_DELAY": "1",
            "UPSTREAM": str(tmp / "upstream"),
            "MCP_TLS_DIR": str(self.tls_dir),
        }

    def write_env(self, **extra):
        values = {
            "WA_DB_PATH": str(self.db), "WHATSAPP_ALLOW_SEND": "false",
            "MCP_AUTH_TOKEN": "A" * 43, "MCP_PUBLIC_HOSTNAME": "", "MCP_PORT": str(self.port),
            "MCP_ADMIN_CHANNEL": "tcp", "MCP_ADMIN_PORT": str(self.admin_port),
            "MCP_TLS_DIR": str(self.tls_dir),
            **extra,
        }
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")

    def get(self, key: str) -> str:
        for line in self.env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return ""

    def run(self, script: str, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(WIN / script), *args],
            env={**self.vars, **env}, capture_output=True, text=True, timeout=180)

    def status(self, token: str, cf_ip: str = "") -> int:
        """tools/list on the PUBLIC port, as a remote caller via the tunnel would."""
        if self.allowlist and not cf_ip:
            cf_ip = "160.79.105.10"
        req = urllib.request.Request(
            net.url(self.port, "/mcp"), method="POST",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     **({"CF-Connecting-IP": cf_ip} if cf_ip else {})})
        status, raw = net.request(req, self.tls_dir, timeout=10)
        if status == 200:
            self.last_tools = [t["name"] for t in json.loads(raw)["result"]["tools"]]
        return status

    def healthy(self) -> bool:
        return net.healthy(self.port, self.tls_dir)

    def connector_pids(self) -> list[int]:
        """Real connector processes (server.py --transport http) for this test's port."""
        # Name filter: only the Python server itself, not the cmd.exe that
        # redirects its output, nor this very PowerShell query (whose own
        # command line contains the search strings).
        ps = ("Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Where-Object { "
              "$_.CommandLine -and $_.CommandLine.Contains('server.py') -and "
              "$_.CommandLine.Contains('--transport http') } | ForEach-Object { $_.ProcessId }")
        out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60, env=self.vars).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]


@pytest.fixture
def env(tmp_path, db):
    e = Env(tmp_path, db)
    yield e
    e.run("stop.ps1")


@pytest.fixture(params=["no-allowlist", "allowlist"])
def running(env, request):
    env.allowlist = request.param == "allowlist"
    env.write_env(**({"MCP_ALLOWED_IPS": ANTHROPIC_LIKE} if env.allowlist else {}))
    r = env.run("start.ps1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.status(env.get("MCP_AUTH_TOKEN")) == 200
    return env


# ---- install ---------------------------------------------------------------

def fake_upstream(env: Env, main_go: str) -> Path:
    bridge = Path(env.vars["UPSTREAM"]) / "whatsapp-bridge"
    bridge.mkdir(parents=True)
    (Path(env.vars["UPSTREAM"]) / ".git").mkdir()
    (bridge / "main.go").write_text(main_go, encoding="utf-8")
    return bridge / "main.go"


def acl_rules(path: Path) -> dict:
    """Every access rule on `path` (explicit and inherited), plus whether
    inheritance is blocked and who the current user is, as a SID."""
    ps = ("$a = [IO.File]::GetAccessControl('%s'); "
          "$r = @($a.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | "
          "ForEach-Object { @{ sid = $_.IdentityReference.Value; type = [string]$_.AccessControlType; "
          "rights = [string]$_.FileSystemRights; inherited = $_.IsInherited } }); "
          "@{ protected = $a.AreAccessRulesProtected; me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
          "rules = $r } | ConvertTo-Json -Depth 4 -Compress") % str(path).replace("'", "''")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    data = json.loads(out.stdout)
    rules = data["rules"]
    data["rules"] = rules if isinstance(rules, list) else [rules]
    return data


def test_install_fresh_without_hostname(env):
    main_go = fake_upstream(env, (FIXTURES / "upstream_main.go.txt").read_text())
    r = env.run("install.ps1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Install done" in r.stdout
    assert len(env.get("MCP_AUTH_TOKEN")) == 43
    raw = env.env_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM would corrupt the first .env key"
    acl = acl_rules(env.env_file)
    assert acl["protected"], acl                        # nothing inherited from the folder
    assert [(r["sid"], r["type"], r["inherited"]) for r in acl["rules"]] == [(acl["me"], "Allow", False)], acl
    patched = main_go.read_text(encoding="utf-8")
    assert 'fmt.Sprintf("127.0.0.1:%d", port)' in patched
    assert "sender" not in patched.split("// Start the server")[0].split("Log based on message type")[1]
    assert (env.startup / "wa-connector.lnk").exists()
    assert '"whatsapp-family"' in r.stdout              # Claude Desktop snippet printed
    calls = (env.state / "calls").read_text()
    assert "go build" in calls and "uv sync" in calls
    assert patched.count("context.Background()") == 5
    assert "go get go.mau.fi/whatsmeow@v0.0.0-20261102152715-54038813c47f" in calls and "go mod tidy" in calls
    assert "checkout -q -- ." in calls


def test_install_rerun_keeps_token(env):
    fake_upstream(env, (FIXTURES / "upstream_main.go.txt").read_text())
    assert env.run("install.ps1").returncode == 0
    token = env.get("MCP_AUTH_TOKEN")
    r = env.run("install.ps1")
    assert r.returncode == 0, r.stderr
    assert env.get("MCP_AUTH_TOKEN") == token


def test_install_stops_if_upstream_changed(env):
    fake_upstream(env, "package main\nfunc main() {}\n")
    r = env.run("install.ps1")
    assert r.returncode != 0 and "Patch failed" in r.stderr
    assert not env.env_file.exists()


def test_install_reports_missing_tools(env):
    fake_upstream(env, (FIXTURES / "upstream_main.go.txt").read_text())
    hidden = env.tmp / "only-some-tools"
    hidden.mkdir()
    shutil.copy(STUBS / "git.cmd", hidden / "git.cmd")
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.pathsep.join([str(hidden), str(Path(windir) / "System32"),
                            str(Path(windir) / "System32" / "WindowsPowerShell" / "v1.0")])
    r = env.run("install.ps1", PATH=path)
    assert r.returncode != 0
    for tool in ("go", "gcc", "uv", "cloudflared"):
        assert f"  {tool}:" in r.stderr


# ---- start / stop / supervise ----------------------------------------------

def test_stop_really_stops(running):
    assert running.connector_pids()
    r = running.run("stop.ps1")
    assert r.returncode == 0, r.stderr
    assert not running.healthy()
    assert running.connector_pids() == []


def test_supervisor_restarts_crashed_connector(running):
    # A venv's python.exe on Windows is a launcher that starts the real
    # interpreter as a child, so one connector is 1 or 2 python processes.
    pids = running.connector_pids()
    assert 1 <= len(pids) <= 2, pids
    for pid in pids:  # crash: everything belonging to the connector dies
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        new = running.connector_pids()
        if new and not set(new) & set(pids) and running.healthy():
            break
        time.sleep(0.5)
    else:
        pytest.fail("supervisor did not restart the connector")
    assert running.status(running.get("MCP_AUTH_TOKEN")) == 200


def test_start_fails_when_service_does_not_come_up(env):
    env.write_env()
    r = env.run("start.ps1", WA_TEST_FAULT="start-noop", WA_RESTART_TIMEOUT="3")
    assert r.returncode != 0 and "did not become healthy" in r.stderr


def test_status_reports_running(running):
    r = running.run("status.ps1")
    assert "running  connector" in r.stdout and "local  health: ok" in r.stdout


# ---- token rotation --------------------------------------------------------

def test_rotate_when_not_running(env):
    env.write_env()
    old = env.get("MCP_AUTH_TOKEN")
    r = env.run("rotate-token.ps1")
    assert r.returncode == 0, r.stderr
    assert env.get("MCP_AUTH_TOKEN") != old and len(env.get("MCP_AUTH_TOKEN")) == 43
    assert "not running" in r.stdout
    assert env.get("MCP_AUTH_TOKEN") not in r.stdout


def test_rotate_verified_against_running_connector(running):
    old = running.get("MCP_AUTH_TOKEN")
    r = running.run("rotate-token.ps1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verified" in r.stdout
    assert running.status(running.get("MCP_AUTH_TOKEN")) == 200
    assert running.status(old) == 404


def test_rotate_reports_failure_when_stop_fails(running):
    old = running.get("MCP_AUTH_TOKEN")
    r = running.run("rotate-token.ps1", WA_TEST_FAULT="stop-fail")
    assert r.returncode != 0 and "Verified" not in r.stdout
    assert "OLD connector URL may STILL WORK" in r.stderr
    assert running.status(old) == 200          # the warning is true


def test_rotate_reports_failure_when_restart_does_not_come_up(running):
    r = running.run("rotate-token.ps1", WA_TEST_FAULT="start-noop", WA_RESTART_TIMEOUT="3")
    assert r.returncode != 0 and "OLD connector URL may STILL WORK" in r.stderr


# ---- sending switch --------------------------------------------------------

def test_sending_switch_verified(running):
    r = running.run("sending.ps1", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verified: running remote connector has sending ON" in r.stdout
    running.status(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" in running.last_tools

    r = running.run("sending.ps1", "off")
    assert r.returncode == 0, r.stdout + r.stderr
    running.status(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" not in running.last_tools
    assert "running remote connector: sending OFF" in running.run("sending.ps1", "status").stdout


def test_sending_off_failure_is_loud(running):
    assert running.run("sending.ps1", "on").returncode == 0
    r = running.run("sending.ps1", "off", WA_TEST_FAULT="stop-fail")
    assert r.returncode != 0 and "Sending may STILL BE ENABLED" in r.stderr
    running.status(running.get("MCP_AUTH_TOKEN"))
    assert "send_message" in running.last_tools   # the warning is true


def test_sending_when_not_running(env):
    env.write_env()
    r = env.run("sending.ps1", "on")
    assert r.returncode == 0 and "applies on windows\\start.ps1" in r.stdout
    assert env.get("WHATSAPP_ALLOW_SEND") == "true"


def test_allowlist_still_enforced_on_public_port(running):
    if not running.allowlist:
        pytest.skip("allowlist variant only")
    token = running.get("MCP_AUTH_TOKEN")
    assert running.status(token, cf_ip="203.0.113.9") == 404


# ---- connector URL -----------------------------------------------------------

def test_connector_url_requires_hostname(env):
    env.write_env()
    r = env.run("connector-url.ps1", "-Print")
    assert r.returncode != 0 and "MCP_PUBLIC_HOSTNAME not set" in r.stderr


def test_connector_url_print(env):
    env.write_env(MCP_PUBLIC_HOSTNAME="wa.example.com")
    r = env.run("connector-url.ps1", "-Print")
    assert r.returncode == 0 and r.stdout.strip() == f"https://wa.example.com/mcp/{'A' * 43}"
