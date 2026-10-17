# tarpm-tag-runner

Fetch the latest builds of packages from a Fedora Koji tag, and run each
RPM through a **tarpm** test cycle (unpack → repack → unpack the repacked
RPM). Failures are collected in a single report.

## Overview

A single Python script, `tarpm-tag-runner.py` (Python ≥ 3.9, standard
library only, threaded via `concurrent.futures`). The tarpm test runs
**by default** on every fetched RPM, **including the source RPM**
(maximum coverage): `--no-test` fetches only, `--no-src` skips
`.src.rpm`, `--test-only` tests already-downloaded RPMs. The default
run pipelines each build — fetch, test, clean up — with `-j` builds in
flight, so disk use stays bounded to the builds in flight plus the
failures (passed RPMs are removed after their test). Results are
written to a single text report, plus an optional `--json FILE`.

## Requirements

- Python ≥ 3.9 (standard library only)
- a configured `koji` client (Fedora hub), e.g. `dnf install koji` — the
  same one fedpkg ships
- `tarpm` (the test phase runs by default):
  ```bash
  dnf copr enable dcantrell/tarpm
  dnf install tarpm
  ```

## Usage

```
python3 tarpm-tag-runner.py [options]

Fetch:
  -t TAG            Koji tag to query (default: f44)
  -p PKG            include builds whose NVR equals PKG or starts with PKG-
                    (repeatable; default: every package in the tag)
  -a ARCH           fetch: restrict "koji download-build" to ARCH;
                    test: only test RPMs for ARCH (plus noarch and src).
                    Repeatable. Default: all arches plus the source RPM.
  -d DIR            download directory (default: ./rpms); each build goes
                    to DIR/<NVR>/
  -n NUM            only process the first NUM builds (smoke test)
      --list        print the NVRs that would be fetched and exit
                    without downloading
      --from-file FILE
                    read the NVR list from FILE (same format as
                    'koji latest-pkg --all TAG') instead of the cached
                    list or Koji
      --refresh     re-fetch the tag list from Koji, even if a cached
                    copy (DIR/tag-<TAG>.txt) exists

Test (tarpm round-trip: unpack -> repack -> unpack repacked; runs by
     default on every fetched RPM, including the source RPM):
      --no-test     fetch only; skip the tarpm test phase
      --no-src      skip .src.rpm files (tested by default)
      --test-only   skip fetching; test the RPMs already in DIR
      --keep        keep per-RPM test work dirs (ext/, ext2/, repacked rpm)
                    and the original RPMs even for passing RPMs (default:
                    kept only on failure)

Common:
  -r FILE           report file (default: ./report.txt)
  -j N              number of parallel fetches/tests (default: 1)
      --json FILE   also write machine-readable results to FILE (JSON)
  -v                verbose: echo each external command
  -h, --help        show this help and exit
```

### Examples

```bash
# See what would be fetched, without downloading anything:
python3 tarpm-tag-runner.py --list -t f44 -p 7zip

# Fetch those builds (4 in parallel); the tarpm test runs by default:
python3 tarpm-tag-runner.py -t f44 -j 4 -p 7zip -p 389-ds-base

# Same, with a JSON results file next to the text report:
python3 tarpm-tag-runner.py -t f44 -j 4 -p 7zip --json results.json

# Fetch only, without testing:
python3 tarpm-tag-runner.py -t f44 -j 4 -p 7zip --no-test

# Test, but skip the source RPM:
python3 tarpm-tag-runner.py -t f44 -j 4 -p 7zip --no-src

# Smoke test: first 3 packages of the whole tag, x86_64 only:
python3 tarpm-tag-runner.py -t f44 -a x86_64 -n 3

# Re-run the tests only, on RPMs already downloaded (e.g. after a tarpm
# update), x86_64 + noarch, 8 in parallel:
python3 tarpm-tag-runner.py --test-only -a x86_64 -j 8
```

