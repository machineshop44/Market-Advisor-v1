"""
Self-signed TLS material for the Market Advisor web monitor.

User/pass over the wire are protected by HTTPS — Basic Auth alone is NOT encryption.
Certs live under Src/monitor_tls/ (gitignored).

SANs include localhost + private LAN IPs so phone companion can connect without
hostname mismatch (fingerprint pinning still applies on the Android side).
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "monitor_tls"
CERT_FILE = _DIR / "cert.pem"
KEY_FILE = _DIR / "key.pem"
FINGERPRINT_FILE = _DIR / "fingerprint.txt"


def tls_dir() -> Path:
    return _DIR


def cert_paths():
    return CERT_FILE, KEY_FILE


def read_fingerprint() -> str:
    if FINGERPRINT_FILE.is_file():
        try:
            return FINGERPRINT_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if CERT_FILE.is_file():
        try:
            return fingerprint_from_pem(CERT_FILE.read_bytes())
        except Exception:
            pass
    return ""


def fingerprint_from_pem(pem_bytes: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    cert = x509.load_pem_x509_certificate(pem_bytes, default_backend())
    der = cert.public_bytes(serialization_encoding_der())
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def serialization_encoding_der():
    from cryptography.hazmat.primitives.serialization import Encoding

    return Encoding.DER


def _discover_lan_ips() -> list[str]:
    """Best-effort private LAN IPv4s for cert SANs (never fails hard)."""
    try:
        from companion_qr import list_lan_ips
        return [ip for ip in list_lan_ips() if ip and not ip.startswith("127.")]
    except Exception:
        return []


def cert_san_ips(pem_bytes: bytes | None = None) -> set[str]:
    """IPv4/IPv6 strings present in the cert SubjectAlternativeName extension."""
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    raw = pem_bytes
    if raw is None:
        if not CERT_FILE.is_file():
            return set()
        raw = CERT_FILE.read_bytes()
    cert = x509.load_pem_x509_certificate(raw, default_backend())
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except Exception:
        return set()
    out: set[str] = set()
    for name in ext.value:
        try:
            if isinstance(name, x509.IPAddress):
                out.add(str(name.value))
        except Exception:
            continue
    return out


def _build_san_list(common_name: str, extra_ips: list[str] | None = None):
    from cryptography import x509

    names = [
        x509.DNSName("localhost"),
        x509.DNSName(common_name),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    seen = {"127.0.0.1"}
    for ip in list(extra_ips or []) + _discover_lan_ips():
        ip = (ip or "").strip()
        if not ip or ip in seen or ip.startswith("127.") or ip.startswith("169.254."):
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
            seen.add(ip)
        except Exception:
            continue
    return names


def _write_cert_pair(common_name: str, extra_ips: list[str] | None = None) -> str:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Market Advisor"),
    ])
    now = datetime.now(timezone.utc)
    san = _build_san_list(common_name, extra_ips)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256(), default_backend())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    fp = fingerprint_from_pem(CERT_FILE.read_bytes())
    FINGERPRINT_FILE.write_text(fp + "\n", encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    return fp


def ensure_tls_material(
    common_name: str = "MarketAdvisor",
    *,
    extra_ips: list[str] | None = None,
    refresh_lan_sans: bool = True,
) -> tuple[Path, Path, str]:
    """
    Ensure cert.pem + key.pem exist. Returns (cert, key, sha256_fingerprint).

    When refresh_lan_sans is True and an existing cert is missing the current
    private LAN IP(s), regenerate so phone HTTPS to https://<lan-ip>:port/ works.
    Regenerating changes the fingerprint — companion QR / pin must be re-scanned
    (same flow as first setup; pinning is not bypassed).
    """
    _DIR.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for ip in list(extra_ips or []) + (_discover_lan_ips() if refresh_lan_sans else []):
        ip = (ip or "").strip()
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            wanted.add(ip)

    if CERT_FILE.is_file() and KEY_FILE.is_file():
        try:
            pem = CERT_FILE.read_bytes()
            fp = read_fingerprint() or fingerprint_from_pem(pem)
            if not fp:
                fp = fingerprint_from_pem(pem)
                FINGERPRINT_FILE.write_text(fp + "\n", encoding="utf-8")
            have = cert_san_ips(pem)
            missing = wanted - have if refresh_lan_sans else set()
            # Always require loopback SAN on healthy certs
            if "127.0.0.1" not in have:
                missing.add("127.0.0.1")
            if not missing:
                return CERT_FILE, KEY_FILE, fp
            # Stale localhost-only cert → regenerate with LAN SANs
            fp = _write_cert_pair(common_name, extra_ips=list(wanted) or list(extra_ips or []))
            return CERT_FILE, KEY_FILE, fp
        except Exception:
            pass

    fp = _write_cert_pair(common_name, extra_ips=list(wanted) or list(extra_ips or []))
    return CERT_FILE, KEY_FILE, fp
