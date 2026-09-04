"""RH/CB/Advisor/App keyring helpers — migrate + scrub settings."""
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
                with mock.patch.object(cred, "load_advisor_api_key", return_value="aik"):
                    with mock.patch.object(cred, "load_monitor_pass", return_value="mp"):
                        with mock.patch.object(cred, "load_discord_webhook", return_value="https://wh"):
                            with mock.patch.object(cred, "load_cursor_monitor_token", return_value="ctok"):
                                with mock.patch.object(cred, "load_cb_api_key", return_value="cbk"):
                                    with mock.patch.object(cred, "load_etrade_consumer_key", return_value="etk"):
                                        out = cred.scrub_settings_for_disk({
                                            "rh_email": "a@b.com",
                                            "rh_password": "legacy",
                                            "cb_api_secret": "legacy2",
                                            "advisor_ai_api_key": "legacy3",
                                            "monitor_pass": "legacym",
                                            "discord_webhook": "legacywh",
                                            "cursor_monitor_token": "legacytok",
                                            "cb_api_key": "legacycb",
                                            "etrade_consumer_key": "legacyet",
                                            "dark_mode": True,
                                        })
        self.assertNotIn("rh_password", out)
        self.assertNotIn("cb_api_secret", out)
        self.assertNotIn("advisor_ai_api_key", out)
        self.assertNotIn("monitor_pass", out)
        self.assertNotIn("discord_webhook", out)
        self.assertNotIn("cursor_monitor_token", out)
        self.assertNotIn("cb_api_key", out)
        self.assertNotIn("etrade_consumer_key", out)
        self.assertTrue(out.get("dark_mode"))

    def test_migrate_moves_plaintext_to_keyring(self):
        settings = {
            "rh_password": "pw123",
            "cb_api_secret": "sec456",
            "advisor_ai_api_key": "ai789",
            "monitor_pass": "mon",
            "discord_webhook": "https://discord.gg/x",
            "cb_api_key": "cbk",
            "etrade_consumer_key": "etk",
        }
        with mock.patch.object(cred, "store_rh_password", return_value=True):
            with mock.patch.object(cred, "store_cb_api_secret", return_value=True):
                with mock.patch.object(cred, "store_advisor_api_key", return_value=True):
                    with mock.patch.object(cred, "store_monitor_pass", return_value=True):
                        with mock.patch.object(cred, "store_discord_webhook", return_value=True):
                            with mock.patch.object(cred, "store_cb_api_key", return_value=True):
                                with mock.patch.object(cred, "store_etrade_consumer_key", return_value=True):
                                    dirty = cred.migrate_settings_secrets(settings)
        self.assertTrue(dirty)
        self.assertEqual(settings["rh_password"], "")
        self.assertEqual(settings["cb_api_secret"], "")
        self.assertEqual(settings["advisor_ai_api_key"], "")

    def test_hydrate_restores_memory(self):
        settings = {}
        with mock.patch.object(cred, "load_advisor_api_key", return_value="aik"):
            with mock.patch.object(cred, "load_monitor_pass", return_value="mp"):
                with mock.patch.object(cred, "load_discord_webhook", return_value="wh"):
                    with mock.patch.object(cred, "load_cb_api_key", return_value="cbk"):
                        with mock.patch.object(cred, "load_etrade_consumer_key", return_value="etk"):
                            cred.hydrate_settings_secrets(settings)
        self.assertEqual(settings["advisor_ai_api_key"], "aik")
        self.assertEqual(settings["monitor_pass"], "mp")
        self.assertEqual(settings["discord_webhook"], "wh")
        self.assertEqual(settings["cb_api_key"], "cbk")
        self.assertEqual(settings["etrade_consumer_key"], "etk")

    def test_resolve_prefers_keyring(self):
        with mock.patch.object(cred, "load_rh_password", return_value="from-ring"):
            self.assertEqual(
                cred.resolve_rh_password({"rh_password": "legacy"}),
                "from-ring",
            )
        with mock.patch.object(cred, "load_rh_password", return_value=""):
            self.assertEqual(
                cred.resolve_rh_password({"rh_password": "legacy"}),
                "legacy",
            )
        with mock.patch.object(cred, "load_advisor_api_key", return_value="ring-ai"):
            self.assertEqual(
                cred.resolve_advisor_api_key({"advisor_ai_api_key": "legacy"}),
                "ring-ai",
            )

    def test_persist_clears_settings(self):
        settings = {
            "rh_password": "x",
            "cb_api_secret": "y",
            "advisor_ai_api_key": "z",
        }
        with mock.patch.object(cred, "store_rh_password", return_value=True):
            self.assertTrue(cred.persist_rh_password("newpw", settings))
        self.assertEqual(settings["rh_password"], "")
        with mock.patch.object(cred, "store_cb_api_secret", return_value=True):
            self.assertTrue(cred.persist_cb_api_secret("newsec", settings))
        self.assertEqual(settings["cb_api_secret"], "")
        with mock.patch.object(cred, "store_advisor_api_key", return_value=True):
            self.assertTrue(cred.persist_advisor_api_key("newai", settings))
        self.assertEqual(settings["advisor_ai_api_key"], "newai")


if __name__ == "__main__":
    unittest.main()
