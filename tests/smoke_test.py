#!/usr/bin/env python3
"""Exercise the real Koji/tarpm pipeline with Fedora's small hello build.

Requires installed koji/tarpm clients and network access. Reports and test
artifacts remain under --output for inspection, including after failures.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "tarpm-tag-runner.py"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new directory for reports and artifacts")
    parser.add_argument("--tag", default="f44")
    args = parser.parse_args()
    for tool in ("koji", "tarpm"):
        require(shutil.which(tool), f"required tool not found: {tool}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    downloads = output / "rpms"

    def invoke(phase, *options, directory=downloads, arches=("x86_64",)):
        json_path = output / f"{phase}.json"
        command = [sys.executable, str(RUNNER), "-t", args.tag, "-p", "hello",
                   "-d", str(directory), "--heartbeat", "15",
                   "-r", str(output / f"{phase}.txt"), "--json", str(json_path)]
        for arch in arches:
            command += ["-a", arch]
        command.extend(options)
        print(f"\n=== {phase} ===", flush=True)
        subprocess.run(command, check=True, timeout=600)
        return json.loads(json_path.read_text()) if json_path.exists() else None

    def journal():
        records = [json.loads(line) for line in
                   (downloads / "journal.jsonl").read_text().splitlines()]
        return {record["nvr"]: record for record in records}

    fetched = invoke("fetch-only", "--no-test")
    require(fetched and fetched["fetch"] and not fetched["test"],
            "fetch-only must download builds without testing")
    require(all(row["ok"] for row in fetched["fetch"]), "hello download failed")
    require(all(row["status"] == "fetched" for row in journal().values()),
            "fetch-only builds must remain eligible for resume")
    manifest = json.loads((downloads / "run.json").read_text())
    require(set(manifest["nvrs"]) == {row["nvr"] for row in fetched["fetch"]},
            "run manifest must capture the complete selection")
    require(all(row.get("run_id") == manifest["run_id"] for row in journal().values()),
            "journal records must belong to the active run")
    require(list(downloads.glob("*/*.src.rpm")), "source RPM was not fetched")

    # A resumed build must discard everything left by an earlier attempt.
    sentinels = []
    for row in fetched["fetch"]:
        sentinel = downloads / row["nvr"] / "stale-from-interrupt"
        sentinel.write_text("must be removed before fetching")
        sentinels.append(sentinel)
    resumed = invoke("resume-and-test", "--resume", "--keep")
    require(resumed and resumed["test"] and all(row["ok"] for row in resumed["test"]),
            "resume must round-trip the fetched hello RPMs")
    require({"src", "x86_64"} <= {row["arch"] for row in resumed["test"]},
            "resume must test source and binary RPMs")
    require(all(not sentinel.exists() for sentinel in sentinels),
            "resume reused a stale build directory")
    require(all(row["status"] in ("pass", "warn") for row in journal().values()),
            "completed tests must be journaled")
    require(json.loads((downloads / "run.json").read_text())["run_id"] == manifest["run_id"],
            "resume must preserve the active run ID")
    run_log = (downloads / "run.log").read_text()
    for row in resumed["test"]:
        rpm = Path(row["rpm"])
        for stage in ("unpack", "repack", "unpack-repacked", "list-original", "list-repacked"):
            require(f"testing {rpm.stem} ({stage})" in run_log,
                    f"stage start missing from run log: {rpm.stem} ({stage})")
        work = rpm.parent / f".tarpm-{rpm.stem}"
        for artifact in (rpm, work / "ext", work / "ext2",
                         work / f"{rpm.stem}-repacked.rpm",
                         work / "list-orig.txt", work / "list-repacked.txt"):
            require(artifact.exists(), f"--keep did not preserve {artifact}")

    before = journal()
    skipped = invoke("resume-skip", "--resume")
    require(skipped is None and journal() == before,
            "resume should skip tested builds without writing new results")

    local = invoke("test-only", "--test-only", "--no-src", "-j", "2")
    require(local and not local["fetch"] and local["test"],
            "test-only must test downloaded binaries")
    require(all(row["ok"] and row["arch"] != "src" for row in local["test"]),
            "test-only failed or tested an excluded source RPM")
    require(all(not Path(row["rpm"]).exists() for row in local["test"]),
            "passing RPMs must be removed without --keep")
    require(list(downloads.glob("*/*.src.rpm")),
            "test-only must leave skipped source RPMs alone")
    require(journal() == before, "test-only must not update the build journal")

    # Exercise --no-src without -a, where Koji downloads all architectures.
    no_src_dir = output / "no-src-rpms"
    no_src = invoke("no-src", "--no-test", "--no-src", "--keep",
                    directory=no_src_dir, arches=())
    require(no_src and no_src["fetch"] and all(row["ok"] for row in no_src["fetch"]),
            "--no-src download failed")
    require(list(no_src_dir.glob("*/*.rpm")), "binary RPMs were not retained")
    require(not list(no_src_dir.glob("*/*.src.rpm")),
            "--no-src retained excluded source downloads")
    print(f"\nhello smoke tests passed; artifacts: {output}", flush=True)


if __name__ == "__main__":
    main()
