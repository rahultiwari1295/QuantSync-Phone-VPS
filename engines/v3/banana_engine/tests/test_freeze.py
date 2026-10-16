"""
tests/test_freeze.py — Integrity Guard for Frozen Architecture & Artifacts
==========================================================================
Enforces that core modeling, feature engineering, structural pattern extraction,
and model weight artifacts remain strictly bit-for-bit unchanged during observation.
"""

import os
import re
import hashlib
import unittest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestFreezeIntegrity(unittest.TestCase):

    def test_frozen_artifacts_integrity(self):
        """Verifies that all frozen modules and model artifacts match locked SHA-256 hashes."""
        lock_file = os.path.join(_PKG_ROOT, "FROZEN_VERSIONS.md")
        self.assertTrue(os.path.exists(lock_file), f"Missing {lock_file}")

        with open(lock_file, "r") as f:
            content = f.read()

        # Parse file paths and hashes from the code block in FROZEN_VERSIONS.md
        # Pattern matches: <path> <64-char hex>
        entries = re.findall(r"^([\w\/\.\-]+)\s+([a-f0-9]{64})$", content, re.MULTILINE)
        self.assertGreaterEqual(len(entries), 8, "Expected at least 8 locked files in FROZEN_VERSIONS.md")

        for rel_path, expected_hash in entries:
            abs_path = os.path.join(_PKG_ROOT, rel_path)
            self.assertTrue(os.path.exists(abs_path), f"Frozen file missing: {rel_path}")

            with open(abs_path, "rb") as fp:
                actual_hash = hashlib.sha256(fp.read()).hexdigest()

            self.assertEqual(
                actual_hash, expected_hash,
                f"INTEGRITY BREACH: Frozen file {rel_path} was modified!\nExpected: {expected_hash}\nActual:   {actual_hash}"
            )
            print(f"[Freeze Guard Verified] {rel_path} matches locked SHA-256")


if __name__ == "__main__":
    unittest.main()
