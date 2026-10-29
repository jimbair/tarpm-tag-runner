"""Size ordering, streaming metadata, and cache/recovery regressions."""
import contextlib
import io
import json
import threading
import unittest
import xmlrpc.client
from pathlib import Path
from unittest.mock import patch

from test_tarpm_tag_runner import RunnerTestCase, mod


def response(value):
    return io.BytesIO(xmlrpc.client.dumps((value,), methodresponse=True,
                                         allow_none=True).encode())


def build(nvr, build_id):
    return {"nvr": nvr, "id": build_id, "state": 1}


def rpm(build_id, size, arch="x86_64", name="demo"):
    return {"build_id": build_id, "size": size, "arch": arch, "name": name}


class SizeSortingTests(RunnerTestCase):
    def cache(self, sizes):
        self.downloads.mkdir(parents=True, exist_ok=True)
        path = self.downloads / "build-sizes.json"
        payload = {"schema_version": 1, "hub": mod.SIZE_HUB,
                   "exclude_debuginfo": True, "builds": {
                       nvr: {"build_id": i, "bytes_by_arch": by_arch}
                       for i, (nvr, by_arch) in enumerate(sizes.items(), 1)}}
        path.write_text(json.dumps(payload))
        return path

    def listed(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = mod.main(self.argv + ["--list", *options])
        return status, stdout.getvalue().splitlines(), stderr.getvalue()

    def test_choices_and_phase_heartbeat_defaults(self):
        args = mod.parse_args(["--sort", "asc"])
        self.assertEqual((args.sort, args.heartbeat, args.size_heartbeat), ("asc", 60, 15))
        for value in (0, 30, 60):
            args = mod.parse_args(["--heartbeat", str(value), "--sort", "desc"])
            self.assertEqual((args.heartbeat, args.size_heartbeat), (value, value))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mod.parse_args(["--sort", "size-asc"])

    def test_unsorted_selection_never_reads_or_fetches_sizes(self):
        self.listfile.write_text("z-1-1\na-1-1\n")
        with patch.object(mod, "read_size_cache") as cache, \
                patch.object(mod, "fetch_size_metadata") as fetch:
            status, nvrs, _ = self.listed()
        self.assertEqual((status, nvrs), (0, ["z-1-1", "a-1-1"]))
        cache.assert_not_called()
        fetch.assert_not_called()

    def test_arch_source_filters_ties_and_limit_apply_to_sizes(self):
        self.listfile.write_text("c-1-1\nb-1-1\na-1-1\n")
        self.cache({"a-1-1": {"x86_64": 10, "noarch": 5, "src": 1000},
                    "b-1-1": {"x86_64": 20, "src": 1, "aarch64": 5000},
                    "c-1-1": {"x86_64": 15, "src": 0}})
        cases = [([], ["c-1-1", "a-1-1", "b-1-1"]),
                 (["-a", "x86_64"], ["c-1-1", "b-1-1", "a-1-1"]),
                 (["-a", "x86_64", "--no-src"], ["a-1-1", "c-1-1", "b-1-1"])]
        with patch.object(mod.urllib.request, "urlopen") as fetch:
            for filters, expected in cases:
                for direction in ("asc", "desc"):
                    with self.subTest(filters=filters, direction=direction):
                        status, nvrs, stderr = self.listed("--sort", direction, *filters)
                        # Equal-size builds always use ascending NVR ties.
                        if direction == "desc":
                            expected_desc = list(reversed(expected))
                            if "--no-src" in filters:
                                expected_desc = ["b-1-1", "a-1-1", "c-1-1"]
                            self.assertEqual(nvrs, expected_desc)
                        else:
                            self.assertEqual(nvrs, expected)
                        self.assertEqual(status, 0)
                        self.assertIn("cached build sizes", stderr)
                self.assertEqual(self.listed("--sort", "asc", "-n", "1", *filters)[1], expected[:1])
        fetch.assert_not_called()

    def test_streamed_batched_lookup_then_offline_cache_reuse(self):
        self.listfile.write_text("large-1-1\nsmall-1-1\n")
        calls = []

        def query(request, timeout):
            params, method = xmlrpc.client.loads(request.data)
            calls.append((method, params))
            methods = [call["methodName"] for call in params[0]]
            if methods == ["getBuild", "getBuild"]:
                return response([[build("large-1-1", 10)], [build("small-1-1", 20)]])
            self.assertEqual(methods, ["listRPMs", "listRPMs"])
            self.assertEqual([c["params"][0]["buildID"] for c in params[0]], [10, 20])
            return response([[[rpm(10, 100), rpm(10, 1000, "src"),
                                rpm(10, 9000, name="large-debuginfo")]],
                             [[rpm(20, 200), rpm(20, 1, "src")]]])

        with patch.object(mod.urllib.request, "urlopen", side_effect=query):
            status, nvrs, stderr = self.listed("--sort", "asc")
        self.assertEqual((status, nvrs), (0, ["small-1-1", "large-1-1"]))
        self.assertIn("Generating build sizes", stderr)
        self.assertEqual(len(calls), 2)
        path = self.downloads / "build-sizes.json"
        before = path.read_bytes()
        self.assertEqual(json.loads(before)["builds"]["large-1-1"]["bytes_by_arch"],
                         {"src": 1000, "x86_64": 100})
        with patch.object(mod.urllib.request, "urlopen") as fetch, \
                patch.object(mod.shutil, "which", return_value=None):
            status, nvrs, _ = self.listed("--sort", "asc", "--no-src")
        self.assertEqual((status, nvrs), (0, ["large-1-1", "small-1-1"]))
        fetch.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_package_filter_limits_missing_lookups(self):
        self.listfile.write_text("excluded-1-1\nwanted-1-1\n")
        with patch.object(mod, "fetch_size_metadata", side_effect=[
                ({"wanted-1-1": 7}, {}), ({}, {7: {"src": 12}})]) as fetch:
            status, nvrs, _ = self.listed("--sort", "desc", "-p", "wanted")
        self.assertEqual((status, nvrs), (0, ["wanted-1-1"]))
        self.assertEqual(fetch.call_args_list[0].args[1][0]["params"], ["wanted-1-1"])

    def test_bulk_query_keeps_cached_selection_and_fills_retired_nvrs(self):
        self.downloads.mkdir()
        tag = self.downloads / "tag-f44.txt"
        before = mod.RPM_TAG_CACHE_HEADER + "latest-1-1\nretired-1-1\n"
        tag.write_text(before)
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        with patch.object(mod, "SIZE_BULK_MIN", 2), \
                patch.object(mod, "fetch_size_metadata", side_effect=[
                    ({"latest-1-1": 1, "unrelated-1-1": 9},
                     {1: {"src": 100}, 9: {"src": 1}}),
                    ({"retired-1-1": 2}, {}), ({}, {2: {"src": 10}})]) as fetch:
            status, nvrs, _ = self.listed("--sort", "asc")
        self.assertEqual((status, nvrs), (0, ["retired-1-1", "latest-1-1"]))
        self.assertEqual(fetch.call_args_list[0].args, ("listTaggedRPMS", "f44"))
        self.assertEqual(fetch.call_args_list[0].kwargs,
                         dict(inherit=True, latest=True, type="rpm", extra=False))
        self.assertEqual(fetch.call_args_list[1].args[1][0]["params"], ["retired-1-1"])
        self.assertEqual(tag.read_text(), before)

    def test_only_missing_entries_are_updated_and_unrelated_entries_survive(self):
        self.listfile.write_text("known-1-1\nnew-1-1\n")
        path = self.cache({"known-1-1": {"src": 10}, "other-1-1": {"src": 20}})
        with patch.object(mod, "fetch_size_metadata", side_effect=[
                ({"new-1-1": 3}, {}), ({}, {3: {"src": 1}})]) as fetch:
            status, nvrs, stderr = self.listed("--sort", "asc")
        self.assertEqual((status, nvrs), (0, ["new-1-1", "known-1-1"]))
        self.assertIn("Updating build sizes for 1", stderr)
        self.assertEqual(fetch.call_args_list[0].args[1][0]["params"], ["new-1-1"])
        self.assertIn("other-1-1", json.loads(path.read_text())["builds"])

    def test_refresh_adds_new_nvrs_without_refetching_known_sizes(self):
        self.cache({"old-1-1": {"src": 100}})
        self.argv = ["-d", str(self.downloads), "--no-run-log"]
        with patch.object(mod, "run", return_value=(0, "old-1-1\nnew-1-1\n")), \
                patch.object(mod, "fetch_size_metadata", side_effect=[
                    ({"new-1-1": 2}, {}), ({}, {2: {"src": 1}})]) as fetch:
            self.assertEqual(self.listed("--sort", "asc", "--refresh")[1],
                             ["new-1-1", "old-1-1"])
        self.assertEqual(fetch.call_args_list[0].args[1][0]["params"], ["new-1-1"])

    def test_invalid_cached_size_is_looked_up_instead_of_sorting_as_zero(self):
        path = self.cache({"demo-1-1": {"src": -1}, "valid-1-1": {"src": 10}})
        with patch.object(mod, "fetch_size_metadata", side_effect=[
                ({"demo-1-1": 8}, {}), ({}, {8: {"src": 12}})]) as fetch:
            self.assertEqual(self.listed("--sort", "asc")[0], 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(json.loads(path.read_text())["builds"]["demo-1-1"]["bytes_by_arch"],
                         {"src": 12})

    def test_incompatible_cache_is_not_trusted(self):
        path = self.cache({"demo-1-1": {"src": 999}})
        original = json.loads(path.read_text())
        for key, value in (("hub", "https://other.example/hub"),
                           ("schema_version", 99), ("exclude_debuginfo", False)):
            with self.subTest(key=key):
                path.write_text(json.dumps({**original, key: value}))
                with patch.object(mod, "fetch_size_metadata", side_effect=[
                        ({"demo-1-1": 8}, {}), ({}, {8: {"src": 1}})]) as fetch:
                    self.assertEqual(self.listed("--sort", "asc")[0], 0)
                self.assertEqual(fetch.call_count, 2)

    def test_write_failure_preserves_cache_manifest_and_rpms(self):
        path = self.cache({"other-1-1": {"src": 10}})
        old_cache = path.read_bytes()
        manifest = self.downloads / "run.json"
        mod.write_run_manifest(manifest, ["demo-1-1"], "old")
        old_manifest = manifest.read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation), \
                    patch.object(mod, "fetch_size_metadata", side_effect=[
                        ({"demo-1-1": 2}, {}), ({}, {2: {"src": 1}})]), \
                    patch.object(mod.os, operation, side_effect=OSError("disk full")), \
                    patch.object(mod, "process_build") as process:
                self.assertEqual(self.run_main("--sort", "asc"), 2)
            process.assert_not_called()
            self.assertEqual(path.read_bytes(), old_cache)
            self.assertEqual(manifest.read_bytes(), old_manifest)
            self.assertEqual(list(self.downloads.glob(".build-sizes-*.tmp")), [])

    def test_interrupted_update_keeps_completed_batches(self):
        self.listfile.write_text("first-1-1\nsecond-1-1\n")
        with patch.object(mod, "SIZE_BATCH", 1), \
                patch.object(mod, "fetch_size_metadata", side_effect=[
                    ({"first-1-1": 1}, {}), ({}, {1: {"src": 10}}), KeyboardInterrupt]), \
                self.assertRaises(KeyboardInterrupt):
            self.listed("--sort", "asc")
        path = self.downloads / "build-sizes.json"
        self.assertEqual(set(json.loads(path.read_text())["builds"]), {"first-1-1"})
        with patch.object(mod, "fetch_size_metadata", side_effect=[
                ({"second-1-1": 2}, {}), ({}, {2: {"src": 1}})]) as fetch:
            self.assertEqual(self.listed("--sort", "asc")[1], ["second-1-1", "first-1-1"])
        self.assertEqual(fetch.call_args_list[0].args[1][0]["params"], ["second-1-1"])

    def test_unknown_or_empty_build_is_an_error_not_zero(self):
        for replies in ([({}, {})], [({"demo-1-1": 1}, {}), ({}, {})]):
            with self.subTest(replies=replies), \
                    patch.object(mod, "fetch_size_metadata", side_effect=replies):
                status, nvrs, _ = self.listed("--sort", "asc")
            self.assertEqual((status, nvrs), (2, []))
            self.assertFalse((self.downloads / "build-sizes.json").exists())

    def test_recovery_filters_after_limit_and_preserves_manifest(self):
        self.listfile.write_text("passed-1-1\nfailed-1-1\nnew-1-1\n")
        self.cache({"passed-1-1": {"src": 1}, "failed-1-1": {"src": 2},
                    "new-1-1": {"src": 3}})
        manifest = self.downloads / "run.json"
        mod.write_run_manifest(manifest, ["passed-1-1", "failed-1-1"], "active")
        before = manifest.read_bytes()
        (self.downloads / "journal.jsonl").write_text("".join(json.dumps(
            {"nvr": nvr, "status": status, "run_id": "active"}) + "\n"
            for nvr, status in (("passed-1-1", "pass"), ("failed-1-1", "test-fail"))))
        for mode in ("--resume", "--retry-failed"):
            with self.subTest(mode=mode):
                self.assertEqual(self.listed(mode, "--sort", "asc", "-n", "1")[1], [])
                self.assertEqual(self.listed(mode, "--sort", "asc", "-n", "2")[1], ["failed-1-1"])
                self.assertEqual(manifest.read_bytes(), before)

    def test_empty_resume_never_loads_size_cache(self):
        mod.write_run_manifest(self.downloads / "run.json", ["demo-1-1"], "active")
        (self.downloads / "journal.jsonl").write_text(json.dumps(
            {"nvr": "demo-1-1", "status": "pass", "run_id": "active"}) + "\n")
        with patch.object(mod, "read_size_cache") as cache, \
                patch.object(mod, "fetch_size_metadata") as fetch:
            self.assertEqual(self.listed("--resume", "--sort", "asc")[0], 0)
        cache.assert_not_called()
        fetch.assert_not_called()

    def test_invalid_manifest_fails_before_size_cache_work(self):
        self.downloads.mkdir()
        (self.downloads / "run.json").write_text('{"run_id":')
        with patch.object(mod, "read_size_cache") as cache, \
                patch.object(mod, "fetch_size_metadata") as fetch:
            self.assertEqual(self.listed("--resume", "--sort", "asc")[0], 2)
        cache.assert_not_called()
        fetch.assert_not_called()

    def test_missing_koji_fails_before_size_lookup_for_downloads(self):
        with patch.object(mod.shutil, "which", return_value=None), \
                patch.object(mod, "read_size_cache") as cache, \
                patch.object(mod, "fetch_size_metadata") as fetch:
            self.assertEqual(self.run_main("--no-test", "--sort", "asc"), 2)
        cache.assert_not_called()
        fetch.assert_not_called()
        self.assertFalse(self.downloads.exists())

    def test_local_sort_uses_selected_files_before_limit_without_metadata(self):
        for name, binary, source in (("a-1-1", 1, 1000), ("b-1-1", 10, 1)):
            directory = self.downloads / name
            directory.mkdir(parents=True)
            (directory / f"{name}.x86_64.rpm").write_bytes(b"x" * binary)
            (directory / f"{name}.src.rpm").write_bytes(b"x" * source)
        for no_src, expected in ((False, "b-1-1"), (True, "a-1-1")):
            with self.subTest(no_src=no_src), \
                    patch.object(mod, "read_size_cache") as cache, \
                    patch.object(mod, "fetch_size_metadata") as fetch:
                args = mod.parse_args(self.argv + ["--test-only", "--sort", "asc", "-n", "1"]
                                      + (["--no-src"] if no_src else []))
                self.assertTrue(all(unit.parent.name == expected for unit in mod.collect_test_units(args)))
            cache.assert_not_called()
            fetch.assert_not_called()

    def test_local_sort_rejects_symlinked_builds_before_testing(self):
        outside = self.root / "outside"
        outside.mkdir()
        rpm_path = outside / "demo-1-1.src.rpm"
        rpm_path.write_text("original")
        self.downloads.mkdir()
        (self.downloads / "demo-1-1").symlink_to(outside, target_is_directory=True)
        with patch.object(mod, "test_one") as test, \
                patch.object(mod, "fetch_size_metadata") as fetch:
            self.assertEqual(self.run_main("--test-only", "--sort", "asc"), 2)
        test.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(rpm_path.read_text(), "original")

    def test_report_records_sort_setting(self):
        self.cache({"demo-1-1": {"src": 1}})
        path = self.root / "result.json"
        with patch.object(mod, "fetch_one", return_value=mod.FetchResult("demo-1-1", True)):
            self.assertEqual(self.run_main("--sort", "desc", "--no-test", "--json", str(path)), 0)
        self.assertEqual(json.loads(path.read_text())["session"]["settings"]["sort"], "desc")
        self.assertIn("sort:      desc", (self.root / "report.txt").read_text())


class SizeMetadataTests(unittest.TestCase):
    def test_retry_discards_partial_aggregates_and_filters_debug_names(self):
        rows = [rpm(1, 100), rpm(1, 200, name="pkg-debugsource"),
                rpm(1, 300, name="pkg-debuginfo-extra"), rpm(1, "4000000000", "src")]
        data = response(rows).getvalue()
        with patch.object(mod.urllib.request, "urlopen", side_effect=[
                io.BytesIO(data[:-20]), io.BytesIO(data)]) as fetch, \
                patch.object(mod.time, "sleep"):
            builds, totals = mod.fetch_size_metadata("listRPMs", buildID=1)
        self.assertEqual(builds, {})
        self.assertEqual(totals, {1: {"x86_64": 100, "src": 4000000000}})
        self.assertEqual(fetch.call_count, 2)

    def test_invalid_sizes_and_rpc_faults_are_not_retried(self):
        for value in (True, -1, "unknown"):
            with self.subTest(value=value), \
                    patch.object(mod.urllib.request, "urlopen", return_value=response([rpm(1, value)])) as fetch, \
                    self.assertRaises(mod.UsageError):
                mod.fetch_size_metadata("listRPMs", buildID=1)
            self.assertEqual(fetch.call_count, 1)
        with patch.object(mod.urllib.request, "urlopen", return_value=response(
                [{"faultCode": 1000, "faultString": "unknown build"}])) as fetch, \
                self.assertRaisesRegex(mod.UsageError, "unknown build"):
            mod.fetch_size_metadata("multiCall", [])
        self.assertEqual(fetch.call_count, 1)

    def test_network_retries_are_bounded_and_interrupts_propagate(self):
        with patch.object(mod.urllib.request, "urlopen", side_effect=OSError("offline")) as fetch, \
                patch.object(mod.time, "sleep") as sleep, self.assertRaises(mod.UsageError):
            mod.fetch_size_metadata("listRPMs", buildID=1)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        with patch.object(mod.urllib.request, "urlopen", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            mod.fetch_size_metadata("listRPMs", buildID=1)

    def test_heartbeat_covers_work_and_routes_list_diagnostics_to_stderr(self):
        args = mod.parse_args(["--list", "--sort", "asc"])
        args.size_heartbeat = 0.01
        seen = threading.Event()

        def log(message, **kwargs):
            self.assertTrue(kwargs["err"])
            if "still generating" in message:
                seen.set()

        with patch.object(mod, "log", side_effect=log):
            with mod.size_progress(args, "generating", 4) as done:
                done[0] = 1
                self.assertTrue(seen.wait(2))
        args.size_heartbeat = 0
        with patch.object(mod.threading, "Thread") as thread, patch.object(mod, "log"):
            with mod.size_progress(args, "updating", 1):
                pass
        thread.assert_not_called()
