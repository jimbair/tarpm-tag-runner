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
                    'koji latest-pkg --all TAG'; blank lines and
                    '#' comments ignored) instead of the cached
                    list or Koji
      --refresh     re-fetch the tag list from Koji, even if a cached
                    copy (DIR/tag-<TAG>.txt) exists

Test (tarpm round-trip: unpack -> repack -> unpack repacked; runs by
     default on every fetched RPM, including the source RPM):
      --no-test     fetch only; skip the tarpm test phase
      --no-src      skip testing .src.rpm files; discard any sources
                    after a successful fetch (tested by default)
      --test-only   skip fetching; test the RPMs already in DIR (the -p
                    and -n filters apply to the downloaded builds)
      --keep        keep per-RPM test work dirs (ext/, ext2/, repacked rpm),
                    logs, and the original RPMs even for passing RPMs
                    (default: passing RPMs and work dirs removed;
                    warning logs retained)

Common:
  -r FILE           report file (default: ./report.txt)
      --run-log FILE
                    append the run log (every log line, plus one header
                    line per invocation) to FILE (default: DIR/run.log)
      --no-run-log  do not write a run log
  -j N              number of parallel fetches/tests (default: 1)
      --heartbeat N
                    while a command runs longer than this many seconds,
                    log a progress line every N seconds so a slow command
                    does not look stuck (0 disables; default: 60)
      --json FILE   also write machine-readable results to FILE (JSON)
      --resume      skip builds whose latest journal record
                    (DIR/journal.jsonl) is pass or warn
      --retry-failed
                    re-run only the builds whose latest journal record is
                    fetch-fail or test-fail; each starts from a clean
                    DIR/<NVR>/. Default report:
                    report-retry-YYYYmmdd-HHMMSS.txt
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

# Re-run exactly the builds that failed in the last run (the failure
# list is plain NVRs, so this works even without the local journal):
python3 tarpm-tag-runner.py --from-file rpms/logs/failed-all.txt -j 4
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

A stage failure stops that RPM's cycle at that stage. The failing stage
and exit code are recorded, along with the error signature of the failing
output (see Error signatures). The stage names, as they appear in reports,
the journal, and the `failed-<stage>.txt` lists:

| stage             | command                         | fails when                                  |
|-------------------|---------------------------------|---------------------------------------------|
| `unpack`          | `tarpm -x` on the original RPM  | the original cannot be unpacked             |
| `repack`          | `tarpm -c` on the unpacked tree | the tree cannot be repacked into an RPM     |
| `unpack-repacked` | `tarpm -x` on the repacked RPM  | the repacked RPM cannot be unpacked         |
| `list-original`   | `tarpm -t` on the original RPM  | the listing exits non-zero                  |
| `list-repacked`   | `tarpm -t` on the repacked RPM  | the listing exits non-zero                  |

A runner-side exception (e.g. disk full) is recorded as a test failure
with stage `exception` and the exception text; a fetch failure has stage
`fetch`.

### What is left on disk

On a clean pass the work dir, the original RPM, and the test log are all
removed, so a full-tag run leaves only the failures (`--keep` retains
everything for passes too). For a failed RPM, two different things remain
and it matters which is which:

- `DIR/<NVR>/<base>.rpm` is the **original** koji download. The runner
  never modifies it; it is deleted after a pass, including warnings,
  unless --keep is used.
- `DIR/<NVR>/.tarpm-<base>/` holds the **test artifacts** of the last
  attempt: `ext/`, the repacked RPM, `ext2/`, and the two listings. Which
  of them exist depends on how far the cycle got. Each new test of that
  RPM wipes and rebuilds this directory.

The original is not guaranteed to be complete if the run was killed
mid-fetch (see Exit status), so a re-run does not trust files left on
disk: `--retry-failed` and `--resume` start each build from a fresh
download (see Journal, resume, and retry). Use `--test-only` only when you
deliberately want to re-test the RPMs already on disk, for example right
after a tarpm update on a run that finished cleanly.

A pass *with a warning* keeps its test log: it holds the content diff, the
only surviving copy. Each RPM gets its own log under `logs/`
(`<rpm-basename>.test.log`), so subpackages of the same build never
clobber each other. Fetch logs are `<NVR>.fetch.log`; they are removed
when the fetch succeeds and kept on failure.

## Layout

```
rpms/
├── 7zip-25.01-5.fc44/
│   ├── 7zip-25.01-5.fc44.src.rpm           # original koji download
│   ├── 7zip-25.01-5.fc44.x86_64.rpm        # (removed after a clean pass)
│   ├── ...                                 # more RPMs of the build
│   └── .tarpm-<base>/                      # test artifacts, last attempt
│       ├── ext/
│       ├── <base>-repacked.rpm
│       ├── ext2/
│       ├── list-orig.txt
│       └── list-repacked.txt
├── journal.jsonl                           # one JSON line per build
├── run.log                                 # tee of every log line
└── logs/
    ├── 7zip-25.01-5.fc44.fetch.log         # kept on fetch failure
    ├── 7zip-25.01-5.fc44.x86_64.test.log   # kept on failure or warning
    ├── failed-all.txt                      # failing build NVRs
    ├── failed-fetch.txt                    # fetch failures
    ├── failed-<stage>.txt                  # one per failing stage
    ├── failed-sig-<slug>-<hash>.txt        # top 20 error signatures
    └── failed-index.tsv                    # stage, signature, nvr, rpm
```

