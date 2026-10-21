#!/usr/bin/env python3
"""Unit and regression tests for tarpm-tag-runner.py.

No koji, tarpm, or network needed: external commands are mocked. Run with:

    python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = _ROOT / "tarpm-tag-runner.py"
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


class JournalWriteTests(RunnerTestCase):
    def test_short_writes_complete_each_record(self):
        path = self.root / "journal.jsonl"
        mod.JOURNAL.open(str(path))
        real_write = os.write
        records = [{"nvr": "first-1-1", "status": "test-fail"},
                   {"nvr": "second-1-1", "status": "pass"}]
        with patch.object(mod.os, "write", side_effect=lambda fd, data: real_write(fd, data[:7])):
            for record in records:
                mod.JOURNAL.append(record)
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], records)

    def test_failed_append_rolls_back_before_next_record(self):
        path = self.root / "journal.jsonl"
        mod.JOURNAL.open(str(path))
        old = {"nvr": "old-1-1", "status": "pass"}
        mod.JOURNAL.append(old)
        before = path.read_bytes()
        real_write = os.write
        calls = 0

        def fail_after_partial_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:12])
            raise OSError("disk full")

        stderr = io.StringIO()
        with patch.object(mod.os, "write", side_effect=fail_after_partial_write), \
                contextlib.redirect_stderr(stderr):
            mod.JOURNAL.append({"nvr": "broken-1-1", "status": "test-fail"})
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("could not append journal record", stderr.getvalue())
        new = {"nvr": "new-1-1", "status": "pass"}
        mod.JOURNAL.append(new)
        self.assertEqual(mod.read_journal(str(path)), {old["nvr"]: old, new["nvr"]: new})

    def test_zero_length_write_does_not_loop_or_damage_next_record(self):
        path = self.root / "journal.jsonl"
        mod.JOURNAL.open(str(path))
        with patch.object(mod.os, "write", return_value=0), \
                contextlib.redirect_stderr(io.StringIO()):
            mod.JOURNAL.append({"nvr": "broken-1-1", "status": "test-fail"})
        record = {"nvr": "new-1-1", "status": "pass"}
        mod.JOURNAL.append(record)
        self.assertEqual(mod.read_journal(str(path)), {record["nvr"]: record})

    def test_failed_rollback_disables_further_appends(self):
        path = self.root / "journal.jsonl"
        mod.JOURNAL.open(str(path))
        real_write = os.write
        calls = 0

        def fail_after_partial_write(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:12])
            raise OSError("disk full")

        stderr = io.StringIO()
        with patch.object(mod.os, "write", side_effect=fail_after_partial_write), \
                patch.object(mod.os, "ftruncate", side_effect=OSError("repair failed")), \
                contextlib.redirect_stderr(stderr):
            mod.JOURNAL.append({"nvr": "broken-1-1", "status": "test-fail"})
        self.assertIsNone(mod.JOURNAL._fh)
        self.assertIn("journal disabled: rollback failed", stderr.getvalue())
        before = path.read_bytes()
        with patch.object(mod.os, "write") as write:
            mod.JOURNAL.append({"nvr": "new-1-1", "status": "pass"})
        write.assert_not_called()
        self.assertEqual(path.read_bytes(), before)


class CliSelectionTests(RunnerTestCase):
    def list_output(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = mod.main(self.argv + ["--list"] + list(options))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_test_only_list_is_rejected_without_touching_rpms(self):
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_text("original rpm")
        with patch.object(mod, "run") as command, \
                self.assertRaises(SystemExit) as error:
            self.run_main("--test-only", "--list")
        self.assertEqual(error.exception.code, 2)
        command.assert_not_called()
        self.assertEqual(rpm.read_text(), "original rpm")
        self.assertFalse((self.root / "report.txt").exists())

    def test_file_list_stdout_round_trips_without_diagnostics(self):
        result, stdout, stderr = self.list_output()
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "demo-1-1\n")
        self.assertIn("list from", stderr)
        self.listfile.write_text(stdout)
        with contextlib.redirect_stdout(io.StringIO()):
            nvrs, _ = mod.read_nvrs(mod.parse_args(self.argv))
        self.assertEqual(nvrs, ["demo-1-1"])

    def test_cached_list_stdout_contains_only_nvrs(self):
        self.downloads.mkdir()
        (self.downloads / "tag-f44.txt").write_text(
            "Build Tag Built by\n------------------\ndemo-1-1 f44 user\n")
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        with patch.object(mod, "run") as command:
            result, stdout, stderr = self.list_output()
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "demo-1-1\n")
        self.assertIn("using cached list", stderr)
        command.assert_not_called()

    def test_fresh_list_and_heartbeat_diagnostics_use_stderr(self):
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        proc = Mock()
        proc.communicate.side_effect = [
            mod.subprocess.TimeoutExpired(["koji"], 60),
            ("demo-1-1 f44 user\n", None),
        ]
        proc.returncode = 0
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod.subprocess, "Popen", return_value=proc):
            result, stdout, stderr = self.list_output()
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "demo-1-1\n")
        self.assertIn("still fetching koji tag", stderr)
        self.assertIn("saved tag list", stderr)

    def test_resume_list_excludes_passed_and_warned_builds(self):
        self.listfile.write_text("pass-1-1\nwarn-1-1\nfail-1-1\nnew-1-1\n")
        self.downloads.mkdir()
        records = [{"nvr": "pass-1-1", "status": "pass"},
                   {"nvr": "warn-1-1", "status": "warn"},
                   {"nvr": "fail-1-1", "status": "test-fail"}]
        (self.downloads / "journal.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records))
        with patch.object(mod, "process_build") as process:
            result, stdout, stderr = self.list_output("--resume")
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "fail-1-1\nnew-1-1\n")
        self.assertIn("skipping 2 of 4", stderr)
        process.assert_not_called()

    def test_empty_recovery_lists_leave_stdout_empty(self):
        self.downloads.mkdir()
        (self.downloads / "journal.jsonl").write_text(json.dumps(
            {"nvr": "demo-1-1", "status": "pass"}) + "\n")
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                result, stdout, stderr = self.list_output(mode)
                self.assertEqual(result, 0)
                self.assertEqual(stdout, "")
                self.assertIn("nothing to do", stderr)
                mod.JOURNAL._fh.close()


class ReportPathTests(unittest.TestCase):
    def test_retry_default_matches_cli_and_explicit_argv(self):
        fixed = datetime(2026, 10, 21, 12, 30, 45, tzinfo=timezone.utc)
        with patch.object(mod, "datetime") as clock:
            clock.now.return_value = fixed
            explicit = mod.parse_args(["--retry-failed"])
            with patch.object(sys, "argv", ["tarpm-tag-runner.py", "--retry-failed"]):
                cli = mod.parse_args(None)
        self.assertEqual(explicit.report, "report-retry-20261021-123045.txt")
        self.assertEqual(cli.report, explicit.report)

    def test_explicit_report_paths_are_preserved(self):
        for report in ("report.txt", "custom.txt"):
            for mode in ([], ["--retry-failed"]):
                with self.subTest(report=report, mode=mode):
                    args = mod.parse_args(mode + ["-r", report])
                    self.assertEqual(args.report, report)
        self.assertEqual(mod.parse_args([]).report, "report.txt")


class BuildPathTests(RunnerTestCase):
    def test_test_only_rejects_symlink_before_testing_any_rpm(self):
        outside = self.root / "outside-1-1"
        outside.mkdir()
        original = outside / "demo-1-1.noarch.rpm"
        original.write_text("external original")
        valid = self.downloads / "aaa-1-1/aaa-1-1.noarch.rpm"
        valid.parent.mkdir(parents=True)
        valid.write_text("local original")
        (self.downloads / "demo-1-1").symlink_to(outside, target_is_directory=True)
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "run") as command:
            self.assertEqual(self.run_main("--test-only"), 2)
        command.assert_not_called()
        self.assertEqual(original.read_text(), "external original")
        self.assertEqual(valid.read_text(), "local original")

    def test_test_only_cleanup_leaves_filtered_symlink_alone(self):
        outside = self.root / "empty-outside"
        outside.mkdir()
        rpm = self.downloads / "good-1-1/good-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_text("local original")
        link = self.downloads / "other-1-1"
        link.symlink_to(outside, target_is_directory=True)
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "run", return_value=(0, "listing\n")):
            self.assertEqual(self.run_main("--test-only", "-p", "good"), 0)
        self.assertFalse(rpm.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.is_dir())

    def test_invalid_nvrs_are_rejected_before_any_build_runs(self):
        outside = self.root / "outside-1-1"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("unrelated data")
        for invalid in ("../outside-1-1", str(outside), ".", "..", "demo",
                        "demo--1", "dir/demo-1-1", r"dir\demo-1-1"):
            with self.subTest(nvr=invalid):
                self.listfile.write_text(f"demo-1-1\n{invalid}\n")
                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build") as process:
                    self.assertEqual(self.run_main("--resume"), 2)
                process.assert_not_called()
                self.assertEqual(marker.read_text(), "unrelated data")

    def test_valid_nvrs_include_rpm_version_separators(self):
        for nvr in ("hello-2.12.2-1.fc44", "389-ds-base-3.1.2-1.fc44",
                    "libstdc++-16.0.1-0.1.fc44", "demo-1.0~rc1^git-1.fc44"):
            with self.subTest(nvr=nvr):
                self.assertEqual(mod.build_dir(str(self.downloads), nvr),
                                 self.downloads / nvr)

    def test_direct_fetch_rejects_paths_and_symlinks(self):
        self.downloads.mkdir()
        outside = self.root / "outside-1-1"
        outside.mkdir()
        (self.downloads / "demo-1-1").symlink_to(outside, target_is_directory=True)
        args = mod.parse_args(self.argv)
        for nvr in ("../outside-1-1", "demo-1-1"):
            with self.subTest(nvr=nvr), patch.object(mod, "run") as command:
                with self.assertRaises(mod.UsageError):
                    mod.fetch_one(nvr, args)
                command.assert_not_called()

    def test_recovery_does_not_delete_symlink_targets(self):
        outside = self.root / "outside-1-1"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("unrelated data")
        self.downloads.mkdir()
        link = self.downloads / "demo-1-1"
        link.symlink_to(outside, target_is_directory=True)
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                (self.downloads / "journal.jsonl").write_text(json.dumps(
                    {"nvr": "demo-1-1", "status": "test-fail"}) + "\n")
                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build") as process:
                    self.assertEqual(self.run_main(mode), 1)
                process.assert_not_called()
                self.assertEqual(marker.read_text(), "unrelated data")
                self.assertTrue(link.is_symlink())
                mod.JOURNAL._fh.close()


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


class ArtifactRetentionTests(RunnerTestCase):
    def test_warning_retains_only_log_unless_keep(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                args = mod.parse_args(self.argv + (["--keep"] if keep else []))
                rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
                rpm.parent.mkdir(parents=True, exist_ok=True)
                rpm.write_text("original rpm")
                (self.downloads / "logs").mkdir(exist_ok=True)
                outputs = [(0, ""), (0, ""), (0, ""),
                           (0, "original list\n"), (0, "changed list\n")]
                with patch.object(mod, "run", side_effect=outputs), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = mod.test_one(rpm, args)
                self.assertTrue(result.ok)
                self.assertEqual(result.warning, "content-diff")
                self.assertIn("changed list", result.log.read_text())
                self.assertEqual(rpm.exists(), keep)
                self.assertEqual((rpm.parent / ".tarpm-demo-1-1.noarch").exists(), keep)

    def test_retry_failed_does_not_select_unfinished_build(self):
        bdir = self.downloads / "demo-1-1"
        bdir.mkdir(parents=True)
        (bdir / "partial.rpm").write_text("unfinished download")
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main("--retry-failed"), 0)
        process.assert_not_called()


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


class ForceQuitTests(unittest.TestCase):
    def child_command(self, body):
        script = (
            "import importlib.util, os, signal, sys\n"
            f"spec = importlib.util.spec_from_file_location('force_test', "
            f"{str(_ROOT / 'tarpm-tag-runner.py')!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name] = mod\n"
            "spec.loader.exec_module(mod)\n"
        )
        return [sys.executable, "-c", script + body]

    def test_second_interrupt_exits_while_log_lock_is_held(self):
        command = self.child_command(
            "class InterruptedLog:\n"
            "    def write(self, text):\n"
            "        os.kill(os.getpid(), signal.SIGINT)\n"
            "    def flush(self):\n"
            "        pass\n"
            "mod.RUNLOG._fh = InterruptedLog()\n"
            "signal.signal(signal.SIGINT, mod._force_quit)\n"
            "mod.RUNLOG.write('interrupted main-thread log write')\n")
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 130)
        self.assertIn("force-quit", result.stderr)

    def test_force_quit_exits_with_closed_stderr(self):
        result = subprocess.run(self.child_command(
            "os.close(2)\nmod._force_quit(signal.SIGINT, None)\n"),
            capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 130)

    def test_force_quit_exits_with_full_stderr_pipe(self):
        command = self.child_command(
            "os.set_blocking(2, False)\n"
            "try:\n"
            "    while True:\n"
            "        os.write(2, b'x' * 4096)\n"
            "except BlockingIOError:\n"
            "    pass\n"
            "os.set_blocking(2, True)\n"
            "mod._force_quit(signal.SIGINT, None)\n")
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.wait(timeout=5), 130)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()


class PoolInterruptTests(RunnerTestCase):
    def exercise_interrupt(self, phase):
        started, release = threading.Event(), threading.Event()
        completed = []

        def worker(item):
            started.set()
            if not release.wait(5):
                raise RuntimeError("worker was not released after interrupt")
            completed.append(item)
            return item

        def items():
            yield "active"
            self.assertTrue(started.wait(5))
            yield "queued"
            if phase == "submission":
                raise KeyboardInterrupt

        original_handler = signal.getsignal(signal.SIGINT)
        with patch.object(mod, "log", side_effect=lambda *a, **kw: release.set()):
            if phase == "collection":
                with patch.object(mod.concurrent.futures, "as_completed",
                                  side_effect=KeyboardInterrupt):
                    results, interrupted = mod.run_pool(worker, items(), 1)
            else:
                results, interrupted = mod.run_pool(worker, items(), 1)
        self.assertTrue(interrupted)
        self.assertEqual(results, ["active"])
        self.assertEqual(completed, ["active"])
        self.assertEqual(signal.getsignal(signal.SIGINT), original_handler)

    def test_interrupt_during_submission_drops_queued_work(self):
        self.exercise_interrupt("submission")

    def test_interrupt_during_collection_drops_queued_work(self):
        self.exercise_interrupt("collection")

    def test_interrupt_before_future_is_tracked_keeps_partial_report(self):
        started, release = threading.Event(), threading.Event()
        executor = mod.concurrent.futures.ThreadPoolExecutor

        class InterruptedSubmit(executor):
            def submit(self, *args, **kwargs):
                super().submit(*args, **kwargs)
                if not started.wait(5):
                    raise RuntimeError("worker did not start")
                # Simulate SIGINT after enqueueing, before returning the
                # future to run_pool's tracking list.
                raise KeyboardInterrupt

        def process(nvr, args, counter):
            started.set()
            if not release.wait(5):
                raise RuntimeError("worker was not released after interrupt")
            return (mod.FetchResult(nvr, True),
                    [mod.TestResult(rpm=nvr + ".noarch.rpm", nvr=nvr,
                                    arch="noarch", ok=True)])

        def log(message, **kwargs):
            if "interrupted:" in message:
                release.set()

        json_path = self.root / "partial.json"
        original_handler = signal.getsignal(signal.SIGINT)
        with patch.object(mod.concurrent.futures, "ThreadPoolExecutor", InterruptedSubmit), \
                patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", side_effect=process), \
                patch.object(mod, "log", side_effect=log):
            self.assertEqual(self.run_main("--json", str(json_path)), 130)
        report = (self.root / "report.txt").read_text()
        self.assertIn("interrupted: yes", report)
        self.assertIn("completed: 1 build(s)", report)
        data = json.loads(json_path.read_text())
        self.assertEqual(len(data["fetch"]), 1)
        self.assertEqual(len(data["test"]), 1)
        latest = mod.read_journal(str(self.downloads / "journal.jsonl"))
        self.assertEqual(latest["demo-1-1"]["status"], "pass")
        self.assertEqual(signal.getsignal(signal.SIGINT), original_handler)


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
