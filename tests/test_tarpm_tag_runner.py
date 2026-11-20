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
        for extra in (["--resume"], ["-p", "hello"], ["--list"], ["-d", "other"]):
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


class CommandOutputTests(unittest.TestCase):
    def test_real_child_preserves_exit_status_with_invalid_utf8(self):
        for code in (0, 7):
            for heartbeat in (0, 1):
                with self.subTest(code=code, heartbeat=heartbeat):
                    rc, output = mod.run([
                        sys.executable, "-c",
                        "import os, sys; os.write(1, b'diagnostic\\xff\\n'); "
                        f"sys.exit({code})"], heartbeat=heartbeat)
                    self.assertEqual(rc, code)
                    self.assertEqual(output, "diagnostic\\xff\n")

    def test_binary_output_preserves_invalid_bytes_and_line_endings(self):
        for code in (0, 7):
            with self.subTest(code=code):
                rc, output = mod.run([
                    sys.executable, "-c",
                    "import os, sys; os.write(1, b'listing\\xff\\r\\n'); "
                    f"sys.exit({code})"], binary_output=True)
                self.assertEqual(rc, code)
                self.assertEqual(output, b"listing\xff\r\n")

    def test_missing_command_respects_binary_output(self):
        with patch.object(mod.subprocess, "Popen", side_effect=FileNotFoundError):
            rc, output = mod.run(["missing-tarpm"], binary_output=True)
        self.assertEqual(rc, 127)
        self.assertEqual(output, b"command not found: missing-tarpm")


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ttr-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / "rpms"
        self.seed_tag("demo-1-1\n")
        self.argv = ["-d", str(self.downloads),
                     "-r", str(self.root / "report.txt"), "--no-run-log"]
        journal = mod.Journal()
        self.enter_patch = patch.object(mod, "JOURNAL", journal)
        self.enter_patch.start()
        self.addCleanup(self.enter_patch.stop)
        self.addCleanup(lambda: journal._fh.close() if journal._fh else None)
        # Main-loop command stubs must not depend on installed clients.
        which_patch = patch.object(mod.shutil, "which",
                                   side_effect=lambda name: f"/fake/{name}")
        which_patch.start()
        self.addCleanup(which_patch.stop)
        # Main-loop tests must not query the host's RPM database.
        package_patch = patch.object(mod, "tarpm_package_info", return_value={
            "executable": "/fake/tarpm", "package": {}, "rpm_info": None,
            "error": "RPM metadata unavailable in unit tests"})
        package_patch.start()
        self.addCleanup(package_patch.stop)

    def seed_tag(self, text):
        self.downloads.mkdir(exist_ok=True)
        (self.downloads / "tag-f44.txt").write_text(
            mod.RPM_TAG_CACHE_HEADER + text, encoding="utf-8")

    def run_main(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return mod.main(self.argv + list(extra))

    def passed_build(self, nvr, args, counter):
        return (mod.FetchResult(nvr, True),
                [mod.TestResult(rpm=nvr + ".noarch.rpm", nvr=nvr,
                                arch="noarch", ok=True)])

    def write_manifest(self, path, nvrs, run_id):
        return mod.write_run_manifest(path, nvrs, run_id, context={
            "started": "2026-10-21T10:00:00+00:00", "tag": "f44",
            "command": ["tarpm-tag-runner.py"],
            "settings": {"architectures": [], "include_source": True},
        })

    def build_record(self, nvr, status, run_id="active"):
        fetch = mod.FetchResult(nvr, status != "fetch-fail")
        tests = []
        if status in ("pass", "warn", "test-fail"):
            tests.append(mod.TestResult(
                f"{nvr}.noarch.rpm", nvr, "noarch", status != "test-fail",
                stage="repack" if status == "test-fail" else None,
                warning="content-diff" if status == "warn" else None))
        attempt = {"id": f"{run_id}-{nvr}", "started": "2026-10-21T10:00:00+00:00",
                   "selected_builds": 1, "tag": "f44", "tarpm": "unknown" if tests else None,
                   "settings": {"architectures": [], "include_source": True}}
        return mod.journal_record(nvr, fetch, tests, run_id, attempt)

    def write_journal(self, records):
        self.downloads.mkdir(parents=True, exist_ok=True)
        path = self.downloads / "journal.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return path


class MissingKojiTests(RunnerTestCase):
    def test_missing_koji_preserves_run_state_for_cached_tag(self):
        manifest = self.downloads / "run.json"
        self.write_manifest(manifest, ["demo-1-1"], "previous-run")
        before_manifest = manifest.read_bytes()
        journal = self.write_journal([self.build_record("demo-1-1", "test-fail", "previous-run")])
        before_journal = journal.read_bytes()
        for options in ([], ["--resume"], ["--retry-failed"]):
            with self.subTest(options=options), \
                    patch.object(mod.shutil, "which", return_value=None), \
                    patch.object(mod, "run") as command, \
                    patch.object(mod, "process_build") as process, \
                    patch.object(mod.JOURNAL, "open") as open_journal:
                self.assertEqual(self.run_main(*options), 2)
            command.assert_not_called()
            process.assert_not_called()
            open_journal.assert_not_called()
            self.assertEqual(manifest.read_bytes(), before_manifest)
            self.assertEqual(journal.read_bytes(), before_journal)

    def test_empty_recovery_does_not_require_koji_or_open_journal(self):
        self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "finished")
        self.write_journal([self.build_record("demo-1-1", "pass", "finished")])
        with patch.object(mod.shutil, "which", side_effect=lambda name:
                          None if name == "koji" else "/fake/tarpm"), \
                patch.object(mod.JOURNAL, "open") as open_journal:
            for option in ("--resume", "--retry-failed"):
                with self.subTest(option=option):
                    self.assertEqual(self.run_main(option), 0)
        open_journal.assert_not_called()

    def test_cli_missing_koji_exits_two_without_creating_run_state(self):
        empty_path = self.root / "empty-path"
        empty_path.mkdir()
        result = subprocess.run(
            [sys.executable, str(_ROOT / "tarpm-tag-runner.py"),
             *self.argv], capture_output=True, text=True,
            env={**os.environ, "PATH": str(empty_path)}, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("koji client not found", result.stderr)
        self.assertFalse((self.downloads / "run.json").exists())
        self.assertFalse((self.downloads / "journal.jsonl").exists())

    def test_cli_download_resolves_koji_from_relative_path(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        koji = bin_dir / "koji"
        koji.write_text('#!/bin/sh\nprintf "downloaded" > "$2.noarch.rpm"\n')
        koji.chmod(0o755)
        tarpm = bin_dir / "tarpm"
        tarpm.write_text('#!/bin/sh\nexit 0\n')
        tarpm.chmod(0o755)
        result = subprocess.run(
            [sys.executable, str(_ROOT / "tarpm-tag-runner.py"), *self.argv],
            cwd=self.root, capture_output=True, text=True,
            env={**os.environ, "PATH": "bin"}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("test:  1 RPM(s), 1 ok", result.stdout)
        self.assertEqual(list(self.downloads.glob(".build-*")), [])


class JournalRecoveryTests(RunnerTestCase):
    def test_resume_retests_an_existing_fetch_only_record(self):
        self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "active")
        self.write_journal([self.build_record("demo-1-1", "fetched")])
        with patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--resume"), 0)
        process.assert_called_once()
        latest = mod.read_journal(str(self.downloads / "journal.jsonl"))
        self.assertEqual(latest["demo-1-1"]["status"], "pass")

    def test_new_record_survives_torn_tail(self):
        path = self.root / "journal.jsonl"
        old = self.build_record("old-1-1", "pass")
        path.write_text(json.dumps(old) + '\n{"nvr":"broken')
        mod.JOURNAL.open(str(path))
        new = self.build_record("new-1-1", "test-fail")
        mod.JOURNAL.append(new)
        self.assertEqual(mod.read_journal(str(path)),
                         {old["nvr"]: old, new["nvr"]: new})

    def test_valid_tail_without_newline_is_preserved(self):
        path = self.root / "journal.jsonl"
        old = self.build_record("old-1-1", "pass")
        path.write_text(json.dumps(old))
        mod.JOURNAL.open(str(path))
        new = self.build_record("new-1-1", "pass")
        mod.JOURNAL.append(new)
        self.assertEqual(mod.read_journal(str(path)),
                         {old["nvr"]: old, new["nvr"]: new})


class RunManifestTests(RunnerTestCase):
    def test_interrupted_rerun_resumes_active_and_queued_old_passes(self):
        nvrs = ["done-1-1", "active-1-1", "queued-1-1"]
        self.seed_tag("\n".join(nvrs) + "\n")
        manifest_path = self.downloads / "run.json"
        self.write_manifest(manifest_path, nvrs, "old-run")
        self.write_journal(self.build_record(nvr, "pass", "old-run") for nvr in nvrs)
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
        self.assertEqual(partial.read_text(), "incomplete rerun")
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
        self.write_manifest(path, ["demo-1-1"], "previous-run")
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

    def test_logs_setup_failure_preserves_active_run(self):
        path = self.downloads / "run.json"
        self.write_manifest(path, ["demo-1-1"], "previous-run")
        before = path.read_bytes()
        journal = self.write_journal([self.build_record("demo-1-1", "test-fail", "previous-run")])
        records = journal.read_bytes()
        (self.downloads / "logs").write_text("regular file")
        with patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main(), 2)
        process.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(journal.read_bytes(), records)

    def test_invalid_manifest_never_falls_back_to_old_passes(self):
        self.downloads.mkdir(exist_ok=True)
        (self.downloads / "journal.jsonl").write_text(json.dumps(
            {"nvr": "demo-1-1", "status": "pass"}) + "\n")
        for invalid in ('{"run_id":', '{}',
                        '{"run_id":"new", "nvrs":["../demo-1-1"]}',
                        '{"run_id":"old", "nvrs":["Fedora-Games-Live-44-20260424.n.0"]}'):
            with self.subTest(manifest=invalid):
                (self.downloads / "run.json").write_text(invalid)
                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build") as process:
                    self.assertEqual(self.run_main("--resume"), 2)
                process.assert_not_called()

    def test_resume_requires_manifest_even_with_journal_history(self):
        journal = self.write_journal([{"nvr": "demo-1-1", "status": "pass"}])
        before = journal.read_bytes()
        with patch.object(mod, "read_nvrs") as read, \
                patch.object(mod, "process_build") as process, \
                patch.object(mod.JOURNAL, "open") as open_journal:
            self.assertEqual(self.run_main("--resume"), 2)
            self.assertEqual(self.run_main("--resume", "--list"), 2)
        read.assert_not_called()
        process.assert_not_called()
        open_journal.assert_not_called()
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse((self.downloads / "run.json").exists())
        self.assertFalse((self.root / "report.txt").exists())

    def test_list_does_not_replace_manifest(self):
        path = self.downloads / "run.json"
        self.write_manifest(path, ["demo-1-1"], "existing-run")
        before = path.read_bytes()
        for options in (["--list"], ["--resume", "--list"]):
            with self.subTest(options=options):
                self.assertEqual(self.run_main(*options), 0)
                self.assertEqual(path.read_bytes(), before)

    def test_filtered_resume_preserves_other_pending_builds(self):
        nvrs = ["aaa-1-1", "bbb-1-1"]
        self.seed_tag("\n".join(nvrs) + "\n")
        path = self.downloads / "run.json"
        self.write_manifest(path, nvrs, "current-run")
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
        self.seed_tag("demo-1-1\nother-1-1\n")
        self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "current-run")
        self.write_journal([self.build_record("demo-1-1", "pass", "current-run")])
        with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                patch.object(mod, "process_build") as process:
            self.assertEqual(self.run_main("--resume"), 0)
        process.assert_not_called()

    def test_resume_keeps_pending_builds_after_tag_refresh(self):
        manifest = self.downloads / "run.json"
        self.write_manifest(manifest, ["old-1-1"], "active")
        before = manifest.read_bytes()
        cache = self.downloads / "tag-f44.txt"
        cache.write_text(mod.RPM_TAG_CACHE_HEADER + "old-1-1\n")
        with patch.object(mod, "run", return_value=(0, "new-1-1 f44 user\n")):
            self.assertEqual(self.run_main("--list", "--refresh"), 0)
        self.assertIn("new-1-1", cache.read_text())
        with patch.object(mod, "read_nvrs") as read, \
                patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--resume"), 0)
        read.assert_not_called()
        self.assertEqual([call.args[0] for call in process.call_args_list], ["old-1-1"])
        self.assertEqual(manifest.read_bytes(), before)

    def test_retry_uses_historical_failures_but_starts_a_new_run(self):
        path = self.downloads / "run.json"
        self.write_manifest(path, ["other-1-1"], "previous-run")
        self.write_journal([self.build_record("demo-1-1", "test-fail", "older-run")])
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
        records = [self.build_record("first-1-1", "test-fail"),
                   self.build_record("second-1-1", "pass")]
        with patch.object(mod.os, "write", side_effect=lambda fd, data: real_write(fd, data[:7])):
            for record in records:
                mod.JOURNAL.append(record)
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], records)

    def test_failed_append_rolls_back_before_next_record(self):
        path = self.root / "journal.jsonl"
        mod.JOURNAL.open(str(path))
        old = self.build_record("old-1-1", "pass")
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
            mod.JOURNAL.append(self.build_record("broken-1-1", "test-fail"))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("could not append journal record", stderr.getvalue())
        new = self.build_record("new-1-1", "pass")
        mod.JOURNAL.append(new)
        self.assertEqual(mod.read_journal(str(path)), {old["nvr"]: old, new["nvr"]: new})

    def test_zero_length_write_does_not_loop_or_damage_next_record(self):
        path = self.root / "journal.jsonl"
        mod.JOURNAL.open(str(path))
        with patch.object(mod.os, "write", return_value=0), \
                contextlib.redirect_stderr(io.StringIO()):
            mod.JOURNAL.append(self.build_record("broken-1-1", "test-fail"))
        record = self.build_record("new-1-1", "pass")
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
            mod.JOURNAL.append(self.build_record("broken-1-1", "test-fail"))
        self.assertIsNone(mod.JOURNAL._fh)
        self.assertIn("journal disabled: rollback failed", stderr.getvalue())
        before = path.read_bytes()
        with patch.object(mod.os, "write") as write:
            mod.JOURNAL.append(self.build_record("new-1-1", "pass"))
        write.assert_not_called()
        self.assertEqual(path.read_bytes(), before)


