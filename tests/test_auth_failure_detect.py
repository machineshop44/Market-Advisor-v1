"""Auth-failure detection for E*TRADE 401 / reauth Discord quiet path."""
import os
import sys
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class TestManualAuthFailureDetect(unittest.TestCase):
    def test_etrade_401_balance_error(self):
        from gui import _is_manual_auth_failure
        msg = "E*TRADE GET /v1/accounts/abc/balance failed (401): <Error>"
        self.assertTrue(_is_manual_auth_failure(msg))

    def test_reauthorization_required(self):
        from gui import _is_manual_auth_failure
        self.assertTrue(_is_manual_auth_failure("Reauthorization required (token inactive)"))

    def test_unauthorized_wording(self):
        from gui import _is_manual_auth_failure
        self.assertTrue(_is_manual_auth_failure("HTTP Status 401 - Unauthorized"))

    def test_non_auth_errors_false(self):
        from gui import _is_manual_auth_failure
        self.assertFalse(_is_manual_auth_failure("E*TRADE GET /v1/accounts/x/balance failed (500): timeout"))
        self.assertFalse(_is_manual_auth_failure("rate limit 429"))
        self.assertFalse(_is_manual_auth_failure("connection reset"))

    def test_version_note_covers_ship(self):
        from version import __version__, VERSION_NOTE
        parts = [int(x) for x in str(__version__).split(".")[:3]]
        self.assertGreaterEqual(parts[0], 1)
        self.assertGreaterEqual(parts[1], 30)
        note = VERSION_NOTE.lower()
        self.assertTrue(
            any(
                k in note
                for k in (
                    "arm", "midnight", "reauth", "fee", "crypto", "etrade", "et ",
                )
            ),
            f"VERSION_NOTE should describe ship themes: {VERSION_NOTE!r}",
        )


if __name__ == "__main__":
    unittest.main()
