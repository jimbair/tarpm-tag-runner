#!/usr/bin/env python3
"""Unit and regression tests for tarpm-tag-runner.py.

No koji, tarpm, or network needed: external commands are mocked. Run with:

    python3 test-tarpm-tag-runner.py
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ttr-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.listfile = self.root / "nvrs.txt"
        self.listfile.write_text("demo-1-1\n")
        self.downloads = self.root / "rpms"
        self.argv = ["--from-file", str(self.listfile),
                     "-d", str(self.downloads),
                     "-r", str(self.root / "report.txt"), "--no-run-log"]
        journal = mod.Journal()
        self.enter_patch = patch.object(mod, "JOURNAL", journal)
        self.enter_patch.start()
        self.addCleanup(self.enter_patch.stop)
        self.addCleanup(lambda: journal._fh.close() if journal._fh else None)

    def run_main(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return mod.main(self.argv + list(extra))


class JournalRecoveryTests(RunnerTestCase):
    def test_fetch_only_does_not_make_resume_skip_tests(self):
        with patch.object(mod, "fetch_one", return_value=mod.FetchResult(
                nvr="demo-1-1", ok=True)):
            self.assertEqual(self.run_main("--no-test"), 0)
        latest = mod.read_journal(str(self.downloads / "journal.jsonl"))
        self.assertEqual(latest["demo-1-1"]["status"], "fetched")
        mod.JOURNAL._fh.close()
        test = mod.TestResult(rpm="demo-1-1.noarch.rpm", nvr="demo-1-1",
                              arch="noarch", ok=True)
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", return_value=(
                    mod.FetchResult("demo-1-1", True), [test])) as process:
            self.assertEqual(self.run_main("--resume"), 0)
        process.assert_called_once()
        latest = mod.read_journal(str(self.downloads / "journal.jsonl"))
        self.assertEqual(latest["demo-1-1"]["status"], "pass")

    def test_new_record_survives_torn_tail(self):
        path = self.root / "journal.jsonl"
        old = {"nvr": "old-1-1", "status": "pass"}
        path.write_text(json.dumps(old) + '\n{"nvr":"broken')
        mod.JOURNAL.open(str(path))
        new = {"nvr": "new-1-1", "status": "test-fail"}
        mod.JOURNAL.append(new)
        self.assertEqual(mod.read_journal(str(path)),
                         {old["nvr"]: old, new["nvr"]: new})

    def test_valid_tail_without_newline_is_preserved(self):
        path = self.root / "journal.jsonl"
        old = {"nvr": "old-1-1", "status": "pass"}
        path.write_text(json.dumps(old))
        mod.JOURNAL.open(str(path))
        new = {"nvr": "new-1-1", "status": "pass"}
        mod.JOURNAL.append(new)
        self.assertEqual(mod.read_journal(str(path)),
                         {old["nvr"]: old, new["nvr"]: new})


class FetchRecoveryTests(RunnerTestCase):
    def test_duplicate_nvrs_are_processed_once(self):
        self.listfile.write_text("demo-1-1\ndemo-1-1  # repeated\n")
        with patch.object(mod, "fetch_one", return_value=mod.FetchResult(
                "demo-1-1", True)) as fetch:
            self.assertEqual(self.run_main("--no-test", "-j", "2"), 0)
        fetch.assert_called_once()

    def test_recovery_clears_build_before_download(self):
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                bdir = self.downloads / "demo-1-1"
                bdir.mkdir(parents=True, exist_ok=True)
                (bdir / "partial.rpm").write_text("truncated download")
                journal = self.downloads / "journal.jsonl"
                journal.write_text(json.dumps({"nvr": "demo-1-1",
                                               "status": "test-fail"}) + "\n")

                def process(nvr, args, counter):
                    self.assertFalse(bdir.exists())
                    return mod.FetchResult(nvr, True), []

                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build", side_effect=process) as work:
                    self.assertEqual(self.run_main(mode), 0)
                work.assert_called_once()
                mod.JOURNAL._fh.close()

    def test_failed_resume_cleanup_does_not_reuse_rpms(self):
        (self.downloads / "demo-1-1").mkdir(parents=True)
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod.shutil, "rmtree", side_effect=PermissionError("denied")), \
                patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main("--resume"), 1)
        process.assert_not_called()
        record = mod.read_journal(str(self.downloads / "journal.jsonl"))
        self.assertEqual(record["demo-1-1"]["status"], "fetch-fail")

    def test_no_src_discards_source_downloads(self):
        for arch in ([], ["-a", "x86_64"]):
            with self.subTest(arch=arch):
                args = mod.parse_args(self.argv + ["--no-src", "--keep"] + arch)
                (self.downloads / "logs").mkdir(parents=True, exist_ok=True)

                def download(cmd, cwd, **kwargs):
                    (cwd / "demo-1-1.src.rpm").write_text("source")
                    (cwd / "demo-1-1.x86_64.rpm").write_text("binary")
                    return 0, "downloaded"

                with patch.object(mod, "run", side_effect=download), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = mod.fetch_one("demo-1-1", args)
                self.assertTrue(result.ok)
                self.assertFalse((self.downloads / "demo-1-1/demo-1-1.src.rpm").exists())
                self.assertTrue((self.downloads / "demo-1-1/demo-1-1.x86_64.rpm").exists())

    def test_no_src_preserves_partial_download_on_failure(self):
        args = mod.parse_args(self.argv + ["--no-src"])
        (self.downloads / "logs").mkdir(parents=True)
        bdir = self.downloads / "demo-1-1"
        bdir.mkdir()
        source = bdir / "demo-1-1.src.rpm"
        source.write_text("partial source")
        with patch.object(mod, "run", return_value=(1, "download failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            result = mod.fetch_one("demo-1-1", args)
        self.assertFalse(result.ok)
        self.assertTrue(source.exists())


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