class CliSelectionTests(RunnerTestCase):
    def list_output(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = mod.main(self.argv + ["--list"] + list(options))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_resume_list_closed_pipe_exits_without_tracebacks(self):
        journal = self.write_journal([self.build_record("demo-0-1", "test-fail", "pipe-test")])
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
                self.seed_tag("\n".join(nvrs) + "\n")
                self.write_manifest(manifest, nvrs, "pipe-test")
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

    def test_removed_flags_are_rejected_before_work(self):
        for options in (["--test-only"], ["--from-file", "nvrs.txt"],
                        ["--no-test"], ["--keep"]):
            with self.subTest(options=options), \
                    patch.object(mod, "run") as command, \
                    self.assertRaises(SystemExit) as error:
                self.run_main(*options)
            self.assertEqual(error.exception.code, 2)
            command.assert_not_called()
            self.assertFalse((self.downloads / "run.json").exists())

    def test_cached_list_stdout_contains_only_nvrs(self):
        self.downloads.mkdir(exist_ok=True)
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
            ("demo-1-1 f44 user\n", "Warning: the pkgurl option is obsolete\n"),
        ]
        proc.returncode = 0
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod.subprocess, "Popen", return_value=proc) as popen:
            result, stdout, stderr = self.list_output("--refresh")
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "demo-1-1\n")
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertIn("still fetching koji tag", stderr)
        self.assertIn("saved tag list", stderr)
        self.assertIn("Warning: the pkgurl option is obsolete", stderr)
        cache = self.downloads / "tag-f44.txt"
        self.assertEqual(cache.read_text(), mod.RPM_TAG_CACHE_HEADER + "demo-1-1 f44 user\n")

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
        self.downloads.mkdir(exist_ok=True)
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
        self.downloads.mkdir(exist_ok=True)
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

    def test_failed_cache_write_preserves_previous_snapshot(self):
        self.downloads.mkdir(exist_ok=True)
        cache = self.downloads / "tag-f44.txt"
        previous = mod.RPM_TAG_CACHE_HEADER + "old-1-1\nother-1-1\n"
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                cache.write_text(previous)
                with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                        patch.object(mod, "run", return_value=(0, "new-1-1\n")), \
                        patch.object(mod.os, operation, side_effect=OSError("disk full")):
                    result, stdout, stderr = self.list_output("--refresh")
                self.assertEqual(result, 2)
                self.assertEqual(stdout, "")
                self.assertIn("cannot save cached tag list", stderr)
                self.assertEqual(cache.read_text(), previous)
                self.assertEqual(list(self.downloads.glob(".tag-*.tmp")), [])
                with patch.object(mod, "run") as query:
                    result, stdout, _ = self.list_output()
                self.assertEqual(result, 0)
                self.assertEqual(stdout, "old-1-1\nother-1-1\n")
                query.assert_not_called()

    def test_interrupted_partial_cache_write_never_replaces_snapshot(self):
        self.downloads.mkdir(exist_ok=True)
        cache = self.downloads / "tag-f44.txt"
        previous = mod.RPM_TAG_CACHE_HEADER + "old-1-1\nother-1-1\n"
        cache.write_text(previous)
        real_temporary = mod.tempfile.NamedTemporaryFile

        def partial_file(**kwargs):
            stream = real_temporary(**kwargs)
            write = stream.write

            def interrupted_write(text):
                write(text[:len(mod.RPM_TAG_CACHE_HEADER) + len("new-1-1\n")])
                stream.flush()
                raise KeyboardInterrupt

            stream.write = interrupted_write
            return stream

        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod, "run", return_value=(0, "new-1-1\nother-1-1\n")), \
                patch.object(mod.tempfile, "NamedTemporaryFile", side_effect=partial_file), \
                self.assertRaises(KeyboardInterrupt):
            self.list_output("--refresh")
        self.assertEqual(cache.read_text(), previous)
        self.assertEqual(list(self.downloads.glob(".tag-*.tmp")), [])

    def test_invalid_query_output_does_not_replace_cache(self):
        self.downloads.mkdir(exist_ok=True)
        cache = self.downloads / "tag-f44.txt"
        previous = mod.RPM_TAG_CACHE_HEADER + "old-1-1\n"
        cache.write_text(previous)
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        with patch.object(mod.shutil, "which", return_value="/fake/koji"), \
                patch.object(mod, "run", return_value=(0, "new-1-1\ninvalid\n")):
            result, stdout, stderr = self.list_output("--refresh")
        self.assertEqual(result, 2)
        self.assertEqual(stdout, "")
        self.assertIn("invalid build NVR", stderr)
        self.assertEqual(cache.read_text(), previous)

    def test_retry_failed_drops_image_failures_from_unfiltered_tag_cache(self):
        cache = self.downloads / "tag-f44.txt"
        image = "Fedora-Games-Live-44-20260424.n.0"
        self.write_journal([self.build_record(image, "fetch-fail"),
                            self.build_record("hello-1-1", "test-fail")])
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        cache.write_text(f"{image}\nhello-1-1\n")
        with patch.object(mod, "run", return_value=(0, "hello-1-1 f44 user\n")):
            result, stdout, _ = self.list_output("--retry-failed")
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "hello-1-1\n")
        self.assertFalse((self.downloads / "run.json").exists())

    def test_resume_list_excludes_passed_and_warned_builds(self):
        nvrs = ["pass-1-1", "warn-1-1", "fail-1-1", "new-1-1"]
        self.seed_tag("\n".join(nvrs) + "\n")
        self.write_manifest(self.downloads / "run.json", nvrs, "active")
        self.write_journal([self.build_record("pass-1-1", "pass"),
                            self.build_record("warn-1-1", "warn"),
                            self.build_record("fail-1-1", "test-fail")])
        with patch.object(mod, "process_build") as process:
            result, stdout, stderr = self.list_output("--resume")
        self.assertEqual(result, 0)
        self.assertEqual(stdout, "fail-1-1\nnew-1-1\n")
        self.assertIn("skipping 2 of 4", stderr)
        process.assert_not_called()

    def test_empty_recovery_lists_leave_stdout_empty(self):
        self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "active")
        self.write_journal([self.build_record("demo-1-1", "pass")])
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                result, stdout, stderr = self.list_output(mode)
                self.assertEqual(result, 0)
                self.assertEqual(stdout, "")
                self.assertIn("nothing to do", stderr)


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


