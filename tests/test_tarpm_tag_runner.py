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


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ttr-update-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / "runner.py"
        self.original = b"# installed runner\n"
        self.target.write_bytes(self.original)
        self.target.chmod(0o755)
        self.source = (b"#!/usr/bin/env python3\n"
                       b"def parse_args(argv): pass\n"
                       b"def main(argv=None): return 0\n"
                       b"raise RuntimeError('must not execute during update')\n")
        file_patch = patch.object(mod, "__file__", str(self.target))
        file_patch.start()
        self.addCleanup(file_patch.stop)

    def response(self, source=None, length=None):
        source = self.source if source is None else source
        response = io.BytesIO(source)
        response.headers = {"Content-Length": str(
            len(source) if length is None else length)}
        return response

    def run_update(self, flag="--update"):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        with contextlib.redirect_stdout(self.stdout), \
                contextlib.redirect_stderr(self.stderr):
            return mod.main([flag])

    def assert_unchanged(self):
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_both_flags_replace_script_preserve_mode_and_skip_run_setup(self):
        for flag in ("-u", "--update"):
            with self.subTest(flag=flag):
                self.target.write_bytes(self.original)
                with patch.object(mod.urllib.request, "urlopen",
                                  return_value=self.response()) as download, \
                        patch.object(mod.RUNLOG, "open") as runlog, \
                        patch.object(mod.JOURNAL, "open") as journal, \
                        patch.object(mod.shutil, "which") as which:
                    self.assertEqual(self.run_update(flag), 0)
                download.assert_called_once_with(mod.UPDATE_URL, timeout=30)
                runlog.assert_not_called()
                journal.assert_not_called()
                which.assert_not_called()
                self.assertEqual(self.target.read_bytes(), self.source)
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
                self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_identical_download_does_not_replace_file(self):
        self.target.write_bytes(self.source)
        with patch.object(mod.urllib.request, "urlopen",
                          return_value=self.response()), \
                patch.object(mod.os, "replace") as replace:
            self.assertEqual(self.run_update(), 0)
        replace.assert_not_called()
        self.assertIn("already up to date", self.stdout.getvalue())

    def test_command_line_argv_and_response_without_content_length(self):
        response = self.response()
        response.headers = {}
        with patch.object(mod.sys, "argv", [str(self.target), "-u"]), \
                patch.object(mod.urllib.request, "urlopen", return_value=response), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(), 0)
        self.assertEqual(self.target.read_bytes(), self.source)

    def test_update_through_symlink_preserves_link(self):
        link = self.root / "linked-runner"
        link.symlink_to(self.target.name)
        with patch.object(mod, "__file__", str(link)), \
                patch.object(mod.urllib.request, "urlopen",
                             return_value=self.response()):
            self.assertEqual(self.run_update(), 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), self.source)
        self.assertEqual(self.target.read_bytes(), self.source)

    def test_network_and_http_failures_leave_script_unchanged(self):
        errors = [mod.urllib.error.URLError("connection failed"),
                  mod.urllib.error.HTTPError(mod.UPDATE_URL, 404,
                                             "Not Found", {}, None),
                  TimeoutError("timed out"),
                  mod.http.client.IncompleteRead(b"partial")]
        for error in errors:
            with self.subTest(error=error), \
                    patch.object(mod.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(self.run_update(), 2)
                self.assertIn("update failed", self.stderr.getvalue())
                self.assert_unchanged()

    def test_invalid_or_unrelated_download_leaves_script_unchanged(self):
        for source in (b"", b"<html>error</html>", b"def main(:\n",
                       b"print('unrelated script')\n",
                       b"def main(): pass\ndef parse_args(): pass\nreturn\n"):
            with self.subTest(source=source), \
                    patch.object(mod.urllib.request, "urlopen",
                                 return_value=self.response(source)):
                self.assertEqual(self.run_update(), 2)
                self.assert_unchanged()

    def test_incomplete_or_oversized_download_leaves_script_unchanged(self):
        for source, length in ((self.source, len(self.source) + 10),
                               (b"x" * (mod.MAX_UPDATE_BYTES + 1), None)):
            with self.subTest(length=length), \
                    patch.object(mod.urllib.request, "urlopen",
                                 return_value=self.response(source, length)):
                self.assertEqual(self.run_update(), 2)
                self.assert_unchanged()

    def test_write_or_replace_failure_preserves_script_and_cleans_tempfile(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation), \
                    patch.object(mod.urllib.request, "urlopen",
                                 return_value=self.response()), \
                    patch.object(mod.os, operation,
                                 side_effect=PermissionError("permission denied")):
                self.assertEqual(self.run_update(), 2)
                self.assertIn("permission denied", self.stderr.getvalue())
                self.assert_unchanged()

    def test_update_rejects_run_options_before_downloading(self):
        for extra in (["--no-test"], ["-p", "hello"], ["--list"],
                      ["--test-only"], ["-d", "other"]):
            with self.subTest(extra=extra), \
                    patch.object(mod.urllib.request, "urlopen") as download, \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                mod.main(["--update", *extra])
            self.assertEqual(error.exception.code, 2)
            download.assert_not_called()

    def test_cleanup_failure_still_reports_update_error_without_traceback(self):
        with patch.object(mod.urllib.request, "urlopen",
                          return_value=self.response()), \
                patch.object(mod.os, "replace", side_effect=PermissionError("denied")), \
                patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            self.assertEqual(self.run_update(), 2)
        self.assertIn("could not remove temporary update", self.stderr.getvalue())
        self.assertEqual(self.target.read_bytes(), self.original)


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
        # Main-loop tests must not query the host's RPM database.
        package_patch = patch.object(mod, "tarpm_package_info", return_value={
            "executable": "/fake/tarpm", "package": {}, "rpm_info": None,
            "error": "RPM metadata unavailable in unit tests"})
        package_patch.start()
        self.addCleanup(package_patch.stop)

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


class RunManifestTests(RunnerTestCase):
    def passed_build(self, nvr, args, counter):
        return (mod.FetchResult(nvr, True),
                [mod.TestResult(rpm=nvr + ".noarch.rpm", nvr=nvr,
                                arch="noarch", ok=True)])

    def test_interrupted_rerun_resumes_active_and_queued_old_passes(self):
        nvrs = ["done-1-1", "active-1-1", "queued-1-1"]
        self.listfile.write_text("\n".join(nvrs) + "\n")
        manifest_path = self.downloads / "run.json"
        mod.write_run_manifest(manifest_path, nvrs, "old-run")
        (self.downloads / "journal.jsonl").write_text("".join(
            json.dumps({"nvr": nvr, "status": "pass", "run_id": "old-run"}) + "\n"
            for nvr in nvrs))
        partial = self.downloads / "active-1-1/partial.rpm"

        def process(nvr, args, counter):
            current = mod.read_run_manifest(manifest_path)
            self.assertNotEqual(current["run_id"], "old-run")
            self.assertEqual(current["nvrs"], nvrs)
            if nvr == "active-1-1":
                partial.parent.mkdir()
                partial.write_text("incomplete rerun")
                raise KeyboardInterrupt
            return self.passed_build(nvr, args, counter)

        def interrupted_pool(worker, items, jobs, on_error=None):
            worker(items[0])
            worker(items[1])
            self.fail("active build should interrupt before queued build starts")

        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", side_effect=process), \
                patch.object(mod, "run_pool", side_effect=interrupted_pool), \
                self.assertRaises(KeyboardInterrupt):
            self.run_main()
        run_id = mod.read_run_manifest(manifest_path)["run_id"]
        self.assertTrue(partial.exists())
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--resume"), 0)
        self.assertEqual([call.args[0] for call in process.call_args_list], nvrs[1:])
        self.assertFalse(partial.exists())
        current = mod.read_journal(str(self.downloads / "journal.jsonl"), run_id=run_id)
        self.assertEqual(set(current), set(nvrs))
        self.assertTrue(all(record["status"] == "pass" for record in current.values()))
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main("--resume"), 0)
        process.assert_not_called()
        self.assertEqual(mod.read_run_manifest(manifest_path)["run_id"], run_id)

    def test_failed_manifest_replace_preserves_previous_run_and_rpms(self):
        path = self.downloads / "run.json"
        mod.write_run_manifest(path, ["demo-1-1"], "previous-run")
        before = path.read_bytes()
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir()
        rpm.write_text("previous RPM")
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod.os, "replace", side_effect=OSError("disk full")), \
                patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main(), 2)
        process.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(rpm.read_text(), "previous RPM")
        self.assertEqual(list(self.downloads.glob(".run-*.tmp")), [])

    def test_invalid_manifest_never_falls_back_to_old_passes(self):
        self.downloads.mkdir()
        (self.downloads / "journal.jsonl").write_text(json.dumps(
            {"nvr": "demo-1-1", "status": "pass"}) + "\n")
        for invalid in ('{"run_id":', '{}',
                        '{"run_id":"new", "nvrs":["../demo-1-1"]}'):
            with self.subTest(manifest=invalid):
                (self.downloads / "run.json").write_text(invalid)
                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build") as process:
                    self.assertEqual(self.run_main("--resume"), 2)
                process.assert_not_called()

    def test_list_does_not_replace_manifest(self):
        path = self.downloads / "run.json"
        mod.write_run_manifest(path, ["demo-1-1"], "existing-run")
        before = path.read_bytes()
        for options in (["--list"], ["--resume", "--list"]):
            with self.subTest(options=options):
                self.assertEqual(self.run_main(*options), 0)
                self.assertEqual(path.read_bytes(), before)

    def test_filtered_resume_preserves_other_pending_builds(self):
        nvrs = ["aaa-1-1", "bbb-1-1"]
        self.listfile.write_text("\n".join(nvrs) + "\n")
        path = self.downloads / "run.json"
        mod.write_run_manifest(path, nvrs, "current-run")
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--resume", "-p", "aaa"), 0)
        self.assertEqual([call.args[0] for call in process.call_args_list], nvrs[:1])
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--resume"), 0)
        self.assertEqual([call.args[0] for call in process.call_args_list], nvrs[1:])
        self.assertEqual(mod.read_run_manifest(path)["nvrs"], nvrs)
        self.assertEqual(mod.read_run_manifest(path)["run_id"], "current-run")

    def test_resume_does_not_add_builds_outside_active_run(self):
        self.listfile.write_text("demo-1-1\nother-1-1\n")
        mod.write_run_manifest(self.downloads / "run.json", ["demo-1-1"], "current-run")
        (self.downloads / "journal.jsonl").write_text(json.dumps(
            {"nvr": "demo-1-1", "status": "pass", "run_id": "current-run"}) + "\n")
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main("--resume"), 0)
        process.assert_not_called()

    def test_retry_uses_historical_failures_but_starts_a_new_run(self):
        path = self.downloads / "run.json"
        mod.write_run_manifest(path, ["other-1-1"], "previous-run")
        (self.downloads / "journal.jsonl").write_text(json.dumps(
            {"nvr": "demo-1-1", "status": "test-fail", "run_id": "older-run"}) + "\n")
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--retry-failed"), 0)
        process.assert_called_once()
        manifest = mod.read_run_manifest(path)
        self.assertNotEqual(manifest["run_id"], "previous-run")
        self.assertEqual(manifest["nvrs"], ["demo-1-1"])
        current = mod.read_journal(str(self.downloads / "journal.jsonl"))
        self.assertEqual(current["demo-1-1"]["run_id"], manifest["run_id"])


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

    def test_resume_list_closed_pipe_exits_without_tracebacks(self):
        self.downloads.mkdir()
        journal = self.downloads / "journal.jsonl"
        journal.write_text(json.dumps({"nvr": "demo-0-1", "status": "test-fail",
                                       "run_id": "pipe-test"}) + "\n")
        before = journal.read_bytes()
        manifest = self.downloads / "run.json"
        runlog = self.root / "pipe-run.log"
        env = os.environ.copy()
        env.pop("PYTHONUNBUFFERED", None)
        # Small output fails at the final flush; large output fails while
        # printing. Both must avoid the interpreter's shutdown traceback.
        for count in (1, 5000):
            with self.subTest(count=count):
                nvrs = [f"demo-{i}-1" for i in range(count)]
                self.listfile.write_text("\n".join(nvrs) + "\n")
                mod.write_run_manifest(manifest, nvrs, "pipe-test")
                saved_manifest = manifest.read_bytes()
                reader, writer = os.pipe()
                os.close(reader)
                try:
                    result = subprocess.run(
                        [sys.executable, str(_ROOT / "tarpm-tag-runner.py"),
                         *self.argv[:-1], "--run-log", str(runlog),
                         "--resume", "--list"],
                        stdout=writer, stderr=subprocess.PIPE, text=True,
                        env=env, timeout=10)
                finally:
                    os.close(writer)
                self.assertEqual(result.returncode, 1)
                for output in (result.stderr, runlog.read_text()):
                    self.assertNotIn("BrokenPipeError", output)
                    self.assertNotIn("Traceback", output)
                    self.assertNotIn("Exception ignored", output)
                self.assertEqual(journal.read_bytes(), before)
                self.assertEqual(manifest.read_bytes(), saved_manifest)
                self.assertFalse((self.root / "report.txt").exists())

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
            mod.RPM_TAG_CACHE_HEADER +
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

    def test_tag_query_filters_rpm_builds_and_cache_is_reused(self):
        self.argv = ["-t", "f45", "-d", str(self.downloads), "--no-run-log"]
        output = ("Build Tag Built by\n------------------\n"
                  "Fedora-rpm-example-1-1 f45 user\nhello-2-1 f45 user\n")
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod, "run", return_value=(0, output)) as command:
            result, stdout, _ = self.list_output()
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "Fedora-rpm-example-1-1\nhello-2-1\n")
        self.assertEqual(command.call_args.args[0],
                         ["koji", "latest-pkg", "--all", "--type=rpm", "f45"])
        self.assertEqual((self.downloads / "tag-f45.txt").read_text(),
                         mod.RPM_TAG_CACHE_HEADER + output)
        with patch.object(mod, "run") as command, \
                patch.object(mod.shutil, "which", return_value=None):
            result, cached_stdout, stderr = self.list_output()
        self.assertEqual(result, 0)
        self.assertEqual(cached_stdout, stdout)
        self.assertIn("using cached list", stderr)
        command.assert_not_called()

    def test_legacy_cache_refreshes_before_package_filter_and_limit(self):
        self.downloads.mkdir()
        cache = self.downloads / "tag-f44.txt"
        cache.write_text("Fedora-Games-Live-44-20260424.n.0 f44 user\n"
                         "hello-1-1 f44 user\n")
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        output = "hello-2-1 f44 user\nhello-extra-2-1 f44 user\n"
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod, "run", return_value=(0, output)) as command:
            result, stdout, stderr = self.list_output("-p", "hello", "-n", "1")
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "hello-2-1\n")
        self.assertIn("RPM build-type filter missing", stderr)
        self.assertIn("--type=rpm", command.call_args.args[0])
        self.assertEqual(cache.read_text(), mod.RPM_TAG_CACHE_HEADER + output)

    def test_failed_filtered_query_preserves_existing_cache(self):
        self.downloads.mkdir()
        cache = self.downloads / "tag-f44.txt"
        for content, options in (
                ("Fedora-IoT-32-20191107.0 f44 user\n", []),
                (mod.RPM_TAG_CACHE_HEADER + "hello-1-1 f44 user\n", ["--refresh"])):
            with self.subTest(options=options):
                cache.write_text(content)
                self.argv = ["-d", str(self.downloads), "--no-run-log"]
                with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                        patch.object(mod, "run", return_value=(1, "hub unavailable")):
                    result, stdout, stderr = self.list_output(*options)
                self.assertEqual(result, 2)
                self.assertEqual(stdout, "")
                self.assertIn("--type=rpm", stderr)
                self.assertIn("hub unavailable", stderr)
                self.assertEqual(cache.read_text(), content)

    def test_recovery_drops_image_failures_from_legacy_cache(self):
        self.downloads.mkdir()
        cache = self.downloads / "tag-f44.txt"
        image = "Fedora-Games-Live-44-20260424.n.0"
        records = [{"nvr": image, "status": "fetch-fail"},
                   {"nvr": "hello-1-1", "status": "test-fail"}]
        (self.downloads / "journal.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records))
        mod.write_run_manifest(self.downloads / "run.json",
                               [image, "hello-1-1"], "old-run")
        manifest = (self.downloads / "run.json").read_text()
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                cache.write_text(f"{image}\nhello-1-1\n")
                with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                        patch.object(mod, "run", return_value=(0, "hello-1-1 f44 user\n")):
                    result, stdout, _ = self.list_output(mode)
                self.assertEqual(result, 0)
                self.assertEqual(stdout, "hello-1-1\n")
                self.assertEqual((self.downloads / "run.json").read_text(), manifest)

    def test_explicit_file_keeps_supplied_builds_without_querying_koji(self):
        self.listfile.write_text("Fedora-IoT-32-20191107.0\nhello-1-1\n")
        with patch.object(mod, "run") as command, \
                patch.object(mod.shutil, "which", return_value=None):
            result, stdout, _ = self.list_output()
        self.assertEqual(result, 0)
        self.assertEqual(stdout, self.listfile.read_text())
        command.assert_not_called()
        self.assertFalse((self.downloads / "tag-f44.txt").exists())

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


class PackageMetadataTests(unittest.TestCase):
    def test_queries_resolved_executable_and_keeps_copr_build_details(self):
        rpm_info = ("Name        : tarpm\nEpoch       : 1\nVersion     : 0.1\n"
                    "Release     : 20261022.gitabcdef.fc44\nArchitecture: x86_64\n"
                    "Build Date  : Thu Oct 22 10:00:00 2026\n"
                    "Source RPM  : tarpm-0.1-20261022.gitabcdef.fc44.src.rpm\n")
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "tarpm-real"
            binary.touch()
            link = Path(directory) / "tarpm"
            link.symlink_to(binary)
            with patch.object(mod.shutil, "which", return_value=str(link)), \
                    patch.object(mod.subprocess, "run", return_value=Mock(
                        returncode=0, stdout=rpm_info, stderr="")) as query:
                info = mod.tarpm_package_info()
            self.assertEqual(query.call_args.args[0],
                             ["rpm", "-qi", "--file", str(binary.resolve())])
            self.assertEqual(query.call_args.kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(query.call_args.kwargs["timeout"], 5)
            self.assertEqual(info["executable"], str(link))
            self.assertEqual(info["rpm_info"], rpm_info.strip())
            self.assertEqual(mod.package_label(info),
                             "tarpm-1:0.1-20261022.gitabcdef.fc44.x86_64")
            self.assertIsNone(info["error"])

    def test_metadata_failures_are_nonfatal_and_explained(self):
        for failure in (FileNotFoundError("rpm missing"), PermissionError("denied"),
                        subprocess.TimeoutExpired(["rpm"], 5)):
            with self.subTest(failure=failure), \
                    patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                    patch.object(mod.subprocess, "run", side_effect=failure):
                info = mod.tarpm_package_info()
                self.assertIn("RPM metadata unavailable", info["error"])
                self.assertEqual(mod.package_label(info), "unknown")
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod.subprocess, "run", return_value=Mock(
                    returncode=1, stdout="file is not owned by any package", stderr="")):
            info = mod.tarpm_package_info()
            self.assertIn("not owned", info["error"])
        with patch.object(mod.shutil, "which", return_value=None), \
                patch.object(mod.subprocess, "run") as query:
            self.assertIn("not found", mod.tarpm_package_info()["error"])
            query.assert_not_called()

    def test_incomplete_metadata_retains_raw_output_and_avoids_false_identity(self):
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod.subprocess, "run", return_value=Mock(
                    returncode=0, stdout="Name : tarpm\n", stderr="")):
            info = mod.tarpm_package_info()
        self.assertIn("incomplete", info["error"])
        self.assertEqual(info["rpm_info"], "Name : tarpm")
        self.assertEqual(mod.package_label(info), "unknown")


