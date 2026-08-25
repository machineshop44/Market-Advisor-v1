"""RH/CB keyring helpers — migrate + scrub settings."""
import os
import sys
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import credentials as cred  # noqa: E402


class TestCredentials(unittest.TestCase):
    def test_scrub_when_keyring_has_secret(self):
        with mock.patch.object(cred, "load_rh_password", return_value="secret"):
            with mock.patch.object(cred, "load_cb_api_secret", return_value="cbsec"):
                out = cred.scrub_settings_for_disk({
                    "rh_email": "a@b.com",
                    "rh_password": "legacy",
                    "cb_api_secret": "legacy2",
                    "dark_mode": True,
                })
        self.assertNotIn("rh_password", out)
        self.assertNotIn("cb_api_secret", out)
        self.assertTrue(out.get("dark_mode"))

    def test_migrate_moves_plaintext_to_keyring(self):
        settings = {"rh_password": "pw123", "cb_api_secret": "sec456"}
        with mock.patch.object(cred, "store_rh_password", return_value=True):
            with mock.patch.object(cred, "store_cb_api_secret", return_value=True):
                dirty = cred.migrate_settings_secrets(settings)
        self.assertTrue(dirty)
        self.assertEqual(settings["rh_password"], "")
        self.assertEqual(settings["cb_api_secret"], "")


if __name__ == "__main__":
    unittest.main()