class ReportPathTests(RunnerTestCase):
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

    def test_output_aliases_are_rejected_before_changing_run_state(self):
        manifest = self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "active")
        journal = self.write_journal([self.build_record("demo-1-1", "pass")])
        original_journal = journal.read_bytes()
        alias = self.root / "journal-copy.json"
        os.link(journal, alias)
        symlink = self.root / "manifest-link.txt"
        symlink.symlink_to(self.downloads / "run.json")
        for options in (
                ["--json", str(self.downloads / "run.json")],
                ["--json", str(journal)],
                ["--json", str(alias)],
                ["-r", str(symlink)],
                ["--json", str(self.root / "report.txt")]):
            with self.subTest(options=options), patch.object(mod, "fetch_one") as fetch:
                self.assertEqual(self.run_main(*options), 2)
                fetch.assert_not_called()
                self.assertEqual(json.loads((self.downloads / "run.json").read_text()), manifest)
                self.assertEqual(journal.read_bytes(), original_journal)

    def test_missing_report_parent_prevents_downloads(self):
        for option in ("-r", "--json"):
            with self.subTest(option=option), patch.object(mod, "process_build") as process:
                self.assertEqual(self.run_main(option, str(self.root / "missing/report")), 2)
            process.assert_not_called()
            self.assertFalse((self.downloads / "run.json").exists())

    def test_reports_can_use_download_directories_created_by_the_runner(self):
        report = self.downloads / "logs" / "report.txt"
        json_path = self.downloads / "results.json"
        with patch.object(mod, "process_build", side_effect=self.passed_build):
            self.assertEqual(self.run_main("-r", str(report),
                                           "--json", str(json_path)), 0)
        self.assertTrue(report.is_file())
        self.assertTrue(json_path.is_file())

    def test_report_cannot_replace_generated_failure_list(self):
        report = self.downloads / "logs" / "failed-all.txt"
        with patch.object(mod, "fetch_one") as fetch:
            self.assertEqual(self.run_main("-r", str(report)), 2)
        fetch.assert_not_called()
        self.assertFalse(report.exists())

    def test_failure_list_cleanup_preserves_logs_and_custom_reports(self):
        logdir = self.downloads / "logs"
        logdir.mkdir(parents=True)
        fetch_log = logdir / "failed-fetch-1-1.fetch.log"
        test_log = logdir / "failed-test-1-1.src.test.log"
        for path in (fetch_log, test_log):
            path.write_text("retained diagnostics")
        args = mod.parse_args(self.argv)
        fetched = mod.FetchResult("failed-fetch-1-1", False, 1, fetch_log,
                                  signature="download error")
        tested = mod.TestResult("failed-test-1-1.src.rpm", "failed-test-1-1", "src",
                                False, "unpack", 1, log=test_log, signature="unpack error")
        mod.write_failure_lists(args, [fetched], [tested])
        generated = set(logdir.iterdir()) - {fetch_log, test_log}
        self.assertEqual(len(generated), 6)
        # Replacing an older failure list must also preserve retained logs.
        mod.write_failure_lists(args, [fetched], [tested])

        report = logdir / "failed-summary.txt"
        with patch.object(mod, "process_build", side_effect=self.passed_build):
            self.assertEqual(self.run_main("-r", str(report)), 0)
        self.assertEqual(set(logdir.iterdir()), {report, fetch_log, test_log})
        self.assertIn("status:    passed", report.read_text())
        for path in (fetch_log, test_log):
            self.assertEqual(path.read_text(), "retained diagnostics")


