#!/usr/bin/env python3
"""Exercise failure recovery and the real Fedora hello RPM round-trip.

Requires installed koji/tarpm clients and network access. Reports and logs
remain under --output, including after failures.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "tarpm-tag-runner.py"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new directory for reports and logs")
    parser.add_argument("--tag", help="Koji tag to test (uses the runner default if omitted)")
    parser.add_argument("--arch", default=platform.machine(),
                        help="binary RPM architecture (default: host architecture)")
    args = parser.parse_args()
    for tool in ("koji", "tarpm"):
        require(shutil.which(tool), f"required tool not found: {tool}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    downloads = output / "rpms"
    journal_path = downloads / "journal.jsonl"

    def invoke(phase, *options, directory=downloads, arches=(args.arch,),
               expected_status=0, env=None):
        json_path = output / f"{phase}.json"
        command = [sys.executable, str(RUNNER), "-p", "hello",
                   "-d", str(directory), "--heartbeat", "15",
                   "-r", str(output / f"{phase}.txt"), "--json", str(json_path)]
        if args.tag is not None:
            command += ["-t", args.tag]
        for arch in arches:
            command += ["-a", arch]
        print(f"\n=== {phase} ===", flush=True)
        result = subprocess.run([*command, *options], timeout=600, env=env)
        require(result.returncode == expected_status,
                f"{phase} exited {result.returncode}, expected {expected_status}")
        return json.loads(json_path.read_text())

    def journal():
        records = [json.loads(line) for line in journal_path.read_text().splitlines()]
        return {record["nvr"]: record for record in records}

    def check_cleanup(directory):
        require(not list(directory.glob(".build-*")) and not list(directory.rglob("*.rpm")),
                f"build artifacts leaked under {directory}")

    with tempfile.TemporaryDirectory(prefix="ttr-smoke-tool-") as tools:
        failing_tarpm = Path(tools) / "tarpm"
        failing_tarpm.write_text(
            '#!/bin/sh\nprintf "tarpm: simulated unpack failure\\n" >&2\nexit 97\n')
        failing_tarpm.chmod(0o755)
        search_path = os.environ.get("PATH", os.defpath)
        failed = invoke("first-attempt", "--sort", "asc", expected_status=1,
                        env={**os.environ, "PATH": f"{tools}{os.pathsep}{search_path}"})
    require(failed["fetch"] and all(row["ok"] for row in failed["fetch"]),
            "hello download failed before the simulated test failure")
    require(failed["test"] and all(row["stage"] == "unpack" and row["exit_code"] == 97
                                  for row in failed["test"]),
            "the first attempt must record the simulated unpack failures")
    require(all(Path(row["log"]).is_file() for row in failed["test"]),
            "failure diagnostics must survive cleanup")
    check_cleanup(downloads)

    manifest = json.loads((downloads / "run.json").read_text())
    require(set(manifest["nvrs"]) == {row["nvr"] for row in failed["fetch"]},
            "run manifest must capture the complete selection")
    require(all(row["status"] == "test-fail" and row["run_id"] == manifest["run_id"]
                for row in journal().values()), "failures must belong to the active run")
    size_path = downloads / "build-sizes.json"
    size_snapshot = size_path.read_bytes()
    sizes = json.loads(size_snapshot)
    require(sizes["schema_version"] == 1 and sizes["exclude_debuginfo"]
            and all({"src", args.arch} <= sizes["builds"][nvr]["bytes_by_arch"].keys()
                    for nvr in manifest["nvrs"]),
            "size cache must retain source and binary architecture totals")

    resumed = invoke("resume-and-test", "--resume", "--sort", "desc")
    require(size_path.read_bytes() == size_snapshot,
            "fully cached size sorting must preserve the snapshot")
    require(resumed["test"] and all(row["ok"] for row in resumed["test"]),
            "resume must round-trip the failed hello builds")
    require(resumed["run"]["id"] == manifest["run_id"] and resumed["run"]["pending_builds"] == 0,
            "resume must complete the active run")
    require(all(row["status"] in ("pass", "warn") for row in journal().values()),
            "the final journal records must show completed tests")
    require(resumed["run"]["attempts"][0]["test_failed"] == sorted(manifest["nvrs"]),
            "attempt history must preserve the initial failures")
    check_cleanup(downloads)

    session = resumed["session"]
    require(session["runtime_seconds"] > 0 and session["status"] == "passed",
            "session must record runtime and completion status")
    require(not session["tarpm"]["error"] and session["tarpm"]["rpm_info"]
            and session["tarpm"]["package"].get("release"),
            "report must identify the installed tarpm RPM build")
    require(session["tarpm"]["rpm_info"] in (output / "resume-and-test.txt").read_text(),
            "text report must retain the RPM build metadata")
    expected_stages = {"unpack", "repack", "unpack-repacked", "list-original", "list-repacked"}
    require(expected_stages <= resumed["timings"].keys(),
            "summary must include all five command timings")
    require({"src", args.arch} <= {row["arch"] for row in resumed["test"]},
            "resume must test source and binary RPMs")
    run_log = (downloads / "run.log").read_text()
    for row in resumed["test"]:
        require(expected_stages == row["stage_seconds"].keys()
                and all(seconds > 0 for seconds in row["stage_seconds"].values())
                and row["duration_seconds"] >= sum(row["stage_seconds"].values()),
                "each passed RPM must retain all stage timings and its duration")
        rpm = Path(row["rpm"])
        require(f": testing {rpm.stem} (" not in run_log,
                f"unexpected stage-start noise in run log: {rpm.stem}")
        require(f"[test]   {rpm.stem} ok" in run_log,
                f"test completion missing from run log: {rpm.stem}")

    before = journal_path.read_bytes()
    skipped = invoke("resume-skip", "--resume")
    require(skipped["summary"] == resumed["summary"]
            and skipped["run"]["id"] == resumed["run"]["id"]
            and skipped["run"]["attempts"] == resumed["run"]["attempts"]
            and skipped["session"]["selected_builds"] == 0
            and journal_path.read_bytes() == before,
            "completed resume must rebuild cumulative reports with the saved results")

    no_src_dir = output / "no-src-rpms"
    no_src = invoke("no-src", "--no-src", "--compare", str(output / "resume-and-test.json"),
                    directory=no_src_dir, arches=())
    require(no_src["test"] and all(row["ok"] and row["arch"] != "src" for row in no_src["test"]),
            "--no-src must only test binary RPMs")
    check_cleanup(no_src_dir)
    comparison = no_src["comparison"]["test"]
    require(comparison["rechecked"] >= 1
            and comparison["not_rechecked"] == sum(row["arch"] == "src" for row in resumed["test"])
            and not comparison["regressions"] and not comparison["resolved"],
            "binary tests must compare with the baseline and leave sources unverified")
    print(f"\nhello smoke tests passed. Reports and logs are in {output}", flush=True)


if __name__ == "__main__":
    main()
