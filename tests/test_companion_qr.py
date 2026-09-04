"""Unit tests for companion setup QR payload encode/decode."""
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import companion_qr as cq  # noqa: E402


def test_encode_decode_uri_roundtrip():
    raw = cq.encode_setup_payload(
        "https://192.168.1.10:8791/",
        username="phone",
        password="s3cret!",
        fingerprint="AA:BB:CC:DD",
    )
    assert raw.startswith("ma-companion://v1?")
    parsed = urlparse(raw)
    assert parsed.scheme == "ma-companion"
    qs = parse_qs(parsed.query)
    assert qs["url"][0] == "https://192.168.1.10:8791/"
    assert qs["user"][0] == "phone"
    assert qs["pass"][0] == "s3cret!"
    assert qs["fp"][0] == "AA:BB:CC:DD"

    got = cq.decode_setup_payload(raw)
    assert got["url"] == "https://192.168.1.10:8791/"
    assert got["user"] == "phone"
    assert got["pass"] == "s3cret!"
    assert got["fp"] == "AA:BB:CC:DD"


def test_encode_omits_empty_optional_fields():
    raw = cq.encode_setup_payload("https://10.0.0.2:8791/", username="u")
    assert "pass=" not in raw
    assert "fp=" not in raw
    got = cq.decode_setup_payload(raw)
    assert got["user"] == "u"
    assert got["pass"] == ""
    assert got["fp"] == ""


def test_json_roundtrip():
    raw = cq.encode_setup_payload(
        "https://example.test:8791/",
        username="a",
        password="",
        fingerprint="11:22",
        as_json=True,
    )
    assert raw.startswith("{")
    assert "pass" not in raw  # empty password omitted
    got = cq.decode_setup_payload(raw)
    assert got["url"] == "https://example.test:8791/"
    assert got["user"] == "a"
    assert got["fp"] == "11:22"


def test_decode_rejects_bad_payloads():
    for bad in ("", "http://nope", "ma-companion://v99?url=x", '{"v":1}', "not-json"):
        try:
            cq.decode_setup_payload(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_companion_base_url_lan_bind():
    url = cq.companion_base_url("0.0.0.0", 8791, True)
    assert url.startswith("https://")
    assert url.endswith(":8791/")
    assert "0.0.0.0" not in url
    assert "127.0.0.1" not in url or cq.detect_lan_ip() == "127.0.0.1"


def test_companion_base_url_localhost():
    assert cq.companion_base_url("127.0.0.1", 8791, False) == "http://127.0.0.1:8791/"


def test_list_lan_ips_shape():
    ips = cq.list_lan_ips()
    assert isinstance(ips, list)
    for ip in ips:
        assert not ip.startswith("127.")
        assert not ip.startswith("169.254.")
        parts = ip.split(".")
        assert len(parts) == 4


def test_companion_base_url_prefers_public_when_bound_all():
    url = cq.companion_base_url(
        "0.0.0.0", 8791, True,
        public_host="67.84.101.14",
        prefer_public=True,
    )
    assert url == "https://67.84.101.14:8791/"


def test_companion_base_url_lan_ip_override():
    url = cq.companion_base_url("0.0.0.0", 8791, True, lan_ip="10.0.0.42")
    assert url == "https://10.0.0.42:8791/"


def test_companion_base_url_lan_override_wins_when_selected():
    url = cq.companion_base_url(
        "0.0.0.0", 8791, True,
        lan_ip="192.168.1.20",
        public_host="67.84.101.14",
        prefer_public=False,
    )
    assert url == "https://192.168.1.20:8791/"


def test_normalize_reachable_host():
    assert cq.normalize_reachable_host("https://67.84.101.14:8791/") == "67.84.101.14"
    assert cq.normalize_reachable_host("67.84.101.14") == "67.84.101.14"
    assert cq.normalize_reachable_host("127.0.0.1") == ""


if __name__ == "__main__":
    test_encode_decode_uri_roundtrip()
    test_encode_omits_empty_optional_fields()
    test_json_roundtrip()
    test_decode_rejects_bad_payloads()
    test_companion_base_url_lan_bind()
    test_companion_base_url_localhost()
    test_list_lan_ips_shape()
    test_companion_base_url_lan_ip_override()
    test_companion_base_url_prefers_public_when_bound_all()
    test_companion_base_url_lan_override_wins_when_selected()
    test_normalize_reachable_host()
    print("ok")