class TimingTests(RunnerTestCase):
    def test_stage_times_include_the_failing_command_and_skip_unreached_stages(self):
        stages = ["unpack", "repack", "unpack-repacked", "list-original", "list-repacked"]
        for failure in [None] + stages:
            with self.subTest(failure=failure):
                args = mod.parse_args(self.argv)
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
                if failure:
                    self.assertIn("timings: unpack=2.000s", result.log.read_text())

    def test_fetch_time_and_summary_show_measured_failures_without_inventing_exception_times(self):
        args = mod.parse_args(self.argv)
        (self.downloads / "logs").mkdir(parents=True)
        with patch.object(mod, "run", return_value=(1, "failed download")), \
                patch.object(mod.time, "monotonic", side_effect=[10, 14.5]), \
                contextlib.redirect_stdout(io.StringIO()):
            fetched = mod.fetch_one("demo-1-1", args, download_dir=self.root / "fetch")
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
                    self.assertEqual(self.run_main("--compare", str(path)), 2)
                fetch.assert_not_called()
                process.assert_not_called()
                self.assertFalse((self.downloads / "run.json").exists())
                self.assertFalse((self.root / "report.txt").exists())
        with self.assertRaises(mod.UsageError):
            mod.read_comparison_report(self.root / "missing.json")

    def test_main_loads_baseline_before_overwriting_the_same_json_path(self):
        path = self.baseline(fetch=[mod.FetchResult("demo-1-1", False, 1, signature="network error")])
        with patch.object(mod, "process_build", side_effect=self.passed_build):
            self.assertEqual(self.run_main("--compare", str(path),
                                           "--json", str(path)), 0)
        data = json.loads(path.read_text())
        self.assertEqual(len(data['comparison']['fetch']['resolved']), 1)
        self.assertEqual(data['comparison']['generated'], "2026-10-21T10:00:00+00:00")
        self.assertIn("1 resolved", (self.root / "report.txt").read_text())

    def test_current_failure_still_exits_one_when_comparison_is_text_only(self):
        path = self.baseline(fetch=[mod.FetchResult("demo-1-1", True)])
        failed = mod.FetchResult("demo-1-1", False, 1, signature="download failed")
        with patch.object(mod, "fetch_one", return_value=failed):
            self.assertEqual(self.run_main("--compare", str(path)), 1)
        report = (self.root / "report.txt").read_text()
        self.assertIn("fetch REGRESSION demo-1-1", report)
        self.assertIn("1 regressions", report)

    def test_list_cannot_silently_ignore_comparison(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            mod.parse_args(["--list", "--compare", "baseline.json"])
        self.assertEqual(exc.exception.code, 2)


class BuildPathTests(RunnerTestCase):
    def test_invalid_nvrs_are_rejected_before_any_build_runs(self):
        outside = self.root / "outside-1-1"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("unrelated data")
        self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "active")
        for invalid in ("../outside-1-1", str(outside), ".", "..", "demo",
                        "demo--1", "dir/demo-1-1", r"dir\demo-1-1"):
            with self.subTest(nvr=invalid):
                self.write_manifest(self.downloads / "run.json", ["demo-1-1", invalid], "active")
                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build") as process:
                    self.assertEqual(self.run_main("--resume"), 2)
                process.assert_not_called()
                self.assertEqual(marker.read_text(), "unrelated data")

    def test_valid_nvrs_include_rpm_version_separators(self):
        for nvr in ("hello-2.12.2-1.fc44", "389-ds-base-3.1.2-1.fc44",
                    "libstdc++-16.0.1-0.1.fc44", "demo-1.0~rc1^git-1.fc44"):
            with self.subTest(nvr=nvr):
                mod.validate_nvr(nvr)

    def test_recovery_does_not_delete_symlink_targets(self):
        outside = self.root / "outside-1-1"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("unrelated data")
        self.downloads.mkdir(exist_ok=True)
        link = self.downloads / "demo-1-1"
        link.symlink_to(outside, target_is_directory=True)
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                self.write_manifest(self.downloads / "run.json", ["demo-1-1"], "active")
                self.write_journal([self.build_record("demo-1-1", "test-fail")])
                with patch.object(mod.shutil, "which", return_value="/fake/tarpm"), \
                        patch.object(mod, "process_build", side_effect=self.passed_build) as process:
                    self.assertEqual(self.run_main(mode), 0)
                process.assert_called_once()
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
        self.downloads.mkdir(exist_ok=True)
        (self.downloads / "first.rpm").write_bytes(b"x" * 2048)
        (self.downloads / "next.rpm").write_bytes(b"x" * 1024)
        output = "Downloading [2/2]: next.rpm\n"
        self.assertEqual(mod.fetch_progress("demo-1-1", self.downloads, output, 60),
                         ("fetching next.rpm (RPM 2/2, 100.0%)", "1m 00s, 1 KiB so far"))
        self.assertEqual(mod.fetch_progress("demo-1-1", self.downloads, None, 60),
                         ("fetching demo-1-1", "1m 00s, 3 KiB build total so far"))

    def test_unknown_percentage_retains_native_rpm_size(self):
        output = "Downloading: demo.rpm\n" + self.bar("???%")
        self.assertEqual(mod.fetch_progress("demo-1-1", self.downloads, output, 60),
                         ("fetching demo.rpm", "1m 00s, 913.00 MiB / 1.46 GiB so far"))
        # Clients may emit only downloaded bytes, without the expected total.
        output = output.replace(" / 1.46 GiB", "")
        self.assertEqual(mod.fetch_progress("demo-1-1", self.downloads, output, 60),
                         ("fetching demo.rpm", "1m 00s, 913.00 MiB so far"))
        # Batch position is available even when the RPM's byte progress isn't.
        output = output.replace("Downloading:", "Downloading [2/3]:")
        self.assertEqual(mod.fetch_progress("demo-1-1", self.downloads, output, 60),
                         ("fetching demo.rpm (RPM 2/3, 66.7%)", "1m 00s, 913.00 MiB so far"))

    def test_fetch_heartbeat_uses_native_progress_and_keeps_full_output(self):
        args = mod.parse_args(self.argv + ["--heartbeat", "1"])
        (self.downloads / "logs").mkdir(parents=True)
        first = "Downloading [1/2]: first.rpm\n" + self.bar("61%")
        next_rpm = first + self.bar("100%") + "Downloading [2/2]: next.rpm\n"
        final = next_rpm + self.bar("21%") + self.bar("100%") + "\n"
        cmd = ["koji", "download-build", "demo-1-1"]
        proc = Mock()
        proc.returncode = 7
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd, 1, output=first.encode()),
            subprocess.TimeoutExpired(cmd, 1, output=next_rpm.encode()),
            subprocess.TimeoutExpired(cmd, 1, output=next_rpm + self.bar("21%")),
            (final, None),
        ]
        with patch.object(mod.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(mod, "koji_download_progress", wraps=mod.koji_download_progress) as parse, \
                patch.object(mod, "_file_bytes", return_value=1024), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = mod.fetch_one("demo-1-1", args, mod.Counter(2),
                                   download_dir=self.root / "fetch")
        self.assertEqual(result.exit_code, 7)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], cmd)
        beats = [line for line in stdout.getvalue().splitlines() if "still fetching" in line]
        self.assertEqual(len(beats), 3)
        self.assertEqual(parse.call_count, len(beats))
        self.assertIn("fetching first.rpm (RPM 1/2, 50.0%) (build 1/2)", beats[0])
        self.assertIn(", 913.00 MiB / 1.46 GiB, 61% so far)", beats[0])
        self.assertIn("fetching next.rpm (RPM 2/2, 100.0%) (build 1/2)", beats[1])
        self.assertIn(", 1 KiB so far)", beats[1])
        self.assertNotIn("%", beats[1].rsplit("(", 1)[-1])
        self.assertIn("fetching next.rpm (RPM 2/2, 100.0%) (build 1/2)", beats[2])
        self.assertIn(", 913.00 MiB / 1.46 GiB, 21% so far)", beats[2])
        self.assertEqual(result.log.read_bytes().decode(), final)

    def test_fast_fetch_does_not_parse_progress(self):
        args = mod.parse_args(self.argv)
        (self.downloads / "logs").mkdir(parents=True)
        proc = Mock(returncode=0)
        proc.communicate.return_value = ("downloaded\n", None)
        with patch.object(mod.subprocess, "Popen", return_value=proc), \
                patch.object(mod, "koji_download_progress") as parse, \
                contextlib.redirect_stdout(io.StringIO()):
            result = mod.fetch_one("demo-1-1", args, download_dir=self.root / "fetch")
        self.assertTrue(result.ok)
        parse.assert_not_called()


