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
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(__import__("sys").platform == "win32", reason="Unix socket admin channel (macOS/Linux)")

from admin_unix import AdminSocketError, bind_admin_socket, remove_admin_socket

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
    """A socket file this connector did NOT create (no lock-file record)."""
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.close()  # file stays, nobody listening


def fill_backlog(path: Path) -> list:
    """Queue unaccepted connections until the listener refuses more, then
    prove it is busy: a further connect must fail (ECONNREFUSED on macOS,
    EAGAIN on Linux). Returns the queued clients so the test can drain."""
    clients = []
    for _ in range(256):
        c = socket.socket(socket.AF_UNIX)
        c.setblocking(False)
        try:
            c.connect(str(path))
            clients.append(c)
        except OSError:
            c.close()
            break
    probe = socket.socket(socket.AF_UNIX)
    probe.setblocking(False)
    with pytest.raises(OSError):
        probe.connect(str(path))  # busy: a connect-based staleness test would be fooled
    probe.close()
    return clients


def drain(listener, clients) -> None:
    listener.setblocking(False)
    for _ in clients:
        try:
            listener.accept()[0].close()
        except BlockingIOError:
            break
    for c in clients:
        c.close()


CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from admin_unix import bind_admin_socket
    a = bind_admin_socket(Path(sys.argv[1]))
    a.sock.listen(1)
    print("ready", flush=True)
    time.sleep(60)