class ReportContentTests(RunnerTestCase):
    def test_text_and_json_share_session_metadata_captured_before_tests(self):
        events = []
        info = {"executable": "/usr/bin/tarpm", "package": {
            "name": "tarpm", "version": "0.1", "release": "gitabcdef.fc44",
            "arch": "x86_64"}, "rpm_info": "Build Date : before testing", "error": None}

        def package():
            events.append("metadata")
            return info

        def process(nvr, args, counter):
            events.append("test")
            return mod.FetchResult(nvr, True), [
                mod.TestResult("demo-1-1.src.rpm", nvr, "src", True),
                mod.TestResult("demo-1-1.x86_64.rpm", nvr, "x86_64", True,
                               warning="content-diff")]

        json_path = self.root / "results.json"
        with patch.object(mod.shutil, "which", return_value="/usr/bin/tarpm"), \
                patch.object(mod, "tarpm_package_info", side_effect=package), \
                patch.object(mod, "process_build", side_effect=process):
            self.assertEqual(self.run_main("--json", str(json_path), "-j", "2"), 0)
        self.assertEqual(events, ["metadata", "test"])
        data = json.loads(json_path.read_text())
        session = data["session"]
        report = (self.root / "report.txt").read_text()
        self.assertEqual(session["tarpm"], info)
        self.assertIn("tarpm-0.1-gitabcdef.fc44.x86_64", report)
        self.assertIn("Build Date : before testing", report)
        self.assertIn("started:   " + session["started"], report)
        self.assertIn("generated: " + session["finished"], report)
        self.assertIn("runtime:   " + mod.fmt_duration(session["runtime_seconds"]), report)
        self.assertGreaterEqual(session["runtime_seconds"], 0)
        self.assertEqual(session["status"], "passed")
        self.assertEqual(session["command"][2:], self.argv + ["--json", str(json_path), "-j", "2"])
        self.assertEqual(session["settings"]["jobs"], 2)
        self.assertEqual(data["summary"]["test"], {
            "total": 2, "ok": 2, "failed": 0, "warnings": 1, "clean_passed": 1})
        self.assertEqual(data["failure_types"], {"fetch": [], "test": []})

    def test_ranked_groups_precede_logs_and_json_matches_text(self):
        fetch = [mod.FetchResult("demo-1-1", False, 1, signature="download failed")]
        tests = [mod.TestResult(f"demo-1-1.{arch}.rpm", "demo-1-1", arch,
                               False, "repack", 1, signature="write payload failed")
                 for arch in ("src", "x86_64", "noarch")]
        tests.extend([
            mod.TestResult("other-1-1.src.rpm", "other-1-1", "src", False,
                           "unpack", 2, signature="bad header"),
            # The same diagnostic with a different exit code remains distinct.
            mod.TestResult("third-1-1.src.rpm", "third-1-1", "src", False,
                           "repack", 2, signature="write payload failed")])
        args = mod.parse_args(self.argv)
        report = mod.write_report(args, fetch, tests, 3665).read_text()
        self.assertIn("runtime:   1h 01m 05s", report)
        self.assertLess(report.index("=== TEST FAILURES BY TYPE ==="),
                        report.index("=== FETCH FAILURES ==="))
        self.assertIn("3x (60.0% of test failures)", report)
        self.assertIn("1x (100.0% of fetch failures)", report)
        path = self.root / "groups.json"
        mod.write_json(path, args, fetch, tests)
        groups = json.loads(path.read_text())["failure_types"]
        self.assertEqual(groups["test"][0]["count"], 3)
        self.assertEqual(groups["test"][0]["percent_of_failures"], 60.0)
        self.assertEqual(len(groups["test"]), 3)
        self.assertEqual(groups["test"][0]["examples"], sorted(
            ["demo-1-1.src", "demo-1-1.x86_64", "demo-1-1.noarch"]))
        self.assertEqual(groups["fetch"][0]["count"], 1)

    def test_exception_and_empty_output_groups_are_actionable(self):
        tests = [mod.TestResult("demo-1-1.src.rpm", "demo-1-1", "src", False,
                                "exception", error="OSError: failed at /tmp/a/file"),
                 mod.TestResult("other-1-1.src.rpm", "other-1-1", "src", False,
                                "exception", error="OSError: failed at /tmp/b/file"),
                 mod.TestResult("third-1-1.src.rpm", "third-1-1", "src", False,
                                "unpack", 1)]
        groups = mod.failure_summary(None, tests, "test")
        self.assertEqual(groups[0]["signature"], "OSError: failed at <path>")
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[1]["signature"], "(no diagnostic output)")
        args = mod.parse_args(self.argv)
        mod.write_failure_lists(args, None, tests)
        self.assertIn("OSError: failed at <path>",
                      (self.downloads / "logs/failed-index.tsv").read_text())

    def test_fetch_only_skips_rpm_lookup_and_has_timing_and_empty_test_summary(self):
        path = self.root / "fetch.json"
        with patch.object(mod, "tarpm_package_info") as package, \
                patch.object(mod, "fetch_one", return_value=mod.FetchResult("demo-1-1", True)):
            self.assertEqual(self.run_main("--no-test", "--json", str(path)), 0)
        package.assert_not_called()
        data = json.loads(path.read_text())
        self.assertIsNone(data["session"]["tarpm"])
        self.assertEqual(data["session"]["mode"], "fetch-only")
        self.assertNotIn("test", data["summary"])


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


