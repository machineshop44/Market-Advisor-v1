"""TLS SAN / LAN polish for companion monitor certs."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class TestMonitorTlsSans(unittest.TestCase):
    def test_new_cert_includes_lan_and_loopback(self):
        import monitor_tls as mt

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch.object(mt, "_DIR", td_path), \
                 patch.object(mt, "CERT_FILE", td_path / "cert.pem"), \
                 patch.object(mt, "KEY_FILE", td_path / "key.pem"), \
                 patch.object(mt, "FINGERPRINT_FILE", td_path / "fingerprint.txt"), \
                 patch.object(mt, "_discover_lan_ips", return_value=["192.168.1.50"]):
                cert, key, fp = mt.ensure_tls_material(refresh_lan_sans=True)
                self.assertTrue(cert.is_file())
                self.assertTrue(key.is_file())
                self.assertTrue(fp)
                sans = mt.cert_san_ips()
                self.assertIn("127.0.0.1", sans)
                self.assertIn("192.168.1.50", sans)

    def test_localhost_only_cert_refreshes_when_lan_appears(self):
        import monitor_tls as mt

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch.object(mt, "_DIR", td_path), \
                 patch.object(mt, "CERT_FILE", td_path / "cert.pem"), \
                 patch.object(mt, "KEY_FILE", td_path / "key.pem"), \
                 patch.object(mt, "FINGERPRINT_FILE", td_path / "fingerprint.txt"), \
                 patch.object(mt, "_discover_lan_ips", return_value=[]):
                _, _, fp1 = mt.ensure_tls_material(refresh_lan_sans=True)
                sans1 = mt.cert_san_ips()
                self.assertIn("127.0.0.1", sans1)
                self.assertNotIn("10.0.0.9", sans1)

            with patch.object(mt, "_DIR", td_path), \
                 patch.object(mt, "CERT_FILE", td_path / "cert.pem"), \
                 patch.object(mt, "KEY_FILE", td_path / "key.pem"), \
                 patch.object(mt, "FINGERPRINT_FILE", td_path / "fingerprint.txt"), \
                 patch.object(mt, "_discover_lan_ips", return_value=["10.0.0.9"]):
                _, _, fp2 = mt.ensure_tls_material(refresh_lan_sans=True)
                sans2 = mt.cert_san_ips()
                self.assertIn("10.0.0.9", sans2)
                # Fingerprint changes on regen — companion must re-pin via QR (expected)
                self.assertNotEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
