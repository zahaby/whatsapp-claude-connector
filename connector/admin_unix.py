"""
Local admin channel for macOS / Linux: an owner-only Unix socket.

The connector serves the same token-protected MCP app on this socket as on
its TCP port, but without the IP allowlist, so connector/verify.py can check
the live server even when MCP_ALLOWED_IPS admits only Anthropic's ranges.
The Cloudflare tunnel cannot reach a Unix socket.

Safety rules (each covered by tests/test_admin_socket.py):
  * never chmod an existing directory; refuse group/world-writable ones
  * never delete anything except a stale socket this connector recorded,
    proven stale by a free flock on admin.sock.lock (not by connect():
    a busy listener on macOS also refuses connections)
  * never truncate a lock file that is not in our own format
  * on shutdown remove only the exact socket this process bound
"""

from __future__ import annotations

import http.client
import os
import socket
import stat
from pathlib import Path
from typing import Optional

from admin_channel import AdminChannelError


class AdminSocketError(AdminChannelError):
    """MCP_ADMIN_SOCKET is unsafe to use; the server refuses to start."""


def _prepare_admin_dir(directory: Path) -> None:
    """Create the socket's directory 0700 if missing. An EXISTING directory
    is never chmod-ed (it may be shared, e.g. /tmp or the user's home); it
    must be a real directory we own that no one else can write to, because
    anyone who can write there could swap the socket."""
    try:
        directory.mkdir(mode=0o700, parents=True)
        os.chmod(directory, 0o700)  # mkdir's mode is filtered by umask; this dir is ours
        return
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode):
        raise AdminSocketError(f"socket directory is a symlink, refusing: {directory}")
    if not stat.S_ISDIR(st.st_mode):
        raise AdminSocketError(f"socket directory is not a directory: {directory}")
    if st.st_uid != os.geteuid():
        raise AdminSocketError(f"socket directory is not owned by this user: {directory}")
    if st.st_mode & 0o022:
        raise AdminSocketError(
            f"socket directory is writable by group/others (mode {stat.S_IMODE(st.st_mode):o}): {directory}. "
            "Use a private directory, e.g. leave MCP_ADMIN_SOCKET unset.")


LOCK_MAGIC = b"wa-connector-admin-lock v1\n"
LOCK_MAX_BYTES = 4096


def _lock_content_is_ours(fd: int) -> bool:
    """An existing lock file may be reused only if it is empty (created but
    never written, e.g. a crash right after creation) or is exactly in our
    format: the magic line, then optionally one record line. Anything else
    belongs to someone else and must not be truncated."""
    size = os.fstat(fd).st_size
    if size == 0:
        return True
    if size > LOCK_MAX_BYTES:
        return False
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, LOCK_MAX_BYTES + 1)
    if not data.startswith(LOCK_MAGIC):
        return False
    rest = data[len(LOCK_MAGIC):]
    try:
        text = rest.decode("ascii")
    except UnicodeDecodeError:
        return False
    lines = text.splitlines()
    return len(lines) <= 1 and all(ch in "0123456789 \n" for ch in text)


def _open_lock(lock_path: Path) -> int:
    """Open the lock file next to the socket and take an exclusive,
    non-blocking flock. The kernel drops the lock when the process exits for
    any reason, including SIGKILL, so "lock is free" reliably means "no
    connector is running on this socket".

    A new file is created with O_EXCL (so we know it is ours) and gets the
    magic line at once. An existing file is reused only if its content is in
    our format; otherwise startup is refused and the file is left untouched.
    Nothing is written before that check."""
    import fcntl
    nofollow, cloexec = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_CLOEXEC", 0)
    created = True
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow | cloexec, 0o600)
    except FileExistsError:
        created = False
        try:
            fd = os.open(lock_path, os.O_RDWR | nofollow | cloexec)  # no O_CREAT, no O_TRUNC
        except OSError as e:  # O_NOFOLLOW on a symlink -> ELOOP
            raise AdminSocketError(f"cannot open lock file {lock_path} safely ({e}); refusing")
    except OSError as e:
        raise AdminSocketError(f"cannot create lock file {lock_path} ({e}); refusing")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise AdminSocketError(f"lock file {lock_path} is not a regular file owned by this user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AdminSocketError(f"another connector is running on {lock_path.with_suffix('')} "
                                   "(its lock is held); refusing to start a second one")
        if created:
            os.write(fd, LOCK_MAGIC)
            os.fsync(fd)
        elif not _lock_content_is_ours(fd):
            raise AdminSocketError(
                f"{lock_path} exists but is not a wa-connector lock file; refusing to overwrite it. "
                "Move it away or set MCP_ADMIN_SOCKET to another path.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_record(fd: int) -> Optional[tuple[int, int, int]]:
    """The recorded socket identity, or None if there is none (new file,
    empty file, or a record cut short by a crash)."""
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, LOCK_MAX_BYTES)
    if not data.startswith(LOCK_MAGIC):
        return None
    parts = data[len(LOCK_MAGIC):].split()
    if len(parts) != 3:
        return None
    try:
        dev, ino, ctime = (int(x) for x in parts)
    except ValueError:
        return None
    return dev, ino, ctime