class TimingTests(RunnerTestCase):
    def test_stage_times_include_the_failing_command_and_skip_unreached_stages(self):
        stages = ["unpack", "repack", "unpack-repacked", "list-original", "list-repacked"]
        for failure in [None] + stages:
            with self.subTest(failure=failure):
                args = mod.parse_args(self.argv + ["--keep"])
                rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
                rpm.parent.mkdir(parents=True, exist_ok=True)
                rpm.write_text("rpm")
                (self.downloads / "logs").mkdir(exist_ok=True)
                elapsed = 100.0
                visited = []

                def command(cmd, **kwargs):
                    nonlocal elapsed
                    name = stages[len(visited)]
                    visited.append(name)
                    elapsed += len(visited) * 2
                    return (1, "tarpm: failed") if name == failure else (0, "same listing\n")

                with patch.object(mod.time, "monotonic", side_effect=lambda: elapsed), \
                        patch.object(mod, "run", side_effect=command), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = mod.test_one(rpm, args)
                expected = stages if failure is None else stages[:stages.index(failure) + 1]
                self.assertEqual(list(result.stage_seconds), expected)
                self.assertEqual(result.stage_seconds,
                                 {name: (i + 1) * 2 for i, name in enumerate(expected)})
                self.assertEqual(result.duration_seconds, elapsed - 100)
                self.assertEqual(result.ok, failure is None)
                self.assertEqual(result.stage, failure)
                self.assertIn("timings: unpack=2.000s", result.log.read_text())

    def test_fetch_time_and_summary_show_measured_failures_without_inventing_exception_times(self):
        args = mod.parse_args(self.argv)
        (self.downloads / "logs").mkdir(parents=True)
        with patch.object(mod, "run", return_value=(1, "failed download")), \
                patch.object(mod.time, "monotonic", side_effect=[10, 14.5]), \
                contextlib.redirect_stdout(io.StringIO()):
            fetched = mod.fetch_one("demo-1-1", args)
        self.assertFalse(fetched.ok)
        self.assertEqual(fetched.duration_seconds, 4.5)
        fetch = [fetched, mod.FetchResult("unknown-1-1", False, error="worker exception")]
        tests = [mod.TestResult(f"demo-1-1.{i}.rpm", "demo-1-1", str(i), True,
                               duration_seconds=i, stage_seconds={"repack": i, "unpack": 0.012})
                 for i in range(1, 7)]
        summary = mod.timing_summary(fetch, tests)
        self.assertEqual(summary['fetch']['count'], 1)
        self.assertEqual(summary['fetch']['total_seconds'], 4.5)
        self.assertEqual(summary['repack']['total_seconds'], 21)
        self.assertEqual(summary['repack']['mean_seconds'], 3.5)
        self.assertEqual(summary['repack']['max_seconds'], 6)
        self.assertEqual(len(summary['repack']['slowest']), 5)
        self.assertEqual(summary['repack']['slowest'][0]['name'], "demo-1-1.6")
        report = mod.write_report(args, fetch, tests, 10).read_text()
        self.assertIn("totals sum workers and may exceed session runtime", report)
        self.assertIn("repack: 6 command(s), total 21.0s", report)
        self.assertIn("mean 12.0ms", report)
        path = self.root / "timings.json"
        mod.write_json(path, args, fetch, tests)
        data = json.loads(path.read_text())
        self.assertEqual(data['timings'], summary)
        self.assertEqual(data['test'][0]['stage_seconds'], {"repack": 1, "unpack": 0.012})


