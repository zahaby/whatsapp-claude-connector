"""
Regression tests: the admin socket must never delete or re-permission
anything it did not create.
"""

import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from server import AdminSocketError, bind_admin_socket, remove_admin_socket

SERVER = Path(__file__).resolve().parents[1] / "server.py"


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="wa", dir="/tmp"))  # short: Unix socket path limit
    os.chmod(d, 0o700)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def stale_socket(path: Path) -> None:
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.close()  # file stays, nobody listening


# ---- refused: never deleted -------------------------------------------------

def test_regular_file_is_refused_and_kept(base):
    target = base / "admin.sock"
    target.write_text("precious")
    with pytest.raises(AdminSocketError, match="not a socket"):
        bind_admin_socket(target)
    assert target.read_text() == "precious"


def test_symlink_to_file_is_refused_and_both_kept(base):
    real = base / "real.txt"
    real.write_text("precious")
    link = base / "admin.sock"
    link.symlink_to(real)
    with pytest.raises(AdminSocketError, match="symlink"):
        bind_admin_socket(link)
    assert link.is_symlink() and real.read_text() == "precious"


def test_symlink_to_stale_socket_is_refused(base):
    sock = base / "elsewhere.sock"
    stale_socket(sock)
    link = base / "admin.sock"
    link.symlink_to(sock)
    with pytest.raises(AdminSocketError, match="symlink"):
        bind_admin_socket(link)
    assert link.is_symlink() and stat.S_ISSOCK(os.lstat(sock).st_mode)


def test_dangling_symlink_is_refused(base):
    link = base / "admin.sock"
    link.symlink_to(base / "missing")
    with pytest.raises(AdminSocketError, match="symlink"):
        bind_admin_socket(link)
    assert link.is_symlink()


def test_directory_is_refused(base):
    d = base / "admin.sock"
    d.mkdir()
    (d / "keep").write_text("x")
    with pytest.raises(AdminSocketError, match="not a socket"):
        bind_admin_socket(d)
    assert (d / "keep").exists()


def test_live_socket_is_refused_and_keeps_working(base):
    path = base / "admin.sock"
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(path))
    live.listen(1)
    try:
        with pytest.raises(AdminSocketError, match="already serving"):
            bind_admin_socket(path)
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(path))  # still reachable: we did not unlink it
        c.close()
    finally:
        live.close()


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create another user's file")
def test_other_users_socket_is_refused(base):
    path = base / "admin.sock"
    stale_socket(path)
    os.chown(path, 65534, 65534)
    with pytest.raises(AdminSocketError, match="another user"):
        bind_admin_socket(path)
    assert path.exists()


# ---- stale socket: the one thing that may be removed ------------------------

def test_stale_socket_is_replaced(base):
    path = base / "admin.sock"
    stale_socket(path)
    old_ino = os.lstat(path).st_ino
    sock, identity = bind_admin_socket(path)
    try:
        assert identity[1] == os.lstat(path).st_ino
        assert mode(path) == 0o600
        sock.listen(1)
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(path))
        c.close()
    finally:
        sock.close()
    assert old_ino is not None


# ---- directory permissions --------------------------------------------------

def test_missing_directory_created_0700(base):
    sock, _ = bind_admin_socket(base / "new" / "run" / "admin.sock")
    sock.close()
    assert mode(base / "new" / "run") == 0o700


def test_existing_private_directory_mode_untouched(base):
    d = base / "shared-ish"
    d.mkdir()
    os.chmod(d, 0o755)
    sock, _ = bind_admin_socket(d / "admin.sock")
    sock.close()
    assert mode(d) == 0o755  # not silently tightened (or loosened)


@pytest.mark.parametrize("bad_mode", [0o777, 0o775, 0o1777])
def test_group_or_world_writable_directory_refused_untouched(base, bad_mode):
    d = base / "open"
    d.mkdir()
    os.chmod(d, bad_mode)
    with pytest.raises(AdminSocketError, match="writable by group/others"):
        bind_admin_socket(d / "admin.sock")
    assert mode(d) == bad_mode and not (d / "admin.sock").exists()


def test_symlinked_directory_refused(base):
    real = base / "real"
    real.mkdir()
    os.chmod(real, 0o700)
    (base / "linkdir").symlink_to(real)
    with pytest.raises(AdminSocketError, match="symlink"):
        bind_admin_socket(base / "linkdir" / "admin.sock")
    assert mode(real) == 0o700 and not (real / "admin.sock").exists()


def test_directory_path_is_a_file_refused(base):
    f = base / "notadir"
    f.write_text("x")
    with pytest.raises((AdminSocketError, NotADirectoryError, FileExistsError)):
        bind_admin_socket(f / "admin.sock")
    assert f.read_text() == "x"


# ---- shutdown removes only what this process created ------------------------

def test_shutdown_removes_own_socket(base):
    path = base / "admin.sock"
    sock, identity = bind_admin_socket(path)
    sock.close()
    remove_admin_socket(path, identity)
    assert not path.exists()


def test_shutdown_leaves_replacement_file_alone(base):
    path = base / "admin.sock"
    sock, identity = bind_admin_socket(path)
    sock.close()
    path.unlink()
    path.write_text("someone else's file")
    remove_admin_socket(path, identity)
    assert path.read_text() == "someone else's file"


def test_shutdown_leaves_replacement_socket_alone(base):
    path = base / "admin.sock"
    sock, identity = bind_admin_socket(path)
    sock.close()
    path.unlink()
    stale_socket(path)  # a different socket at the same path
    remove_admin_socket(path, identity)
    assert path.exists()


# ---- end to end: the server refuses to start, file intact ------------------

def test_server_refuses_to_start_over_regular_file(base, db, tmp_path):
    target = base / "admin.sock"
    target.write_text("precious")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db),
           "MCP_AUTH_TOKEN": "z" * 43, "MCP_PORT": str(port), "MCP_ADMIN_SOCKET": str(target)}
    r = subprocess.run([sys.executable, str(SERVER), "--transport", "http"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "Refusing to start" in r.stderr and "not a socket" in r.stderr
    assert target.read_text() == "precious"
