"""1.42.17 — monitor auth lockout must ignore bare 401 challenges."""
import os
import sys
import unittest
from unittest.mock import MagicMock

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestMonitorAuthLockout(unittest.TestCase):
    def setUp(self):
        import monitor
        self.m = monitor
        monitor.clear_auth_lockouts()
        monitor._auth_user = "desk"
        monitor._auth_pass = "secret"
        monitor._auth_required = True

    def tearDown(self):
        self.m.clear_auth_lockouts()
        self.m._auth_user = ""
        self.m._auth_pass = ""
        self.m._auth_required = False

    def _handler(self, *, authorization=None, ip="203.0.113.10"):
        h = MagicMock()
        h.client_address = (ip, 40000)
        headers = {}
        if authorization is not None:
            headers["Authorization"] = authorization
        h.headers.get.side_effect = lambda k, default="": headers.get(k, default)
        return h

    def test_missing_auth_does_not_lock(self):
        import base64
        h = self._handler()  # no Authorization
        for _ in range(10):
            self.assertFalse(self.m._check_auth(h))
        self.assertFalse(self.m._auth_is_locked("203.0.113.10"))
        self.assertEqual(self.m.auth_lockout_snapshot(), [])

    def test_wrong_password_locks_after_max(self):
        import base64
        bad = "Basic " + base64.b64encode(b"desk:wrong").decode()
        h = self._handler(authorization=bad)
        for _ in range(self.m._AUTH_MAX_FAILS):
            self.assertFalse(self.m._check_auth(h))
        self.assertTrue(self.m._auth_is_locked("203.0.113.10"))
        snap = self.m.auth_lockout_snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["ip"], "203.0.113.10")

    def test_good_password_clears(self):
        import base64
        bad = "Basic " + base64.b64encode(b"desk:wrong").decode()
        good = "Basic " + base64.b64encode(b"desk:secret").decode()
        h_bad = self._handler(authorization=bad)
        for _ in range(3):
            self.m._check_auth(h_bad)
        h_ok = self._handler(authorization=good)
        self.assertTrue(self.m._check_auth(h_ok))
        self.assertFalse(self.m._auth_is_locked("203.0.113.10"))


if __name__ == "__main__":
    unittest.main()
