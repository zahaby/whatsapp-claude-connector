"""
Loopback TLS, so the connector can prove who it is to its local clients.

Why: the Cloudflare tunnel forwards the iPhone's requests, which carry the
connector token in the URL, to 127.0.0.1:MCP_PORT. If the connector were
stopped and another local program took that port, plain HTTP would hand it
the token. With TLS, cloudflared verifies the origin against THIS
certificate (config/cloudflared.yml: caPool + originServerName), so a
listener without our private key fails the handshake before any request,
and the token, is sent. verify.py pins the same certificate.

The certificate is self-signed (it is its own trust anchor), for
wa-connector.local and 127.0.0.1, valid 10 years. The private key is
readable only by its owner: created 0600 on macOS/Linux; on Windows it
lives under %LOCALAPPDATA% (private to the user) and install.ps1 restricts
its ACL to the user alone.

  python tls.py ensure     create if missing, validate, print the cert path
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "wa-connector.local"
HERE = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
VALID_DAYS = 3650


class TlsError(RuntimeError):
    """TLS material is missing, mismatched or not private; refuse to start."""


@dataclass
class TlsFiles:
    cert: Path
    key: Path


def default_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "wa-connector" / "tls"
    return HERE / ".tls"


def files(directory: Path) -> TlsFiles:
    return TlsFiles(cert=directory / "connector.crt", key=directory / "connector.key")


def _prepare_dir(directory: Path) -> None:
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        return
    if created:
        os.chmod(directory, 0o700)
    st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise TlsError(f"TLS directory is not a real directory: {directory}")
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise TlsError(f"TLS directory must be owned by you and not group/world-writable: {directory}")


def _check_key_private(path: Path) -> None:
    if IS_WINDOWS:
        return  # ACL set by windows\install.ps1 (Protect-File); profile dir is private by default
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise TlsError(f"TLS key is not a regular file: {path}")
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise TlsError(f"TLS key must be owner-only (chmod 600): {path}")


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _generate(f: TlsFiles) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SERVER_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName(SERVER_NAME), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        # Self-signed and used as its own trust anchor by cloudflared and verify.py.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False, key_agreement=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(f.key, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    f.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _validate(f: TlsFiles) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    _check_key_private(f.key)
    try:
        cert = x509.load_pem_x509_certificate(f.cert.read_bytes())
        key = serialization.load_pem_private_key(f.key.read_bytes(), password=None)
    except (ValueError, TypeError) as e:
        raise TlsError(f"cannot read TLS certificate/key in {f.cert.parent}: {e}")
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise TlsError(f"TLS key does not match the certificate in {f.cert.parent}")
    try:
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise TlsError("TLS certificate has no subjectAltName")
    if SERVER_NAME not in sans.get_values_for_type(x509.DNSName):
        raise TlsError(f"TLS certificate is not for {SERVER_NAME}")
    if cert.not_valid_after_utc <= datetime.datetime.now(datetime.timezone.utc):
        raise TlsError(f"TLS certificate expired; delete {f.cert} and {f.key}, restart, re-run setup-tunnel")


def ensure(directory: Path) -> TlsFiles:
    """Create the certificate and key if both are missing; otherwise
    validate them. Never overwrites existing material."""
    _prepare_dir(directory)
    f = files(directory)
    if not f.cert.exists() and not f.key.exists():
        _generate(f)
    elif not (f.cert.exists() and f.key.exists()):
        raise TlsError(f"only one of {f.cert.name} / {f.key.name} exists in {directory}; refusing to guess")
    _validate(f)
    return f


def client_context(cert: Path) -> ssl.SSLContext:
    """Trust exactly this certificate, nothing else, and check the name."""
    if not cert.exists():
        raise OSError(f"no connector TLS certificate at {cert}; start the connector once")
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cert))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


if __name__ == "__main__":
    if sys.argv[1:] != ["ensure"]:
        sys.exit("usage: tls.py ensure")
    d = Path(os.environ.get("MCP_TLS_DIR") or default_dir())
    try:
        print(ensure(d).cert)
    except TlsError as e:
        sys.exit(f"ERROR: {e}")