class ComparisonTests(RunnerTestCase):
    def baseline(self, fetch=(), test=()):
        path = self.root / "baseline.json"
        # Deliberately use the older schema without session metadata or timings.
        path.write_text(json.dumps({
            "generated": "2026-10-21T10:00:00+00:00", "tag": "f44",
            "fetch": [mod.dataclasses.asdict(row) for row in fetch],
            "test": [mod.dataclasses.asdict(row) for row in test]}))
        return path

    def test_exact_identities_distinguish_regressions_resolutions_and_coverage(self):
        def test(name, ok, signature="", stage="repack", prefix="/previous/rpms"):
            return mod.TestResult(f"{prefix}/{name}-1-1/{name}-1-1.src.rpm",
                                  f"{name}-1-1", "src", ok,
                                  None if ok else stage, None if ok else 1, signature=signature)

        previous = [test("regressed", True), test("resolved", False, "old error"),
                    test("persistent", False, "same error"),
                    test("changed", False, "old error"), test("omitted", False, "old error"),
                    test("rebuilt", False, "old error")]
        current = [test("regressed", False, "new error", prefix="/new/rpms"),
                   test("resolved", True), test("persistent", False, "same error"),
                   test("changed", False, "new error", stage="unpack"),
                   test("new", False, "new error"),
                   mod.TestResult("rebuilt-2-1.src.rpm", "rebuilt-2-1", "src", True)]
        baseline = mod.read_comparison_report(self.baseline(test=previous))
        comparison = mod.compare_results(baseline, None, current)
        changes = comparison['test']
        self.assertEqual(changes['rechecked'], 4)
        self.assertEqual(changes['new_cases'], 2)
        self.assertEqual(changes['not_rechecked'], 2)
        self.assertEqual([row['nvr'] for row in changes['regressions']], ["regressed-1-1"])
        self.assertEqual([row['nvr'] for row in changes['resolved']], ["resolved-1-1"])
        self.assertEqual(len(changes['persistent']), 2)
        self.assertEqual([row['nvr'] for row in changes['changed_failures']], ["changed-1-1"])
        self.assertEqual([row['nvr'] for row in changes['first_seen_failures']], ["new-1-1"])
        args = mod.parse_args(self.argv)
        report = mod.write_report(args, None, current, 5, comparison=comparison).read_text()
        self.assertIn("REGRESSION regressed-1-1.src.rpm", report)
        self.assertIn("RESOLVED resolved-1-1.src.rpm", report)
        self.assertIn("previously: stage repack, exit 1: old error", report)
        self.assertNotIn("RESOLVED omitted", report)
        self.assertLess(report.index("=== TEST FAILURES BY TYPE ==="),
                        report.index("=== COMPARISON DETAILS ==="))
        path = self.root / "comparison.json"
        mod.write_json(path, args, None, current, comparison=comparison)
        self.assertEqual(json.loads(path.read_text())['comparison'], comparison)

    def test_fetch_resolution_does_not_resolve_tests_that_were_not_run(self):
        path = self.baseline(
            fetch=[mod.FetchResult("demo-1-1", False, 1, signature="network error")],
            test=[mod.TestResult("demo-1-1.src.rpm", "demo-1-1", "src", False,
                                 "repack", 1, signature="payload error")])
        changes = mod.compare_results(mod.read_comparison_report(path),
                                      [mod.FetchResult("demo-1-1", True)], None)
        self.assertEqual(len(changes['fetch']['resolved']), 1)
        self.assertEqual(changes['test']['resolved'], [])
        self.assertEqual(changes['test']['not_rechecked'], 1)

    def test_invalid_baselines_fail_before_work_or_output_changes(self):
        path = self.root / "invalid.json"
        invalid = ["not JSON", "[]", '{}', '{"fetch": [], "test": null}',
                   '{"fetch": [{"nvr": "demo-1-1", "ok": "false"}], "test": []}',
                   '{"fetch": [], "test": [{"nvr": "demo-1-1", "ok": true}]}',
                   '{"fetch": [], "test": [], "session": {"tarpm": {"package": null}}}',
                   json.dumps({"fetch": [{"nvr": "demo-1-1", "ok": True}] * 2, "test": []})]
        for contents in invalid:
            with self.subTest(contents=contents):
                path.write_text(contents)
                with patch.object(mod, "fetch_one") as fetch, \
                        patch.object(mod, "process_build") as process:
                    self.assertEqual(self.run_main("--no-test", "--compare", str(path)), 2)
                fetch.assert_not_called()
                process.assert_not_called()
                self.assertFalse(self.downloads.exists())
                self.assertFalse((self.root / "report.txt").exists())
        with self.assertRaises(mod.UsageError):
            mod.read_comparison_report(self.root / "missing.json")

    def test_main_loads_baseline_before_overwriting_the_same_json_path(self):
        path = self.baseline(fetch=[mod.FetchResult("demo-1-1", False, 1, signature="network error")])
        with patch.object(mod, "fetch_one", return_value=mod.FetchResult("demo-1-1", True)):
            self.assertEqual(self.run_main("--no-test", "--compare", str(path),
                                           "--json", str(path)), 0)
        data = json.loads(path.read_text())
        self.assertEqual(len(data['comparison']['fetch']['resolved']), 1)
        self.assertEqual(data['comparison']['generated'], "2026-10-21T10:00:00+00:00")
        self.assertIn("1 resolved", (self.root / "report.txt").read_text())

    def test_current_failure_still_exits_one_when_comparison_is_text_only(self):
        path = self.baseline(fetch=[mod.FetchResult("demo-1-1", True)])
        failed = mod.FetchResult("demo-1-1", False, 1, signature="download failed")
        with patch.object(mod, "fetch_one", return_value=failed):
            self.assertEqual(self.run_main("--no-test", "--compare", str(path)), 1)
        report = (self.root / "report.txt").read_text()
        self.assertIn("fetch REGRESSION demo-1-1", report)
        self.assertIn("1 regressions", report)

    def test_list_cannot_silently_ignore_comparison(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            mod.parse_args(["--list", "--compare", "baseline.json"])
        self.assertEqual(exc.exception.code, 2)


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


class FetchProgressTests(RunnerTestCase):
    @staticmethod
    def bar(percent):
        return f"[{'=' * 21}{' ' * 15}] {percent:>4} 913.00 MiB / 1.46 GiB\r"

    def test_latest_native_bar_in_bytes_or_text(self):
        output = ("Downloading [1/2]: demo.rpm\n" + self.bar("12%")
                  + self.bar("61%"))
        for snapshot in (output, output.encode(), output.replace("\r", "\n"),
                         output.encode().replace(b"demo", b"\xffdemo")):
            with self.subTest(snapshot=snapshot):
                progress = mod.koji_download_progress(snapshot)
                self.assertEqual(progress.percent, 61)
                self.assertEqual(progress.position, (1, 2))
                self.assertTrue(progress.filename.endswith("demo.rpm"))
                self.assertEqual(progress.size_text, "913.00 MiB / 1.46 GiB")

    def test_new_rpm_does_not_inherit_completed_percentage(self):
        completed = "Downloading [1/2]: first.rpm\n" + self.bar("100%")
        for heading in ("Downloading [2/2]: next.rpm\n", "Downloading: next.rpm\n",
                        "Downloading [2/"):
            with self.subTest(heading=heading):
                output = completed + heading
                progress = mod.koji_download_progress(output)
                if heading.endswith("\n"):
                    self.assertEqual(progress.filename, "next.rpm")
                    self.assertIsNone(progress.percent)
                    self.assertIsNone(progress.size_text)
                    self.assertEqual(mod.koji_download_progress(
                        output + self.bar("3%")).percent, 3)
                else:
                    self.assertIsNone(progress)

    def test_missing_unknown_or_invalid_progress_has_no_percentage(self):
        for output in (None, b"", "retry 61% complete", self.bar("61%"),
                       "Downloading [2/1]: demo.rpm\n" + self.bar("61%"),
                       "Downloading: ../demo.rpm\n" + self.bar("61%")):
            with self.subTest(output=output):
                self.assertIsNone(mod.koji_download_progress(output))
        for bar in ("", self.bar("101%"), self.bar("61%") + self.bar("???%")):
            with self.subTest(bar=bar):
                progress = mod.koji_download_progress("Downloading: demo.rpm\n" + bar)
                self.assertIsNone(progress.percent)

    def test_partial_bar_preserves_last_complete_current_rpm_bar(self):
        output = "Downloading: demo.rpm\n" + self.bar("61%") + "[======"
        self.assertEqual(mod.koji_download_progress(output).percent, 61)

    def test_sizes_fall_back_to_current_rpm_or_explicit_build_total(self):
        self.downloads.mkdir()
        (self.downloads / "first.rpm").write_bytes(b"x" * 2048)
        (self.downloads / "next.rpm").write_bytes(b"x" * 1024)
        output = "Downloading [2/2]: next.rpm\n"
        self.assertEqual(mod.fetch_progress_label("demo-1-1", output),
                         "fetching next.rpm (RPM 2/2, 100.0%)")
        self.assertEqual(mod.fetch_progress_suffix(self.downloads, output),
                         ", 1 KiB so far")
        self.assertEqual(mod.fetch_progress_label("demo-1-1", None), "fetching demo-1-1")
        self.assertEqual(mod.fetch_progress_suffix(self.downloads, None),
                         ", 3 KiB build total so far")

    def test_unknown_percentage_retains_native_rpm_size(self):
        output = "Downloading: demo.rpm\n" + self.bar("???%")
        self.assertEqual(mod.fetch_progress_suffix(self.downloads, output),
                         ", 913.00 MiB / 1.46 GiB so far")
        # Clients may emit only downloaded bytes, without the expected total.
        output = output.replace(" / 1.46 GiB", "")
        self.assertEqual(mod.fetch_progress_suffix(self.downloads, output),
                         ", 913.00 MiB so far")
        self.assertEqual(mod.fetch_progress_label("demo-1-1", output),
                         "fetching demo.rpm")
        # Batch position is available even when the RPM's byte progress isn't.
        output = output.replace("Downloading:", "Downloading [2/3]:")
        self.assertEqual(mod.fetch_progress_label("demo-1-1", output),
                         "fetching demo.rpm (RPM 2/3, 66.7%)")
        self.assertEqual(mod.fetch_progress_suffix(self.downloads, output),
                         ", 913.00 MiB so far")

    def test_fetch_heartbeat_uses_native_progress_and_keeps_full_output(self):
        args = mod.parse_args(self.argv + ["--keep", "--heartbeat", "1"])
        (self.downloads / "logs").mkdir(parents=True)
        first = "Downloading [1/2]: first.rpm\n" + self.bar("61%")
        next_rpm = first + self.bar("100%") + "Downloading [2/2]: next.rpm\n"
        final = next_rpm + self.bar("21%") + self.bar("100%") + "\n"
        cmd = ["koji", "download-build", "demo-1-1"]
        proc = Mock()
        proc.returncode = 0
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd, 1, output=first.encode()),
            subprocess.TimeoutExpired(cmd, 1, output=next_rpm.encode()),
            subprocess.TimeoutExpired(cmd, 1, output=next_rpm + self.bar("21%")),
            (final, None),
        ]
        with patch.object(mod.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(mod, "_file_bytes", return_value=1024), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = mod.fetch_one("demo-1-1", args, mod.Counter(2))
        self.assertTrue(result.ok)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], cmd)
        beats = [line for line in stdout.getvalue().splitlines() if "still fetching" in line]
        self.assertEqual(len(beats), 3)
        self.assertIn("fetching first.rpm (RPM 1/2, 50.0%) (build 1/2)", beats[0])
        self.assertIn(", 913.00 MiB / 1.46 GiB, 61% so far)", beats[0])
        self.assertIn("fetching next.rpm (RPM 2/2, 100.0%) (build 1/2)", beats[1])
        self.assertIn(", 1 KiB so far)", beats[1])
        self.assertNotIn("%", beats[1].rsplit("(", 1)[-1])
        self.assertIn("fetching next.rpm (RPM 2/2, 100.0%) (build 1/2)", beats[2])
        self.assertIn(", 913.00 MiB / 1.46 GiB, 21% so far)", beats[2])
        self.assertEqual(result.log.read_bytes().decode(), final)


