#!/usr/bin/env python3
"""Unit tests for the pure helpers in tarpm-tag-runner.py.

No koji, tarpm, or network needed: only the parts of the script that
are pure functions are exercised. Run with:

    python3 test-tarpm-tag-runner.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
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


class JournalInterruptTests(unittest.TestCase):
    """A Ctrl+C mid-run must keep the records of the builds that finished."""

    def test_interrupted_run_keeps_finished_records(self):
        d = tempfile.mkdtemp(prefix="ttr-journal-")
        listf = os.path.join(d, "nvrs.txt")
        nvrs = ["aaa-1-1.fc44", "bbb-1-1.fc44", "ccc-1-1.fc44",
                "ddd-1-1.fc44"]
        with open(listf, "w", encoding="utf-8") as fh:
            fh.write("\n".join(nvrs) + "\n")

        real_process_build = mod.process_build
        real_run_pool = mod.run_pool
        real_which = mod.shutil.which

        def fake_process_build(nvr, args, fetch_counter=None):
            return (mod.FetchResult(nvr=nvr, ok=True),
                    [mod.TestResult(rpm=nvr + ".noarch", nvr=nvr,
                                    arch="noarch", ok=True)])

        def fake_run_pool(worker, items, jobs, on_error=None):
            # The first three builds finish, then the main thread is
            # interrupted before the fourth starts.
            for item in list(items)[:3]:
                worker(item)
            raise KeyboardInterrupt

        mod.process_build = fake_process_build
        mod.run_pool = fake_run_pool
        mod.shutil.which = lambda name: "/usr/bin/fake"  # tarpm gate
        try:
            with self.assertRaises(KeyboardInterrupt):
                mod.main(argv=["--from-file", listf, "-d", d,
                               "--no-run-log"])
        finally:
            mod.process_build = real_process_build
            mod.run_pool = real_run_pool
            mod.shutil.which = real_which
            if mod.JOURNAL._fh is not None:
                mod.JOURNAL._fh.close()
                mod.JOURNAL._fh = None

        # The records were written as each build finished (one os.write
        # per line), so they are on disk despite the interrupt.
        with open(os.path.join(d, "journal.jsonl"), "r",
                  encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual([r["nvr"] for r in records], nvrs[:3])
        for r in records:
            self.assertEqual(r["status"], "pass")
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
