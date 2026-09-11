import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from djinni_http import (  # noqa: E402
    BlockedResponseError,
    is_blocked_response,
    raise_if_blocked,
)


class DjinniHttpTests(unittest.TestCase):
    def test_detects_block_markers(self):
        self.assertTrue(is_blocked_response("Sorry, you have been blocked by the firewall. Page has been blocked"))
        self.assertTrue(is_blocked_response("<html>cf-chl-bypass</html>"))
        self.assertFalse(is_blocked_response("<html>normal job page</html>"))

    def test_raise_if_blocked(self):
        raise_if_blocked("ok content")
        with self.assertRaises(BlockedResponseError):
            raise_if_blocked("cf-chl-token present")


if __name__ == "__main__":
    unittest.main()