class FetchRecoveryTests(RunnerTestCase):
    def test_failed_fetch_removes_empty_directory_and_keeps_failure_records(self):
        for options in (["--no-test"], []):
            with self.subTest(options=options), \
                    patch.object(mod.shutil, "which", return_value="/fake/tool"), \
                    patch.object(mod, "run", return_value=(7, "download failed")):
                self.assertEqual(self.run_main(*options), 1)
                self.assertFalse((self.downloads / "demo-1-1").exists())
                logfile = self.downloads / "logs/demo-1-1.fetch.log"
                self.assertEqual(logfile.read_text(), "download failed")
                record = mod.read_journal(self.downloads / "journal.jsonl")["demo-1-1"]
                self.assertEqual(record["status"], "fetch-fail")
                self.assertEqual(record["exit_code"], 7)
                self.assertEqual(record["signature"], "download failed")

        # Recovery uses the journal, even after the empty directory is gone.
        with patch.object(mod.shutil, "which", return_value="/fake/tool"), \
                patch.object(mod, "run", return_value=(0, "downloaded")) as command:
            self.assertEqual(self.run_main("--retry-failed"), 0)
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0][-1], "demo-1-1")

    def test_failed_fetch_keep_retains_empty_directory(self):
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod, "run", return_value=(1, "download failed")):
            self.assertEqual(self.run_main("--no-test", "--keep"), 1)
        bdir = self.downloads / "demo-1-1"
        self.assertTrue(bdir.is_dir())
        self.assertEqual(list(bdir.iterdir()), [])

    def test_failed_fetch_preserves_all_directory_contents(self):
        args = mod.parse_args(self.argv)
        (self.downloads / "logs").mkdir(parents=True)
        bdir = self.downloads / "demo-1-1"
        bdir.mkdir()
        for name in ("partial.rpm", ".download", "nested"):
            with self.subTest(name=name):
                item = bdir / name
                if name == "nested":
                    item.mkdir()
                else:
                    item.write_text("partial download")
                with patch.object(mod, "run", return_value=(1, "download failed")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = mod.fetch_one("demo-1-1", args)
                self.assertFalse(result.ok)
                self.assertTrue(item.exists())
                if item.is_dir():
                    item.rmdir()
                else:
                    self.assertEqual(item.read_text(), "partial download")
                    item.unlink()

    def test_failed_fetch_cleanup_error_preserves_original_failure(self):
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod, "run", return_value=(7, "download failed")), \
                patch.object(Path, "rmdir", side_effect=PermissionError("denied")):
            self.assertEqual(self.run_main("--no-test"), 1)
        self.assertTrue((self.downloads / "demo-1-1").is_dir())
        record = mod.read_journal(self.downloads / "journal.jsonl")["demo-1-1"]
        self.assertEqual(record["exit_code"], 7)
        self.assertEqual(record["signature"], "download failed")

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


