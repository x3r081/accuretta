"""PKCE unit tests.

Run: python3 -m unittest tests.test_oauth_pkce -v
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.pkce import generate_code_challenge, generate_code_verifier, generate_state, pkce_pair


class PkceTest(unittest.TestCase):
    def test_verifier_length_bounds(self):
        v = generate_code_verifier(64)
        self.assertGreaterEqual(len(v), 43)
        self.assertLessEqual(len(v), 128)

    def test_challenge_is_s256(self):
        pair = pkce_pair()
        digest = hashlib.sha256(pair.verifier.encode("utf-8")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        self.assertEqual(pair.challenge, expected)
        self.assertEqual(pair.method, "S256")
        self.assertEqual(generate_code_challenge(pair.verifier), pair.challenge)

    def test_state_random(self):
        a = generate_state()
        b = generate_state()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 20)


if __name__ == "__main__":
    unittest.main()