class FetchRecoveryTests(RunnerTestCase):
    def test_failed_fetch_discards_partial_download_and_can_resume(self):
        fetched = []

        def download(cmd, cwd, **kwargs):
            fetched.append(cwd)
            (cwd / "demo-1-1.noarch.rpm").write_text("partial RPM")
            (cwd / ".part").mkdir()
            return 7, "download interrupted"

        with patch.object(mod, "run", side_effect=download), \
                patch.object(mod, "test_one") as test:
            self.assertEqual(self.run_main(), 1)
        test.assert_not_called()
        self.assertEqual(len(fetched), 1)
        self.assertFalse(fetched[0].parent.exists())
        journal = self.downloads / "journal.jsonl"
        record = mod.read_journal(journal)["demo-1-1"]
        self.assertEqual((record["status"], record["exit_code"]), ("fetch-fail", 7))
        self.assertIn("download interrupted", Path(record["results"]["fetch"]["log"]).read_text())
        with patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("--resume"), 0)
        process.assert_called_once()
        updated = mod.read_journal(journal)["demo-1-1"]
        self.assertEqual(updated["status"], "pass")
        self.assertEqual(updated["run_id"], record["run_id"])

    def test_failed_workspace_cleanup_does_not_hide_download_failure(self):
        with patch.object(mod, "run", return_value=(7, "download failed")), \
                patch.object(mod, "remove_tree", side_effect=PermissionError("denied")):
            self.assertEqual(self.run_main(), 1)
        record = mod.read_journal(self.downloads / "journal.jsonl")["demo-1-1"]
        self.assertEqual(record["exit_code"], 7)
        self.assertEqual(record["signature"], "download failed")

    def test_duplicate_nvrs_are_processed_once(self):
        self.seed_tag("demo-1-1\ndemo-1-1  # repeated\n")
        with patch.object(mod, "process_build", side_effect=self.passed_build) as process:
            self.assertEqual(self.run_main("-j", "2"), 0)
        process.assert_called_once()

    def test_run_and_resume_use_only_their_own_downloads(self):
        saved = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        saved.parent.mkdir()
        saved.write_bytes(b"unrelated RPM")
        unfinished = self.downloads / ".build-interrupted/demo-1-1/demo-1-1.noarch.rpm"
        unfinished.parent.mkdir(parents=True)
        unfinished.write_bytes(b"old partial download")
        fetched = []

        def command(cmd, cwd=None, binary_output=False, **kwargs):
            if cmd[1] == "download-build":
                self.assertEqual(list(cwd.iterdir()), [])
                fetched.append(cwd)
                (cwd / saved.name).write_bytes(b"current download")
            elif cmd[1] == "-x" and Path(cmd[3]).name == saved.name:
                rpm = Path(cmd[3])
                self.assertEqual(rpm.parent, fetched[-1])
                self.assertEqual(rpm.read_bytes(), b"current download")
                if len(fetched) == 1:
                    return 9, "tarpm: first attempt failed"
            return 0, b"same listing\n" if binary_output else ""

        report = self.root / "downloads.json"
        with patch.object(mod, "run", side_effect=command):
            self.assertEqual(self.run_main("--json", str(report)), 1)
            first = json.loads(report.read_text())
            self.assertEqual(first["test"][0]["stage"], "unpack")
            self.assertFalse(fetched[0].parent.exists())
            self.assertEqual(self.run_main("--resume", "--json", str(report)), 0)
        resumed = json.loads(report.read_text())
        self.assertEqual(len(fetched), 2)
        self.assertNotEqual(fetched[0], fetched[1])
        self.assertFalse(fetched[1].parent.exists())
        self.assertEqual(resumed["run"]["id"], first["run"]["id"])
        self.assertEqual(resumed["run"]["status"], "passed")
        self.assertEqual(resumed["test"][0]["rpm"], str(fetched[1] / saved.name))
        self.assertEqual(saved.read_bytes(), b"unrelated RPM")
        self.assertEqual(unfinished.read_bytes(), b"old partial download")

    def test_no_src_filters_tests_and_removes_all_downloads(self):
        def command(cmd, cwd=None, binary_output=False, **kwargs):
            if cmd[1] == "download-build":
                (cwd / "demo-1-1.src.rpm").write_text("source")
                (cwd / "demo-1-1.x86_64.rpm").write_text("binary")
            return 0, b"same listing\n" if binary_output else ""

        for arch in ([], ["-a", "x86_64"]):
            with self.subTest(arch=arch), patch.object(mod, "run", side_effect=command):
                report = self.root / "no-src.json"
                self.assertEqual(self.run_main("--no-src", *arch, "--json", str(report)), 0)
                self.assertEqual([r["arch"] for r in json.loads(report.read_text())["test"]],
                                 ["x86_64"])
                self.assertEqual(list(self.downloads.glob(".build-*")), [])

    def test_no_eligible_rpms_are_reported_as_a_failure(self):
        def download(cmd, cwd, **kwargs):
            (cwd / "demo-1-1.src.rpm").write_text("source")
            return 0, "downloaded"

        with patch.object(mod, "run", side_effect=download), \
                patch.object(mod, "test_one") as test:
            self.assertEqual(self.run_main("--no-src"), 1)
        test.assert_not_called()
        record = mod.read_journal(self.downloads / "journal.jsonl")["demo-1-1"]
        self.assertEqual(record["status"], "fetch-fail")
        self.assertIn("no RPMs matching", record["signature"])
        self.assertEqual(list(self.downloads.glob(".build-*")), [])


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
        args = mod.parse_args(self.argv + ["--heartbeat", "1"])
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
        args = mod.parse_args(self.argv + ["--heartbeat", "1"])
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


