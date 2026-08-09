"""
Companion setup QR payload encode/decode.

Payload scheme (v1):
  ma-companion://v1?url=...&user=...&pass=...&fp=...
Query values are URL-encoded. Omit empty optional fields (pass may be absent).
Also accepts compact JSON: {"v":1,"url":"...","user":"...","pass":"...","fp":"..."}.
"""
from __future__ import annotations

import json
import socket
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

SCHEME = "ma-companion"
PAYLOAD_VERSION = 1


def _is_private_ipv4(ip: str) -> bool:
    try:
        parts = [int(x) for x in (ip or "").split(".")]
        if len(parts) != 4:
            return False
        a, b = parts[0], parts[1]
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
    except Exception:
        return False


def list_lan_ips() -> list[str]:
    """
    Candidate non-loopback IPv4s for companion QR / phone URLs.
    Private ranges first; skips 127.x and link-local 169.254.x.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(ip: str):
        ip = (ip or "").strip()
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip in seen:
            return
        seen.add(ip)
        found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            _add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            _add(info[4][0])
    except Exception:
        pass

    found.sort(key=lambda ip: (0 if _is_private_ipv4(ip) else 1, ip))
    return found


def detect_lan_ip() -> str:
    """Best-effort LAN IPv4 for companion URLs when monitor binds 0.0.0.0."""
    ips = list_lan_ips()
    if ips:
        return ips[0]
    return "127.0.0.1"


def companion_base_url(host: str, port: int, use_https: bool, lan_ip: str | None = None) -> str:
    """
    Reachable base URL for the companion.
    When bind is 0.0.0.0 / all-interfaces, substitute the LAN IP (not 127.0.0.1).
    Optional lan_ip overrides detection when host is all-interfaces.
    """
    h = (host or "127.0.0.1").strip()
    if h in ("0.0.0.0", "::", "*"):
        override = (lan_ip or "").strip()
        if override and not override.startswith("127.") and not override.startswith("::1"):
            h = override
        else:
            h = detect_lan_ip()
    elif h in ("127.0.0.1", "localhost", "::1"):
        h = "127.0.0.1"
    scheme = "https" if use_https else "http"
    return f"{scheme}://{h}:{int(port)}/"


def encode_setup_payload(
    url: str,
    username: str = "",
    password: str = "",
    fingerprint: str = "",
    *,
    as_json: bool = False,
) -> str:
    """Build a versioned companion setup string (URI by default)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    user = (username or "").strip()
    pwd = password or ""
    fp = (fingerprint or "").strip()

    if as_json:
        data: dict[str, Any] = {"v": PAYLOAD_VERSION, "url": url}
        if user:
            data["user"] = user
        if pwd:
            data["pass"] = pwd
        if fp:
            data["fp"] = fp
        return json.dumps(data, separators=(",", ":"))

    params: list[tuple[str, str]] = [("url", url)]
    if user:
        params.append(("user", user))
    if pwd:
        params.append(("pass", pwd))
    if fp:
        params.append(("fp", fp))
    # quote_via=quote keeps : / in url value readable but still safe
    qs = urlencode(params, quote_via=quote)
    return f"{SCHEME}://v{PAYLOAD_VERSION}?{qs}"


def decode_setup_payload(raw: str) -> dict[str, str]:
    """
    Parse a companion setup QR payload.
    Returns dict with keys: url, user, pass, fp (missing keys -> "").
    Raises ValueError on invalid / unsupported payloads.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty payload")

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON payload: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")
        ver = int(data.get("v") or 0)
        if ver != PAYLOAD_VERSION:
            raise ValueError(f"unsupported payload version: {ver}")
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError("missing url")
        return {
            "url": url,
            "user": str(data.get("user") or "").strip(),
            "pass": str(data.get("pass") or ""),
            "fp": str(data.get("fp") or "").strip(),
        }

    parsed = urlparse(text)
    if parsed.scheme != SCHEME:
        raise ValueError(f"unsupported scheme: {parsed.scheme or '(none)'}")
    # netloc is "v1", path may be empty; some parsers put v1 in path
    ver_token = (parsed.netloc or parsed.path.lstrip("/").split("/")[0] or "").strip()
    if not ver_token.startswith("v"):
        raise ValueError(f"missing version in URI: {ver_token!r}")
    try:
        ver = int(ver_token[1:])
    except ValueError as e:
        raise ValueError(f"invalid version: {ver_token}") from e
    if ver != PAYLOAD_VERSION:
        raise ValueError(f"unsupported payload version: {ver}")

    qs = parse_qs(parsed.query, keep_blank_values=True)
    def _one(key: str, default: str = "") -> str:
        vals = qs.get(key)
        if not vals:
            return default
        return unquote(vals[0])

    url = _one("url").strip()
    if not url:
        raise ValueError("missing url")
    return {
        "url": url,
        "user": _one("user").strip(),
        "pass": _one("pass"),
        "fp": _one("fp").strip(),
    }


def qr_png_bytes(payload: str, box_size: int = 6, border: int = 2) -> bytes:
    """Render payload as a PNG QR code (requires qrcode + pillow)."""
    import io

    try:
        import qrcode
    except ImportError as e:
        raise ImportError(
            "QR generation needs the 'qrcode' package. "
            "Install with: py -3.12 -m pip install \"qrcode[pil]\""
        ) from e
    try:
        from PIL import Image  # noqa: F401 — required by qrcode PNG output
    except ImportError as e:
        raise ImportError(
            "QR generation needs Pillow. "
            "Install with: py -3.12 -m pip install pillow"
        ) from e

    img = qrcode.make(payload, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