class TestProgressTests(RunnerTestCase):
    def test_tree_sizes_ignore_links_outside_tree_and_count_hardlinks_once(self):
        tree = self.root / "tree"
        nested = tree / "payload/nested"
        nested.mkdir(parents=True)
        (tree / "header.json").write_bytes(b"x" * 17)
        source = nested / "data"
        source.write_bytes(b"x" * 31)
        os.link(source, nested / "hardlink")
        (tree / "file-link").symlink_to(source)
        (tree / "dir-link").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(tree / "pipe")
        self.assertEqual(mod._tree_bytes(tree), 48)
        self.assertEqual(mod._tree_bytes(self.root / "missing"), 0)
        self.assertEqual(mod._file_bytes(source), 31)
        for path in (tree, tree / "missing", tree / "file-link", tree / "pipe"):
            with self.subTest(path=path):
                self.assertIsNone(mod._file_bytes(path))

    def test_payload_probe_ignores_unrelated_and_disappearing_descriptors(self):
        payload = self.root / "payload"
        payload.write_bytes(b"x" * 1024)
        fds = [self.root / str(i) for i in range(4)]
        for fd in fds[:3]:
            fd.symlink_to(payload)

        def target(fd):
            if fd == fds[3]:
                raise FileNotFoundError("descriptor closed")
            return ("/memfd:demo-payload.321 (deleted)" if fd in fds[:2]
                    else "/memfd:unrelated (deleted)")

        with patch.object(mod.Path, "iterdir", return_value=iter(fds)), \
                patch.object(mod.os, "readlink", side_effect=target):
            self.assertEqual(mod._tarpm_payload_bytes(321), 1024)

    def test_payload_probe_tolerates_unavailable_procfs(self):
        with patch.object(mod.Path, "iterdir", side_effect=PermissionError):
            self.assertIsNone(mod._tarpm_payload_bytes(321))
            self.assertIsNone(mod._tarpm_payload_bytes(None))

    @unittest.skipUnless(hasattr(os, "memfd_create") and Path("/proc/self/fd").is_dir(),
                         "Linux memfd and procfs required")
    def test_real_anonymous_payload_growth(self):
        fd = os.memfd_create(f"demo-payload.{os.getpid()}", os.MFD_CLOEXEC)
        self.addCleanup(os.close, fd)
        duplicate = os.dup(fd)
        self.addCleanup(os.close, duplicate)
        os.write(fd, b"x" * 1024)
        self.assertEqual(mod._tarpm_payload_bytes(os.getpid()), 1024)
        os.write(fd, b"x" * 1024)
        self.assertEqual(mod._tarpm_payload_bytes(os.getpid()), 2048)

    def test_repack_falls_back_to_rpm_size_and_cached_input_sizes(self):
        repacked = self.root / "repacked.rpm"
        with patch.object(mod, "_tarpm_payload_bytes", return_value=None):
            self.assertEqual(mod.repack_progress(repacked, 321, 2048, 4096),
                             ("", "; RPM pending; input 4 KiB; original 2 KiB"))
            repacked.write_bytes(b"x" * 1024)
            self.assertEqual(mod.repack_progress(repacked, 321, 2048, 4096),
                             (" ~50.0%", "; RPM 1/2 KiB; input 4 KiB"))
            self.assertEqual(mod.repack_progress(repacked, 321, None, 4096),
                             ("", "; RPM 1 KiB; input 4 KiB"))

    def test_repack_estimates_allow_growth_past_original_and_unknown_totals(self):
        repacked = self.root / "repacked.rpm"
        for payload, expected, estimate, size in (
                (0, 2048, " ~0.0%", "0 B/2 KiB"),
                (3072, 2048, " ~150.0%", "3/2 KiB"),
                (1024, 0, "", "1 KiB/0 B"),
                (1024, None, "", "1 KiB")):
            with self.subTest(payload=payload, expected=expected), \
                    patch.object(mod, "_tarpm_payload_bytes", return_value=payload):
                self.assertEqual(mod.repack_progress(repacked, 321, expected, 4096),
                                 (estimate, f"; payload {size}; RPM pending; input 4 KiB"))

    def test_only_heartbeats_show_stage_sizes(self):
        args = mod.parse_args(self.argv + ["--keep", "--heartbeat", "1"])
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_bytes(b"x" * 2048)
        (self.downloads / "logs").mkdir()
        work = rpm.parent / ".tarpm-demo-1-1.noarch"
        repacked = work / "demo-1-1.noarch-repacked.rpm"

        def extract(directory):
            directory.mkdir(parents=True)
            (directory / "data").write_bytes(b"x" * 4096)

        def child(output="", write=None, heartbeats=0):
            proc = Mock(pid=321, returncode=0)
            pending = write
            remaining = heartbeats

            def communicate(**kwargs):
                nonlocal pending, remaining
                if pending:
                    pending()
                    pending = None
                if remaining:
                    remaining -= 1
                    raise subprocess.TimeoutExpired(["tarpm"], 1)
                return output, None

            proc.communicate.side_effect = communicate
            return proc

        repack = child(write=lambda: repacked.write_bytes(b"x" * 1536), heartbeats=2)
        children = [child(write=lambda: extract(work / "ext/payload"), heartbeats=1), repack,
                    child(write=lambda: extract(work / "ext2/payload"), heartbeats=1),
                    child("same listing\n", heartbeats=1),
                    child("same listing\n", heartbeats=1)]
        with patch.object(mod.subprocess, "Popen", side_effect=children) as popen, \
                patch.object(mod, "_tarpm_payload_bytes",
                             side_effect=lambda pid: 1024 if pid == 321 else None), \
                patch.object(mod, "_tree_bytes", wraps=mod._tree_bytes) as tree_bytes, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = mod.test_one(rpm, args, progress=(1, 2))
        self.assertTrue(result.ok)
        self.assertIsNone(result.warning)
        self.assertEqual(popen.call_count, 5)
        starts = [line for line in stdout.getvalue().splitlines() if ": testing " in line]
        self.assertEqual(starts, [])
        beats = [line for line in stdout.getvalue().splitlines() if "still testing" in line]
        self.assertEqual(len(beats), 6)
        for beat in beats:
            self.assertIn("still testing demo-1-1.noarch (RPM 1/2, 50.0%) (", beat)
        self.assertIn("(unpack, ", beats[0])
        self.assertIn("; unpacked 4 KiB; RPM 2 KiB)", beats[0])
        self.assertNotIn("~", beats[0])
        for beat in beats[1:3]:
            self.assertIn("(repack ~50.0%, ", beat)
            self.assertIn("; payload 1/2 KiB; RPM 1.5 KiB; input 4 KiB)", beat)
        self.assertIn("(unpack-repacked ~100.0%, ", beats[3])
        self.assertIn("; unpacked 4/4 KiB; RPM 1.5 KiB)", beats[3])
        self.assertIn("(list-original, ", beats[4])
        self.assertIn("(list-repacked, ", beats[5])
        # The completed input is measured once after unpacking, not repeatedly
        # during repack (the earlier scan is the unpack heartbeat).
        self.assertEqual(sum(call.args == (work / "ext",)
                             for call in tree_bytes.call_args_list), 2)

    def test_reunpack_estimates_without_batch_counter_or_repack_heartbeat(self):
        args = mod.parse_args(self.argv + ["--keep", "--heartbeat", "1"])
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_bytes(b"x" * 2048)
        (self.downloads / "logs").mkdir()
        work = rpm.parent / ".tarpm-demo-1-1.noarch"
        details = []

        def command(cmd, **kwargs):
            if cmd[1] == "-x":
                directory = Path(cmd[-1])
                directory.mkdir()
                size = 4096 if directory.name == "ext" else 1024
                (directory / "data").write_bytes(b"x" * size)
                if directory.name == "ext2":
                    details.append(kwargs["detail_fn"](None, 321, 60))
            elif cmd[1] == "-c":
                Path(cmd[3]).write_bytes(b"x" * 2048)
            return 0, ""

        with patch.object(mod, "run", side_effect=command), \
                patch.object(mod, "_tree_bytes", wraps=mod._tree_bytes) as tree_bytes, \
                contextlib.redirect_stdout(io.StringIO()):
            result = mod.test_one(rpm, args)
        self.assertTrue(result.ok)
        self.assertEqual(details, ["unpack-repacked ~25.0%, 1m 00s; "
                                   "unpacked 1/4 KiB; RPM 2 KiB"])
        self.assertEqual(sum(call.args == (work / "ext",)
                             for call in tree_bytes.call_args_list), 1)

    def test_fast_tests_only_log_completion_and_do_not_scan_tree(self):
        args = mod.parse_args(self.argv)
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_text("rpm")
        (self.downloads / "logs").mkdir()
        with patch.object(mod, "run", side_effect=[(0, "")] * 5), \
                patch.object(mod, "_tree_bytes") as tree_bytes, \
                patch.object(mod, "_tarpm_payload_bytes") as payload_bytes, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = mod.test_one(rpm, args)
        self.assertTrue(result.ok)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("[test]   demo-1-1.noarch ok", lines[0])
        tree_bytes.assert_not_called()
        payload_bytes.assert_not_called()

    def test_failed_stage_does_not_announce_later_stages(self):
        args = mod.parse_args(self.argv + ["--heartbeat", "0"])
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_text("rpm")
        (self.downloads / "logs").mkdir()
        with patch.object(mod, "run", return_value=(1, "unpack failed")), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = mod.test_one(rpm, args)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "unpack")
        starts = [line for line in stdout.getvalue().splitlines() if ": testing " in line]
        self.assertEqual(starts, [])


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
        self.assertEqual(data["session"]["status"], "interrupted")
        self.assertTrue(data["session"]["interrupted"])
        self.assertEqual(data["session"]["selected_builds"], 1)
        self.assertGreaterEqual(data["session"]["runtime_seconds"], 0)
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