The `.rpm` files are the unmodified originals; `.tarpm-<base>/` holds the
test artifacts, kept on failure or with `--keep` and rebuilt by each new
test (see "What is left on disk"). The `failed-*` lists describe the most
recent report (see "Failure lists").

The report is written to `./report.txt` by default (the current
directory; `-r` changes it) — it is not inside DIR. Its header also
records the run log path (`run log:`, or `- (disabled)`).

## Run log

By default every log line is tee'd to `DIR/run.log` in append mode: each
invocation adds one header line — `=== <PROG> start <UTC>: <argv> ===` —
followed by that run's log lines, so several runs stack in one file. Each
line is one locked write+flush, so `-j` runs never interleave lines.
`--run-log FILE` chooses a different file, `--no-run-log` disables it
entirely. Force-quit messages and uncaught tracebacks are written to it
as well. An unopenable or unwritable run log never aborts the run — it
simply proceeds without one.

## Journal, resume, and retry

Every finished build appends one JSON line to `DIR/journal.jsonl` (one
`os.write()` under a lock per line; the reader skips a torn or invalid
last line, and the latest record per NVR wins). Fields: `nvr`, `status`
(`fetched`, `pass`, `warn`, `fetch-fail`, `test-fail`), `stage`, `exit_code`,
`signature` (see Error signatures), `rpms` (failing RPM basenames; for
`warn`, the warned ones), and `ts` (UTC). Records are written as each
build finishes, so an interrupted or crashed run still keeps the records
of the builds that completed; a build that did not finish gets no
record.

`fetched` means download succeeded without any completed tests (including
`--no-test`); `--resume` still tests these builds. `--test-only` does not
update the journal. A torn final line is separated before the next append,
so it does not consume the new record.

- `--resume` skips builds whose latest record is `pass` or `warn`, so an
  interrupted run can be continued without re-testing what already
  passed. Each selected build starts from a clean download directory.
- `--retry-failed` re-runs only the builds whose latest record is
  `fetch-fail` or `test-fail`, each from a clean `DIR/<NVR>/`, through
  the normal fetch-and-test pipeline (`-j`, `-a`, `--no-src`, `-n`, and
  `-p` all still apply). Its report goes to
  `report-retry-YYYYmmdd-HHMMSS.txt` unless `-r` is given.
- If nothing is left to do, the run exits 0 with a message.

`--resume` and `--retry-failed` are mutually exclusive, and neither may
be combined with `--test-only`, `--refresh`, or `--no-test` (exit 2).

Typical uses:

```bash
# A run was killed, crashed, or stopped with Ctrl+C: run the same command
# again with --resume. Builds that finished as pass/warn are skipped;
# builds that never ran, were in flight, or failed are (re)done.
python3 tarpm-tag-runner.py -t f44 -j 2 --resume

# After installing a newer tarpm: re-run only what failed before, each
# build from a fresh download.
python3 tarpm-tag-runner.py -t f44 -j 2 --retry-failed
```

Things to know:

- `--resume` also re-runs earlier failures (it skips only `pass` and
  `warn`), so after a crash the failures from the part that finished
  before the crash are tested again, and the report and `failed-*` lists
  of the resumed run still cover every outstanding failure.
- The report and failure lists describe only the invocation that wrote
  them. A build that warned before the crash is skipped on `--resume`, so
  it does not appear in the resumed run's `TEST WARNINGS`; its record in
  `journal.jsonl` and its test log are still there.
- Both options select from the same build list as a normal run (the tag
  list, `--from-file`, `-p`, `-n`), so a journal NVR that is not in that
  list is not retried. To retry a set of NVRs on another machine, use a
  failure list with `--from-file` (see Failure lists).
- Records are per build, so a retry re-downloads and re-tests every RPM of
  a failed build, not just the one that failed.

## Failure lists

Whenever a report is written, the run's failures are also written as
plain NVR lists under `DIR/logs/`. The files always describe the most
recent run that wrote a report: they are overwritten when that run had
failures, and removed when it did not, so they never describe a stale
run. Each list has no header lines: one build NVR per line, sorted and
deduplicated, each optionally followed by `  # detail` (the failing
stage, exit code, and signature):

- `failed-all.txt` — every build with a fetch or test failure
- `failed-fetch.txt` — the fetch failures
- `failed-<stage>.txt` — one per failing stage (e.g.
  `failed-repack.txt`, `failed-unpack-repacked.txt`,
  `failed-list-original.txt`)
- `failed-sig-<slug>-<hash>.txt` — one per error signature, top 20 by
  failure count (the 6-hex-char hash keeps distinct signatures from
  colliding)
- `failed-index.tsv` — one row per failure:
  `stage<TAB>signature<TAB>nvr<TAB>rpm` (fetch failures leave the rpm
  column empty)