A `-p` filter is a package-name prefix: `-p 7zip` selects `7zip`,
`7zip-reduced`, `7zip-standalone`, `7zip-standalone-all`, but not
`7zipfoo`. Passing a full NVR (e.g. `7zip-25.01-5.fc44`) matches exactly.

The tag list is cached as `DIR/tag-<TAG>.txt` (raw `koji latest-pkg
--all` output). Later runs for the same tag reuse it instead of
querying Koji — re-testing the exact same build set, and avoiding
repeated tag fetches. `--refresh` forces a re-fetch; `--from-file FILE`
reads an explicit list (same format).

## The tarpm test cycle

For each fetched RPM (the test is the default; `--no-test` skips it;
`.src.rpm` is included by default, `--no-src` skips it):

1. `tarpm -x -f in.rpm -O <work>/ext` — unpack
2. `tarpm -c -f <work>/<base>-repacked.rpm <work>/ext` — repack
3. `tarpm -x -f <repacked.rpm> -O <work>/ext2` — unpack the repacked RPM
4. `tarpm -t` on both original and repacked + `diff` — content check,
   reported as a **warning** (non-fatal) if the file lists differ

A stage failure stops that RPM's cycle at that stage; the failing stage and
exit code are recorded. Work dirs and original RPMs are kept for failures
(and for passes with `--keep`), so you can inspect what tarpm saw and
re-run the test. Passing RPMs are removed from `DIR/<NVR>/` after a
successful test, so a full-tag run leaves only the failures (plus the
per-RPM test logs and the cached tag list). Each RPM gets its own test log
(`<rpm-basename>.test.log` under `logs/`), so subpackages of the same
build never clobber each other's logs.

## Layout

```
rpms/
├── 7zip-25.01-5.fc44/
│   ├── 7zip-25.01-5.fc44.src.rpm        # koji download-build output
│   ├── 7zip-25.01-5.fc44.x86_64.rpm
│   ├── 7zip-reduced-25.01-5.fc44.x86_64.rpm   # subpackages of the build
│   ├── ...
│   └── .tarpm-<base>/                   # test work dir (kept on failure
│       ├── ext/                         #   or with --keep)
│       ├── <base>-repacked.rpm
│       ├── ext2/
│       ├── list-orig.txt
│       └── list-repacked.txt
├── logs/
│   ├── 7zip-25.01-5.fc44.log           # stdout+stderr of the fetch
│   └── 7zip-reduced-25.01-5.fc44.x86_64.test.log   # per-RPM test log
└── report.txt                           # single summary + per-failure detail
```

Passed RPMs are removed from their build dir after a successful test
(kept on failure, or with `--keep`); a build dir left empty by that
cleanup is removed as well.

## Report

`report.txt` (path configurable with `-r`) has a summary — per phase
(fetch: selected/ok/failed; test: rpms/ok/failed/warnings, plus how many
passed RPMs were removed) — followed by `FETCH FAILURES` (exit code + last
15 lines of the log), `TEST FAILURES` (stage + exit code + last 20 lines of
the log), and `TEST WARNINGS`.

With `--json FILE`, the same results are written as machine-readable JSON:
per-build fetch results, and per-RPM test results (stage, exit code,
warning, log path).

## Exit status

Ctrl+C (first press) stops gracefully: queued work is dropped, in-flight
work finishes, the partial results are reported, and the exit status is
130. A second Ctrl+C while waiting force-quits (no report).

| code | meaning                                    |
|------|--------------------------------------------|
| 0    | all fetches and tests succeeded            |
| 1    | one or more fetches/tests failed (report)  |
| 2    | usage / environment error (koji/tarpm not found, bad args, nothing to test) |
| 130  | interrupted by Ctrl+C (partial report)     |

## Roadmap

- stricter checks (e.g. treat content-diff as a failure, header
  comparison), CentOS Stream / AlmaLinux sources (non-Koji backends)