def _write_record(fd: int, identity: tuple[int, int, int]) -> None:
    """Only ever called on a file that passed _open_lock's ownership check."""
    payload = LOCK_MAGIC + (" ".join(str(x) for x in identity) + "\n").encode()
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.fsync(fd)


def _clear_stale_socket(path: Path, lock_fd: int) -> None:
    """Remove `path` only if it is provably a leftover of a previous
    connector: we hold the lock (so no connector is running) AND the file
    is exactly the socket the previous connector recorded.

    Connecting to the socket is NOT used as evidence: on macOS a live but
    busy listener (full backlog) also returns ECONNREFUSED. Regular files,
    symlinks, directories, other users' files and any socket we did not
    record are refused, never deleted."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise AdminSocketError(f"MCP_ADMIN_SOCKET is a symlink, refusing to touch it: {path}")
    if not stat.S_ISSOCK(st.st_mode):
        raise AdminSocketError(f"MCP_ADMIN_SOCKET exists and is not a socket, refusing to delete it: {path}")
    if st.st_uid != os.geteuid():
        raise AdminSocketError(f"MCP_ADMIN_SOCKET is owned by another user: {path}")
    if _read_record(lock_fd) != _identity(st):
        raise AdminSocketError(
            f"{path} is a socket this connector did not create; refusing to remove it. "
            "If nothing uses it, delete it by hand.")
    path.unlink()


class AdminSocket:
    """The bound admin socket plus the lock that proves it is ours. Keep the
    object alive for the life of the server: closing it releases the lock."""

    def __init__(self, sock, identity: tuple[int, int, int], lock_fd: int):
        self.sock, self.identity, self.lock_fd = sock, identity, lock_fd

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            if self.lock_fd >= 0:
                os.close(self.lock_fd)
                self.lock_fd = -1


def bind_admin_socket(path: Path) -> AdminSocket:
    """Bind the local admin socket safely. The returned identity lets
    shutdown remove only the file this process created; it includes the
    nanosecond ctime because filesystems reuse inode numbers at once."""
    if len(str(path).encode()) > 100:  # macOS sun_path limit is 104 bytes
        raise AdminSocketError(f"MCP_ADMIN_SOCKET path too long for a Unix socket: {path}")
    _prepare_admin_dir(path.parent)
    lock_fd = _open_lock(path.with_name(path.name + ".lock"))
    try:
        _clear_stale_socket(path, lock_fd)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # socket file created 0600
        try:
            sock.bind(str(path))  # fails (EADDRINUSE) if anything appeared at path meanwhile
        except OSError as e:
            sock.close()
            raise AdminSocketError(f"cannot bind {path}: {e}")
        finally:
            os.umask(old_umask)
        identity = _identity(os.lstat(path))
        _write_record(lock_fd, identity)
        return AdminSocket(sock, identity, lock_fd)
    except BaseException:
        os.close(lock_fd)
        raise


def _identity(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_dev, st.st_ino, st.st_ctime_ns)


def remove_admin_socket(path: Optional[Path], identity: Optional[tuple[int, int, int]]) -> None:
    """Remove the socket on shutdown, but only if it is still the very file
    this process bound. Anything put there since is left alone."""
    if not path or not identity:
        return
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(st.st_mode) and _identity(st) == identity:
        path.unlink()


class UnixAdminListener:
    """admin_channel listener backed by an AdminSocket."""

    kind = "unix"

    def __init__(self, path: Path):
        self.path = path
        self._admin = bind_admin_socket(path)  # holds the lock until close()
        self.sock = self._admin.sock

    def describe(self) -> str:
        return str(self.path)

    def matches(self, scope) -> bool:
        # uvicorn reports a Unix-socket request as server == (path, None);
        # TCP requests carry (host, port). The exact path is unambiguous.
        server = scope.get("server") or ()
        return len(server) == 2 and server[1] is None and server[0] == str(self.path)

    def cleanup(self) -> None:
        remove_admin_socket(self.path, self._admin.identity)
        self._admin.close()


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 5):
        super().__init__("localhost", timeout=timeout)  # Host: localhost passes the Host check
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)