class ListingEncodingTests(RunnerTestCase):
    def test_listing_diagnostics_do_not_change_the_content_comparison(self):
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir()
        rpm.write_text("original rpm")

        def spawn(cmd, **kwargs):
            child = Mock(returncode=0)
            output = (None, None)
            if cmd[1] == "-t":
                self.assertEqual(kwargs["stderr"], subprocess.PIPE)
                output = (b"same listing\n", f"Warning for {Path(cmd[3]).name}\n".encode())
            child.communicate.return_value = output
            return child

        with patch.object(mod.subprocess, "Popen", side_effect=spawn), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = mod.test_one(rpm, mod.parse_args(self.argv))
        self.assertTrue(result.ok)
        self.assertIsNone(result.warning)
        self.assertIn(f"Warning for {rpm.name}", stderr.getvalue())
        self.assertIn(f"Warning for {rpm.stem}-repacked.rpm", stderr.getvalue())

    def test_listings_are_compared_and_saved_without_lossy_decoding(self):
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        (self.downloads / "logs").mkdir()
        work = rpm.parent / ".tarpm-demo-1-1.noarch"
        for original, repacked in ((b"name-\xff\n", b"name-\xff\n"),
                                   (b"name-\xff\n", b"name-\xfe\n"),
                                   (b"name-\\xff\n", b"name-\xff\n")):
            with self.subTest(original=original, repacked=repacked):
                listings = iter((original, repacked))
                rpm.write_text("original rpm")

                def cleanup(directory):
                    self.assertEqual((work / "list-orig.txt").read_bytes(), original)
                    self.assertEqual((work / "list-repacked.txt").read_bytes(), repacked)
                    shutil.rmtree(directory)

                def command(cmd, **kwargs):
                    return (0, next(listings)) if cmd[1] == "-t" else (0, "")

                with patch.object(mod, "run", side_effect=command), \
                        patch.object(mod, "remove_tree", side_effect=cleanup), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = mod.test_one(rpm, mod.parse_args(self.argv))
                self.assertTrue(result.ok)
                self.assertEqual(result.warning, "content-diff" if original != repacked else None)
                self.assertFalse(work.exists())
                self.assertFalse(rpm.exists())
                if original != repacked:
                    self.assertIn("\\xff", result.log.read_text())

    def test_failed_binary_listing_keeps_stage_exit_code_and_diagnostic(self):
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_text("original rpm")
        (self.downloads / "logs").mkdir()

        def spawn(cmd, **kwargs):
            if cmd[1] == "-t":
                child = Mock(returncode=9)
                child.communicate.return_value = (b"partial listing", b"tarpm: invalid \xff\n")
            else:
                child = Mock(returncode=0)
                child.communicate.return_value = (None, None)
            return child

        with patch.object(mod.subprocess, "Popen", side_effect=spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            result = mod.test_one(rpm, mod.parse_args(self.argv))
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "list-original")
        self.assertEqual(result.exit_code, 9)
        self.assertEqual(result.signature, "invalid \\xff")
        self.assertIn("partial listing\ntarpm: invalid \\xff", result.log.read_text())
        self.assertFalse(rpm.exists())


