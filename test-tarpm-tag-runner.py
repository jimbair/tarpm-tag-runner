#!/usr/bin/env python3
"""Unit tests for the pure helpers in tarpm-tag-runner.py.

No koji, tarpm, or network needed: only the parts of the script that
are pure functions are exercised. Run with:

    python3 test-tarpm-tag-runner.py
"""
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_module():
    path = os.path.join(_HERE, "tarpm-tag-runner.py")
    spec = importlib.util.spec_from_file_location("tarpm_tag_runner", path)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations against sys.modules, so the
    # module must be registered before exec_module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_module()


class ErrorSignatureTests(unittest.TestCase):
    def test_create_payload_line(self):
        # The "tarpm: " line is used, with the prefix stripped.
        out = ("Downloading [1/21]: 7zip-25.01-5.fc44.src.rpm\n"
               "tarpm: create_payload: unable to write payload data\n")
        self.assertEqual(mod.error_signature(out),
                         "create_payload: unable to write payload data")

    def test_last_tarpm_line_wins(self):
        # The LAST "tarpm: " line wins, even if other lines follow.
        out = ("tarpm: first error\n"
               "koji: some noise line\n"
               "tarpm: unable to construct RPM header\n")
        self.assertEqual(mod.error_signature(out),
                         "unable to construct RPM header")

    def test_fallback_last_nonempty_line(self):
        # Without a "tarpm: " line, the last non-empty line is used
        # verbatim (only the "tarpm: " prefix gets stripped).
        out = ("koji: warning: something odd\n"
               "unable to construct RPM header\n")
        self.assertEqual(mod.error_signature(out),
                         "unable to construct RPM header")

    def test_fallback_keeps_other_prefixes(self):
        # A 'tarpm: ' line is never assumed for other tools' prefixes.
        out = "error: unable to construct RPM header from /tmp/x/y/rpm\n"
        self.assertEqual(mod.error_signature(out),
                         "error: unable to construct RPM header from <path>")

    def test_path_normalization(self):
        # Absolute paths collapse to <path>.
        out = ("tarpm: failed to read payload from "
               "/home/jim/rpms/7zip-25.01-5.fc44/7zip-25.01-5.fc44.src.rpm\n")
        self.assertEqual(mod.error_signature(out),
                         "failed to read payload from <path>")

    def test_hex_and_digit_runs(self):
        # Hex addresses and 4+ digit runs collapse to N (shorter runs
        # such as '42' are left alone).
        out = "tarpm: assertion failed at 0x7f8a12345678 in loop 42 123456\n"
        self.assertEqual(mod.error_signature(out),
                         "assertion failed at N in loop 42 N")

    def test_truncated_to_120_chars(self):
        out = "tarpm: " + "x" * 300 + "\n"
        self.assertEqual(len(mod.error_signature(out)), 120)

    def test_empty_output(self):
        self.assertEqual(mod.error_signature(""), "")
        self.assertEqual(mod.error_signature("\n  \n\t\n"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
