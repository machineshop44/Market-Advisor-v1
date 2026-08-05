"""Robinhood pickle restore must load tokens without interactive login."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestRobinhoodPickleRestore(unittest.TestCase):
    def test_login_empty_creds_loads_pickle_not_profile_only(self):
        """Regression: login({}) used to skip pickle and only call load_account_profile."""
        from broker import RobinhoodAdapter

        adapter = RobinhoodAdapter()
        with patch.object(adapter, "_restore_from_pickle", return_value=(True, "Session Verified")) as restore:
            with patch("broker.r") as mock_r:
                mock_r.login = MagicMock()
                mock_r.profiles.load_account_profile = MagicMock(
                    return_value={"account_number": "should-not-matter"}
                )
                ok, msg = adapter.login({})
        self.assertTrue(ok)
        self.assertEqual(msg, "Session Verified")
        restore.assert_called_once()
        mock_r.login.assert_not_called()

    def test_login_empty_creds_fails_when_pickle_expired(self):
        from broker import RobinhoodAdapter

        adapter = RobinhoodAdapter()
        with patch.object(
            adapter, "_restore_from_pickle", return_value=(False, "saved session expired")
        ):
            ok, msg = adapter.login({})
        self.assertFalse(ok)
        self.assertIn("expired", msg)
        self.assertFalse(adapter.is_connected)

    def test_restore_from_pickle_missing_file(self):
        from broker import RobinhoodAdapter

        adapter = RobinhoodAdapter()
        with patch("broker.robinhood_pickle_path", return_value=os.path.join(tempfile.gettempdir(), "nope.pickle")):
            ok, msg = adapter._restore_from_pickle()
        self.assertFalse(ok)
        self.assertEqual(msg, "no saved session")

    def test_password_login_still_calls_r_login(self):
        from broker import RobinhoodAdapter

        adapter = RobinhoodAdapter()
        with patch("broker.r") as mock_r:
            mock_r.login.return_value = {"access_token": "tok"}
            ok, msg = adapter.login(
                {"email": "a@b.com", "password": "secret", "store_session": True}
            )
        self.assertTrue(ok)
        self.assertEqual(msg, "Success")
        mock_r.login.assert_called_once_with(
            username="a@b.com", password="secret", store_session=True
        )


if __name__ == "__main__":
    unittest.main()