The lists feed straight back to `--from-file` (it takes the first
token of each line and ignores blank lines and `#` comments), so a set
of failures can be re-run on demand — even on a machine without the
local journal:

```bash
python3 tarpm-tag-runner.py --from-file rpms/logs/failed-all.txt -j 4
```

Passed RPMs are removed from their build dir after a successful test
(kept on failure, or with `--keep`); a build dir left empty by that
cleanup is removed as well.

## Report

`report.txt` (path configurable with `-r`) has a summary — per phase
(fetch: selected/ok/failed; test: rpms/ok/failed/warnings, plus how many
passed RPMs were removed) — followed by `FETCH FAILURES` (exit code + last
15 lines of the log), `TEST FAILURES` (stage + exit code — or the
exception text when there is no exit code — + last 20 lines of the log),
`TEST WARNINGS`, and failure-type rollups: `FETCH FAILURES BY TYPE` and
`TEST FAILURES BY TYPE` group the failures by (stage, exit code, error
signature), most common first, with up to 3 example NVRs per group. The
same rollup, limited to the top 5 types, is printed to the console at
the end of the run.

With `--json FILE`, the same results are written as machine-readable JSON:
per-build fetch results, and per-RPM test results (stage, exit code,
warning, log path, `error` text when a runner exception was caught, and
the `signature` of the failing output).

## Error signatures

Each failed fetch or test records a `signature`: the last `tarpm: ` line
of the failing command's output (prefix stripped), or the last non-empty
line when there is no such line — with absolute paths replaced by
`<path>`, hex addresses and 4+ digit runs replaced by `N`, truncated to
120 characters. The same failure therefore gets the same signature
regardless of which RPM, path, or address it hit; that is what the
`... BY TYPE` report sections and the console top-5 summary group by.

The pure `error_signature()` helper is covered by unit tests:

    python3 test-tarpm-tag-runner.py

(stdlib `unittest`; no koji, tarpm, or network needed).

## Slow commands, disk, and the full run

A few builds are huge. `0ad-data-0.28.0-2.fc44`, for instance, ships a
~1.5 GiB `.noarch.rpm` and a ~1.4 GiB `.src.rpm`; fetching them took over
10 minutes and the tarpm round-trip of the 1.5 GiB RPM ran for 10+ minutes.
Two consequences for a full-tag run:

- **Heartbeats.** When a `koji` or `tarpm` command runs longer than
  `--heartbeat N` seconds (default 60; `0` disables), a progress line is
  logged every N seconds so a slow command does not look stuck:

      [ts] tarpm-tag-runner: still fetching 0ad-data-0.28.0-2.fc44 (12/20) (10m 02s, 2.2 GiB so far)
      [ts] tarpm-tag-runner: still testing 0ad-data-0.28.0-2.fc44.noarch (unpack) (1/2) (10m 02s)

  Completion lines carry the same facts, so a finished run shows what was
  slow:

      [ts] [fetch]  0ad-data-0.28.0-2.fc44 ok (2 RPMs, 2.9 GiB, 10m 22s)
      [ts] [test]   0ad-data-0.28.0-2.fc44.noarch ok (1/2, 15m 05s)

  (`[ts]` is the UTC timestamp; the lines above are the exact formats.)
- **Disk.** A round-trip keeps the original RPM, the unpacked tree, the
  repacked RPM, and the second unpack on disk at once. A 1.5 GiB RPM can
  need well over 10 GiB in flight, and `-j N` multiplies that by N. Run the
  full ~23,000-build tag with a low `-j` (1–2) on a disk large enough for
  its biggest builds.

## Exit status

Ctrl+C (first press) stops gracefully: queued work is dropped, in-flight
work finishes, the partial results are reported, and the exit status is
130 (even if that run also had failures). External commands (koji,
tarpm) run in their own session, so they survive Ctrl+C and finish.
Builds that finished before the interrupt are in the journal, so
`--resume` continues from there.

A second Ctrl+C while waiting force-quits immediately (no report, no
cleanup). The in-flight koji/tarpm processes are orphaned, and their
output pipe is gone: observed behavior is that they die on their next
write to it (a `BrokenPipeError`, exit 120) rather than finishing. That
can leave a truncated RPM (`rpm -K` reports `DIGESTS NOT OK`) or a partial
work dir. Do not trust files from a force-quit run: continue with
`--resume`, or delete the unfinished `DIR/<NVR>/` directories before
re-running. `--retry-failed` covers recorded failures only; it skips
unfinished builds that have no failure record.

| code | meaning                                    |
|------|--------------------------------------------|
| 0    | all fetches and tests succeeded, or nothing to do (`--resume` / `--retry-failed`) |
| 1    | one or more fetches/tests failed (report)  |
| 2    | usage / environment error (koji/tarpm not found, bad args, nothing to test) |
| 130  | interrupted by Ctrl+C (partial report)     |

## Roadmap

- stricter checks (e.g. treat content-diff as a failure, header
  comparison), CentOS Stream / AlmaLinux sources (non-Koji backends)
