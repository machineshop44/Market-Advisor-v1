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

    def test_existing_cert_keeps_pin_when_lan_ip_changes(self):
        import monitor_tls as mt

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch.object(mt, "_DIR", td_path), \
                 patch.object(mt, "CERT_FILE", td_path / "cert.pem"), \
                 patch.object(mt, "KEY_FILE", td_path / "key.pem"), \
                 patch.object(mt, "FINGERPRINT_FILE", td_path / "fingerprint.txt"), \
                 patch.object(mt, "_discover_lan_ips", return_value=[]):
                _, _, fp1 = mt.ensure_tls_material(refresh_lan_sans=True)

            with patch.object(mt, "_DIR", td_path), \
                 patch.object(mt, "CERT_FILE", td_path / "cert.pem"), \
                 patch.object(mt, "KEY_FILE", td_path / "key.pem"), \
                 patch.object(mt, "FINGERPRINT_FILE", td_path / "fingerprint.txt"), \
                 patch.object(mt, "_discover_lan_ips", return_value=["10.0.0.9"]):
                _, _, fp2 = mt.ensure_tls_material(refresh_lan_sans=True)
                # Keep the pin so companion does not go "offline" after a DHCP change
                self.assertEqual(fp1, fp2)
                _, _, fp3 = mt.ensure_tls_material(force_rotate=True)
                self.assertNotEqual(fp1, fp3)
                self.assertIn("10.0.0.9", mt.cert_san_ips())


if __name__ == "__main__":
    unittest.main()