""")


def spawn_connector(path: Path) -> subprocess.Popen:
    """A separate process that binds the socket the way the server does."""
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(path), str(SERVER.parent)],
                             stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    return child


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


def test_busy_live_connector_socket_never_deleted(base):
    # Regression: a connector whose backlog is full answers ECONNREFUSED on
    # macOS, exactly like a stale socket. It must still never be deleted.
    path = base / "admin.sock"
    child = spawn_connector(path)
    try:
        before = os.lstat(path)
        clients = fill_backlog(path)
        with pytest.raises(AdminSocketError, match="lock is held"):
            bind_admin_socket(path)
        after = os.lstat(path)
        assert (after.st_ino, after.st_ctime_ns) == (before.st_ino, before.st_ctime_ns)
        for c in clients:
            c.close()
    finally:
        child.terminate()
        child.wait(5)


def test_second_instance_in_same_process_refused(base):
    path = base / "admin.sock"
    first = bind_admin_socket(path)
    try:
        first.sock.listen(1)
        with pytest.raises(AdminSocketError, match="lock is held"):
            bind_admin_socket(path)
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(path))  # first instance untouched
        first.sock.accept()[0].close()
        c.close()
    finally:
        first.close()


def test_busy_foreign_socket_never_deleted(base):
    # A live listener this connector did not create, with a full backlog.
    path = base / "admin.sock"
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(path))
    live.listen(1)
    try:
        clients = fill_backlog(path)
        with pytest.raises(AdminSocketError, match="did not create"):
            bind_admin_socket(path)
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        drain(live, clients)
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(path))  # still the same live socket
        c.close()
    finally:
        live.close()


def test_foreign_stale_socket_refused_and_kept(base):
    path = base / "admin.sock"
    stale_socket(path)
    with pytest.raises(AdminSocketError, match="did not create"):
        bind_admin_socket(path)
    assert stat.S_ISSOCK(os.lstat(path).st_mode)


def test_lock_file_symlink_refused(base):
    target = base / "victim.txt"
    target.write_text("precious")
    (base / "admin.sock.lock").symlink_to(target)
    with pytest.raises(AdminSocketError, match="lock file"):
        bind_admin_socket(base / "admin.sock")
    assert target.read_text() == "precious"


def test_lock_file_directory_refused(base):
    (base / "admin.sock.lock").mkdir()
    with pytest.raises(AdminSocketError, match="lock file"):
        bind_admin_socket(base / "admin.sock")


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create another user's file")
def test_other_users_socket_is_refused(base):
    path = base / "admin.sock"
    stale_socket(path)
    os.chown(path, 65534, 65534)
    with pytest.raises(AdminSocketError, match="another user"):
        bind_admin_socket(path)
    assert path.exists()


# ---- stale socket: the one thing that may be removed ------------------------

def test_own_socket_left_by_killed_connector_is_replaced(base):
    path = base / "admin.sock"
    child = spawn_connector(path)
    old = os.lstat(path)
    child.kill()  # SIGKILL: no cleanup ran, socket file remains, kernel drops the lock
    child.wait(5)
    assert stat.S_ISSOCK(os.lstat(path).st_mode)
    a = bind_admin_socket(path)
    try:
        assert (a.identity[1], a.identity[2]) != (old.st_ino, old.st_ctime_ns)
        assert mode(path) == 0o600
        a.sock.listen(1)
        c = socket.socket(socket.AF_UNIX)
        c.connect(str(path))
        a.sock.accept()[0].close()
        c.close()
    finally:
        a.close()


def test_own_leftover_replaced_by_other_socket_is_refused(base):
    path = base / "admin.sock"
    bind_admin_socket(path).close()  # simulate a crash: lock released, file left
    path.unlink()
    stale_socket(path)              # someone else's socket now sits there
    with pytest.raises(AdminSocketError, match="did not create"):
        bind_admin_socket(path)
    assert path.exists()


# ---- directory permissions --------------------------------------------------

def test_missing_directory_created_0700(base):
    bind_admin_socket(base / "new" / "run" / "admin.sock").close()
    assert mode(base / "new" / "run") == 0o700


def test_existing_private_directory_mode_untouched(base):
    d = base / "shared-ish"
    d.mkdir()
    os.chmod(d, 0o755)
    bind_admin_socket(d / "admin.sock").close()
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
    a = bind_admin_socket(path)
    identity = a.identity
    a.close()
    remove_admin_socket(path, identity)
    assert not path.exists()


def test_shutdown_leaves_replacement_file_alone(base):
    path = base / "admin.sock"
    a = bind_admin_socket(path)
    identity = a.identity
    a.close()
    path.unlink()
    path.write_text("someone else's file")
    remove_admin_socket(path, identity)
    assert path.read_text() == "someone else's file"


def test_shutdown_leaves_replacement_socket_alone(base):
    path = base / "admin.sock"
    a = bind_admin_socket(path)
    identity = a.identity
    a.close()
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


# ---- lock file: never overwrite a file that is not ours --------------------

from admin_unix import LOCK_MAGIC, LOCK_MAX_BYTES  # noqa: E402


@pytest.mark.parametrize("content", [
    b"my notes\n",                          # Codex's case: an unrelated text file
    b"12 34 56\n",                          # looks like a record, but no magic line
    b"\x00\xffbinary\x01",                  # binary
    LOCK_MAGIC + b"1 2 3\nextra line\n",    # magic, then more than one record line
    LOCK_MAGIC + b"not numbers\n",          # magic, then non-record text
    LOCK_MAGIC + b"0" * LOCK_MAX_BYTES,     # magic prefix on an oversized file
    b" " + LOCK_MAGIC,                      # magic not at the start
])
def test_unrecognised_lock_file_refused_and_byte_identical(base, content):
    lock = base / "admin.sock.lock"
    lock.write_bytes(content)
    os.chmod(lock, 0o640)
    before = os.stat(lock)
    with pytest.raises(AdminSocketError, match="not a wa-connector lock file"):
        bind_admin_socket(base / "admin.sock")
    after = os.stat(lock)
    assert lock.read_bytes() == content
    assert (after.st_size, after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == \
           (before.st_size, before.st_mtime_ns, 0o640)
    assert not (base / "admin.sock").exists()


def test_unrecognised_lock_file_refused_even_without_socket_dir_prepared(base):
    d = base / "fresh"
    d.mkdir(mode=0o700)
    (d / "admin.sock.lock").write_text("unrelated")
    with pytest.raises(AdminSocketError):
        bind_admin_socket(d / "admin.sock")
    assert (d / "admin.sock.lock").read_text() == "unrelated"


def test_new_lock_file_format(base):
    a = bind_admin_socket(base / "admin.sock")
    try:
        lock = base / "admin.sock.lock"
        data = lock.read_bytes()
        assert data.startswith(LOCK_MAGIC)
        assert tuple(int(x) for x in data[len(LOCK_MAGIC):].split()) == a.identity
        assert mode(lock) == 0o600
    finally:
        a.close()


@pytest.mark.parametrize("content", [b"", LOCK_MAGIC])
def test_empty_or_magic_only_lock_file_reused(base, content):
    # Left by a crash right after creating the lock file: safe to reuse.
    lock = base / "admin.sock.lock"
    lock.write_bytes(content)
    a = bind_admin_socket(base / "admin.sock")
    a.close()
    assert lock.read_bytes().startswith(LOCK_MAGIC)


def test_own_lock_file_reused_across_restarts(base):
    path = base / "admin.sock"
    first = bind_admin_socket(path)
    first.close()                      # crash: socket file and lock file left behind
    second = bind_admin_socket(path)   # recognised as ours, stale socket replaced
    try:
        assert second.identity != first.identity
    finally:
        second.close()


def test_server_refuses_to_start_over_unrelated_lock_file(base, db, tmp_path):
    lock = base / "admin.sock.lock"
    lock.write_text("precious notes\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db),
           "MCP_AUTH_TOKEN": "z" * 43, "MCP_PORT": str(port), "MCP_ADMIN_SOCKET": str(base / "admin.sock")}
    r = subprocess.run([sys.executable, str(SERVER), "--transport", "http"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "not a wa-connector lock file" in r.stderr
    assert lock.read_text() == "precious notes\n"
