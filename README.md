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
  -a ARCH           fetch: download ARCH plus noarch and the source RPM
                    (src unless --no-src; koji matches the arch exactly,
                    so those are added back); test: only test RPMs for
                    ARCH (plus noarch and src). Repeatable. Default: all
                    arches plus the source RPM.
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
      --test-only   skip fetching; test the RPMs already in DIR (the -p
                    and -n filters apply to the downloaded builds)
      --keep        keep per-RPM test work dirs (ext/, ext2/, repacked rpm),
                    logs, and the original RPMs even for passing RPMs
                    (default: removed on a clean pass; kept on failure
                    or warning)

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

A `-p` filter matches source-package names in the tag list: `-p 7zip`
selects the `7zip` build (`7zip-25.01-5.fc44`) — and any other source
package whose name starts with `7zip-`. The `7zip-reduced`,
`7zip-standalone`, and `7zip-standalone-all` entries you see in a build
are subpackages of that one build, not separate selections; they are
fetched and tested along with it. A full NVR (e.g.
`7zip-25.01-5.fc44`) matches exactly.

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
4. `tarpm -t` on both original and repacked, compared in Python
   (`difflib`) — content check, reported as a **warning** (non-fatal) if
   the file lists differ; a listing that fails to generate is a stage
   failure, not a diff

A stage failure stops that RPM's cycle at that stage; the failing stage
(`unpack`, `repack`, `unpack-repacked`, `list-original`, `list-repacked`)
and exit code are recorded. A runner-side exception (e.g. disk full) is
recorded as a test failure with stage `exception` and the exception text.
On a clean pass the work dir, the original RPM, and
the test log are all removed, so a full-tag run leaves only the failures —
each with its RPM, work dir, and log to inspect and re-run (`--keep`
retains everything for passes too). A pass *with a warning* keeps its test
log: it holds the content diff, the only surviving copy. Each RPM gets its
own log under `logs/` (`<rpm-basename>.test.log`), so subpackages of the
same build never clobber each other. Fetch logs are `<NVR>.fetch.log`;
they are removed when the fetch succeeds and kept on failure.

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
└── logs/
    ├── 7zip-25.01-5.fc44.fetch.log     # fetch output (kept on failure)
    └── 7zip-reduced-25.01-5.fc44.x86_64.test.log   # per-RPM test log (kept on failure/warning)
```

The report is written to `./report.txt` by default (the current
directory; `-r` changes it) — it is not inside DIR.

Passed RPMs are removed from their build dir after a successful test
(kept on failure, or with `--keep`); a build dir left empty by that
cleanup is removed as well.

## Report

`report.txt` (path configurable with `-r`) has a summary — per phase
(fetch: selected/ok/failed; test: rpms/ok/failed/warnings, plus how many
passed RPMs were removed) — followed by `FETCH FAILURES` (exit code + last
15 lines of the log), `TEST FAILURES` (stage + exit code — or the
exception text when there is no exit code — + last 20 lines of the log),
and `TEST WARNINGS`.

With `--json FILE`, the same results are written as machine-readable JSON:
per-build fetch results, and per-RPM test results (stage, exit code,
warning, log path, and `error` text when a runner exception was caught).

## Exit status

Ctrl+C (first press) stops gracefully: queued work is dropped, in-flight
work finishes, the partial results are reported, and the exit status is
130 (even if that run also had failures). External commands (koji,
tarpm) run in their own session, so they survive Ctrl+C and finish.
A second Ctrl+C while waiting force-quits (no report) and may orphan
in-flight child processes — they keep running to completion.

| code | meaning                                    |
|------|--------------------------------------------|
| 0    | all fetches and tests succeeded            |
| 1    | one or more fetches/tests failed (report)  |
| 2    | usage / environment error (koji/tarpm not found, bad args, nothing to test) |
| 130  | interrupted by Ctrl+C (partial report)     |

## Roadmap

- stricter checks (e.g. treat content-diff as a failure, header
  comparison), CentOS Stream / AlmaLinux sources (non-Koji backends)
