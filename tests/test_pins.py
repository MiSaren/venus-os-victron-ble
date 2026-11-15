"""The root-only PIN file."""

import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from victronble import pins  # noqa: E402


class PinsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "pins.json")

    def test_valid_pin(self):
        self.assertEqual(pins.valid_pin(" 012345\n"), "012345")
        for bad in ("12345", "1234567", "12a456", "", None):
            self.assertIsNone(pins.valid_pin(bad), bad)

    def test_save_load_and_mode(self):
        pins.save(self.path, {"C0:FF:EE:00:00:03": "012345"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(pins.load(self.path), {"C0:FF:EE:00:00:03": "012345"})
        self.assertEqual(pins.macs(self.path), {"C0:FF:EE:00:00:03"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_bad_entries_dropped(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"c0ffee000003": "012345", "nonsense": "012345", "C0:FF:EE:00:00:04": "12"}, f)
        self.assertEqual(pins.load(self.path), {"C0:FF:EE:00:00:03": "012345"})

    def test_missing_or_broken(self):
        self.assertEqual(pins.load(self.path), {})
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{broken")
        self.assertEqual(pins.load(self.path), {})


if __name__ == "__main__":
    unittest.main()
