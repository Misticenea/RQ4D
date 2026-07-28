"""Self-signed TLS, because WebXR refuses to exist without a secure context.

`navigator.xr` is gated on a secure context. Over plain HTTP to a LAN address
it is simply `undefined`, so the capture client reports "WebXR unavailable"
and there is nothing in the page to explain why. Secure contexts are `https`,
`localhost`, and `127.0.0.1` — a LAN IP over http is none of them.

Two ways out, and the cheap one is worth trying first:

**USB.** `adb reverse tcp:8787 tcp:8787`, then open `http://localhost:8787/`
in the headset. localhost *is* a secure context, so WebXR works with no
certificate at all. This is also the link that removes Wi-Fi variance from
every measurement, so it is the right default for bring-up anyway.

**TLS.** For wireless use there is no way around a certificate. This generates
a self-signed one covering the host's LAN address. The browser will warn once
and the user accepts it; that is the normal cost of local WebXR development,
not a sign anything is wrong.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
import subprocess
from pathlib import Path

CERT_DIR = Path.home() / ".cache" / "rq4d"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
VALID_DAYS = 825  # the longest most browsers accept for a leaf certificate


def ensure_certificate(address: str) -> tuple[Path, Path]:
    """A self-signed cert valid for `address`, generated once and reused.

    Shells out to openssl rather than taking a Python crypto dependency: the
    host otherwise needs only numpy, scikit-image and websockets, and openssl
    is present anywhere this will run.

    Regenerated when the LAN address changes. A certificate that does not cover
    the address being dialled produces a different and far more confusing
    browser error than the expected self-signed warning.
    """
    if CERT_FILE.exists() and KEY_FILE.exists() and _covers(CERT_FILE, address):
        return CERT_FILE, KEY_FILE

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        ipaddress.ip_address(address)
        entry = f"IP:{address}"
    except ValueError:
        entry = f"DNS:{address}"

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
            "-days", str(VALID_DAYS), "-nodes",
            "-subj", "/CN=RQ4D host",
            "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,{entry}",
        ],
        check=True,
        capture_output=True,
    )
    KEY_FILE.chmod(0o600)
    return CERT_FILE, KEY_FILE


def _covers(cert_file: Path, address: str) -> bool:
    try:
        text = subprocess.run(
            ["openssl", "x509", "-in", str(cert_file), "-noout", "-text", "-checkend", "0"],
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False  # expired, or unreadable
    san = re.search(r"Subject Alternative Name:\s*\n\s*(.+)", text)
    if not san:
        return False
    entries = {e.split(":", 1)[-1].strip() for e in san.group(1).split(",")}
    return address in entries


def server_context(address: str) -> ssl.SSLContext:
    cert_file, key_file = ensure_certificate(address)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


def fingerprint(address: str) -> str:
    """SHA-256 prefix, so the warning the browser shows can be checked."""
    cert_file, _ = ensure_certificate(address)
    out = subprocess.run(
        ["openssl", "x509", "-in", str(cert_file), "-noout", "-fingerprint", "-sha256"],
        check=True, capture_output=True, text=True,
    ).stdout
    digest = out.split("=", 1)[-1].strip()
    return ":".join(digest.split(":")[:8]) + "…"
