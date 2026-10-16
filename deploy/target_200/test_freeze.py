#!/usr/bin/env python3
"""
deploy/target_200/test_freeze.py
================================
Integrity check for Target 200 Frozen Production Configuration.
"""
import os
import hashlib
import unittest

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DIR, "frozen_config.json")
EXPECTED_SHA256 = "9f5a266c94a8091b0e98cce10cfd67b7f9a55f2832f8b46a88922ce8cbcde330"

class TestTarget200Freeze(unittest.TestCase):
    def test_config_integrity(self):
        self.assertTrue(os.path.exists(CONFIG_FILE), f"Missing {CONFIG_FILE}")
        with open(CONFIG_FILE, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(
            actual_hash, EXPECTED_SHA256,
            f"INTEGRITY BREACH: {CONFIG_FILE} was altered!\nExpected: {EXPECTED_SHA256}\nActual:   {actual_hash}"
        )
        print(f"[PASSED] frozen_config.json verified bit-for-bit with SHA-256: {actual_hash}")

if __name__ == "__main__":
    unittest.main()