class ArtifactCleanupTests(RunnerTestCase):
    def test_passing_test_removes_readonly_payload_directories(self):
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir(parents=True)
        rpm.write_text("original rpm")
        (self.downloads / "logs").mkdir()
        work = rpm.parent / ".tarpm-demo-1-1.noarch"

        def command(cmd, **kwargs):
            if cmd[1] == "-x":
                directory = Path(cmd[-1]) / "readonly"
                directory.mkdir(parents=True)
                (directory / "data").write_text("payload")
                directory.chmod(0o500)
            return 0, "same listing\n"

        with patch.object(mod, "run", side_effect=command), \
                contextlib.redirect_stdout(io.StringIO()):
            result = mod.test_one(rpm, mod.parse_args(self.argv))
        self.assertTrue(result.ok)
        self.assertFalse(work.exists())
        self.assertFalse(rpm.exists())
        self.assertFalse(result.log.exists())

    def test_permission_retry_repairs_nested_dirs_without_following_symlinks(self):
        tree = self.root / "tree"
        nested = tree / "locked/nested"
        nested.mkdir(parents=True)
        (nested / "data").write_text("payload")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("external data")
        outside.chmod(0o555)
        self.addCleanup(lambda: outside.chmod(0o755))
        (tree / "link").symlink_to(outside, target_is_directory=True)
        (tree / "locked").chmod(0o000)
        real_rmtree = mod.shutil.rmtree
        calls = 0

        def remove(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("read-only payload")
            self.assertEqual((tree / "locked").stat().st_mode & 0o700, 0o700)
            return real_rmtree(path)

        with patch.object(mod.shutil, "rmtree", side_effect=remove):
            mod.remove_tree(tree)
        self.assertEqual(calls, 2)
        self.assertFalse(tree.exists())
        self.assertEqual(outside.stat().st_mode & 0o777, 0o555)
        self.assertEqual((outside / "keep").read_text(), "external data")

    def test_persistent_cleanup_failure_preserves_log_and_timings(self):
        def command(cmd, cwd=None, **kwargs):
            if cmd[1] == "download-build":
                (cwd / "demo-1-1.noarch.rpm").write_bytes(b"downloaded RPM")
            return 0, "listing\n"

        path = self.root / "cleanup.json"
        with patch.object(mod, "run", side_effect=command), \
                patch.object(mod.shutil, "rmtree", side_effect=PermissionError("denied")):
            self.assertEqual(self.run_main("--json", str(path)), 1)
        result = json.loads(path.read_text())["test"][0]
        self.assertEqual(result["stage"], "cleanup")
        self.assertEqual(len(result["stage_seconds"]), 5)
        self.assertIn("cannot clean up files", result["signature"])
        self.assertIn("denied", Path(result["log"]).read_text())
        record = mod.read_journal(self.downloads / "journal.jsonl")["demo-1-1"]
        self.assertEqual(record["status"], "test-fail")
        self.assertEqual(record["stage"], "cleanup")

    def test_warning_retains_log_after_cleaning_artifacts(self):
        args = mod.parse_args(self.argv)
        rpm = self.downloads / "demo-1-1/demo-1-1.noarch.rpm"
        rpm.parent.mkdir()
        rpm.write_text("original rpm")
        outputs = [(0, ""), (0, ""), (0, ""),
                   (0, "original list\n"), (0, "changed list\n")]
        with patch.object(mod, "run", side_effect=outputs), \
                contextlib.redirect_stdout(io.StringIO()):
            result = mod.test_one(rpm, args)
        self.assertTrue(result.ok)
        self.assertEqual(result.warning, "content-diff")
        self.assertIn("changed list", result.log.read_text())
        self.assertFalse(rpm.exists())
        self.assertFalse((rpm.parent / ".tarpm-demo-1-1.noarch").exists())

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
        listf = os.path.join(d, "tag-f44.txt")
        nvrs = ["aaa-1-1.fc44", "bbb-1-1.fc44", "ccc-1-1.fc44",
                "ddd-1-1.fc44"]
        with open(listf, "w", encoding="utf-8") as fh:
            fh.write(mod.RPM_TAG_CACHE_HEADER + "\n".join(nvrs) + "\n")

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
                mod.main(argv=["-d", d, "--no-run-log"])
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
