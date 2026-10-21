# tarpm-tag-runner

Fetch the latest builds from a Fedora Koji tag and test each downloaded RPM
with **tarpm**: unpack → repack → unpack again → compare file listings.
Tests include source RPMs by default. Failures go into one report; listing
differences are non-fatal warnings.

The runner processes up to `-j` builds at once, downloading and testing each
build before cleaning it up. Passed RPMs are deleted by default, keeping
disk use to active builds plus retained failures. Use a low `-j` for a full
tag: a single 1.5 GiB RPM can need over 10 GiB during its round-trip.
`--keep` and `--no-test` retain downloads and therefore need more disk.

## Setup and quick start

Requires Python ≥ 3.9 (standard library only), a `koji` client configured for
the Fedora hub, and `tarpm`. On Fedora with DNF5:

```bash
sudo dnf install python3 koji dnf5-plugins
sudo dnf copr enable dcantrell/tarpm
sudo dnf install tarpm
```

```bash
# Preview a small selection without downloading RPMs:
python3 tarpm-tag-runner.py -t f44 -p hello --list

# Fetch and test hello, including its source RPM; keep all artifacts:
python3 tarpm-tag-runner.py -t f44 -p hello -a x86_64 --keep

# Test two source-package selections, four builds at once:
python3 tarpm-tag-runner.py -t f44 -p 7zip -p 389-ds-base -j 4 --json results.json

# Download only, then test the local RPMs separately:
python3 tarpm-tag-runner.py -p hello -a x86_64 --no-test
python3 tarpm-tag-runner.py --test-only -p hello -a x86_64
```

## Options and build selection

Run `python3 tarpm-tag-runner.py --help` for CLI help. Defaults: tag `f44`,
download directory `./rpms`, report `./report.txt`, one worker, all binary
architectures plus source RPMs. Koji's default debuginfo exclusion applies.

| Option | Effect |
| --- | --- |
| `-t TAG` | Koji tag to query. |
| `-p PKG` | Select NVRs equal to `PKG` or starting with `PKG-`; repeatable. |
| `-a ARCH` | Select this arch plus `noarch` and `src` (unless `--no-src`); repeatable. |
| `-d DIR` | Put each build in `DIR/<NVR>/`. |
| `-n NUM` | Process the first NUM builds; `0` means no limit. |
| `--list` | Print selected NVRs and exit without downloading RPMs. |
| `--from-file FILE` | Read a build list instead of querying Koji or using the cache. |
| `--refresh` | Replace the cached tag list with a fresh Koji query. |
| `--no-test` | Download only; tarpm is not required. |
| `--no-src` | Skip source tests and discard source RPMs after a successful fetch. |
| `--test-only` | Test local RPMs; `-p`, `-n`, `-a`, and `--no-src` still apply. |
| `--keep` | Retain originals, test work directories, and logs even for passes. |
| `-j N` | Parallel builds in normal runs; parallel RPMs in `--test-only`. |
| `-r FILE` | Text report path. |
| `--json FILE` | Also write per-build fetch and per-RPM test results as JSON. |
| `--run-log FILE` | Append runner log lines to FILE; default `DIR/run.log`. |
| `--no-run-log` | Disable the run log. |
| `--heartbeat N` | Log progress every N seconds during external commands; default 60, `0` disables. |
| `--resume` | Skip journaled test passes/warnings; download and test the rest afresh. |
| `--retry-failed` | Download and test only journaled failures, starting from clean build directories. |
| `-v` | Echo external commands. |
| `-h`, `--help` | Show help and exit. |

`-p` selects **source-package builds**, including their binary subpackages.
For example, `-p 7zip` selects `7zip` and source packages named `7zip-*`;
`7zip-reduced` binaries within the selected build are included automatically.
A full NVR such as `7zip-25.01-5.fc44` can select that exact build.

Tag lists are cached as `DIR/tag-<TAG>.txt`, containing raw
`koji latest-pkg --all TAG` output. Later runs reuse that snapshot until
`--refresh`. `--from-file` accepts the same output or plain NVRs: the first
whitespace-delimited token on each line is used; headers, blank lines, and
`#` comments are ignored. Duplicate NVRs run once. `-n` is applied after
package filtering and before journal filtering. In `--test-only`, it limits
matching build directories in alphabetical order.

Koji matches architectures exactly, so the runner adds `noarch` and `src`
when `-a` is supplied. Without `-a`, `--no-src` may still download source
RPMs because Koji has no source-exclusion option; they are deleted after a
successful fetch, even with `--keep`. Failed downloads retain partial files.
In `--test-only`, source RPMs are simply skipped and left on disk.

## Test cycle and retained files

Each RPM goes through these stages; a nonzero exit stops that RPM's cycle.
Other RPMs continue. A runner exception is a test failure at stage
`exception`; download failures use stage `fetch`.

| Stage | Command |
| --- | --- |
| `unpack` | `tarpm -x -f <original.rpm> -O <work>/ext` |
| `repack` | `tarpm -c -f <work>/<base>-repacked.rpm <work>/ext` |
| `unpack-repacked` | `tarpm -x -f <work>/<base>-repacked.rpm -O <work>/ext2` |
| `list-original` | `tarpm -t -f <original.rpm>` |
| `list-repacked` | `tarpm -t -f <work>/<base>-repacked.rpm` |

The two listing outputs are compared with Python's `difflib`. A difference
is a `content-diff` warning, not a failure; failure to generate a listing
is a failed stage. This compares listings, not file bytes or all metadata.

| Result | Original RPM and work directory | Test log |
| --- | --- | --- |
| Clean pass | Deleted | Deleted |
| Pass with warning | Deleted | Kept with the diff |
| Failure | Kept | Kept when available |
| Any result with `--keep` | Kept | Kept |

Original downloads are never modified. Test work directories are rebuilt
on every attempt; retained artifacts show only the last attempt. Empty
build directories are removed. To retest successful RPMs after updating
tarpm, use `--keep` on the original run or download them again.

```text
rpms/
├── tag-f44.txt                      # cached tag list
├── journal.jsonl                    # finished-build records
├── run.log                          # append-only runner log
├── <NVR>/
│   ├── <base>.rpm                   # original download
│   └── .tarpm-<base>/               # work directory
│       ├── ext/
│       ├── <base>-repacked.rpm
│       ├── ext2/
│       ├── list-orig.txt
│       └── list-repacked.txt
└── logs/
    ├── <NVR>.fetch.log              # kept on fetch failure or --keep
    ├── <base>.test.log              # kept on failure, warning, or --keep
    └── failed-*                     # failure lists described below
```

## Resume, retry, and interrupts

Finished builds append records to `DIR/journal.jsonl` immediately, so a
crash preserves completed work. Records contain `nvr`, `status`, `stage`,
`exit_code`, `signature`, `rpms` (failed or warned RPM basenames), and `ts`
(UTC). The latest record per NVR wins; invalid lines are ignored, and an
incomplete tail is separated before new records are appended.

Statuses are `fetched` (download succeeded without completed tests), `pass`,
`warn`, `fetch-fail`, and `test-fail`. `--test-only` does not update the
journal. Resume with the same architecture/source settings: journal records
are per build and do not identify the test configuration.

```bash
# Continue an interrupted run; re-run failures and unfinished/fetched builds:
python3 tarpm-tag-runner.py -t f44 -j 2 --resume

# Re-run recorded failures after a tarpm update:
python3 tarpm-tag-runner.py -t f44 -j 2 --retry-failed
```

Both modes clear each selected build directory before downloading. They
select from the normal tag/file list after `-p` and `-n`, so journal NVRs
outside that selection are not retried. They test every selected RPM of a
build, not just its previously failing RPMs. Nothing to do exits 0.
`--retry-failed` defaults to `report-retry-YYYYmmdd-HHMMSS.txt` unless `-r`
is supplied. The two modes are mutually exclusive and cannot be combined
with `--test-only`, `--refresh`, or `--no-test`. `--refresh` also conflicts
with `--from-file`.

The first Ctrl+C drops queued work, lets active builds finish, writes a
partial report, and exits 130. Worker commands run in separate sessions
and survive the first interrupt. A second Ctrl+C force-quits without a
report or cleanup; orphaned commands can stop on a closed output pipe,
leaving truncated RPMs or partial work directories. Continue with
`--resume` to get fresh downloads. `--retry-failed` alone misses unfinished
builds with no failure record. Use `--test-only` only for files you intend
to trust as test inputs.

## Reports, logs, and failure lists

The text report defaults to `./report.txt`, **outside** the download
directory. It records the selection, counts, runtime, tag-list source,
run-log path, retained warning logs, and failures with log tails (15 lines
for fetches, 20 for tests) or runner exception text. Failure rollups group
by stage, exit code, and error signature, with up to three example builds
or RPMs per group; the console prints the five most common types.
`--json` includes individual results, warning/log paths, exception text,
and signatures.

An error signature takes the last `tarpm: ` output line (prefix removed),
or the last nonempty line otherwise. Common absolute paths become
`<path>`; hex addresses and runs of four or more digits become `N`. The
result is truncated to 120 characters for grouping.

Every report replaces the failure lists in `DIR/logs/`; a report with no
failures removes old lists. Lists contain sorted, deduplicated NVRs,
optionally followed by `  # detail`:

| File | Contents |
| --- | --- |
| `failed-all.txt` | Every failed build. |
| `failed-fetch.txt` | Fetch failures. |
| `failed-<stage>.txt` | Builds with a failure at that stage. |
| `failed-sig-<slug>-<hash>.txt` | Builds for each of the top 20 signatures; six hex hash digits distinguish filenames. |
| `failed-index.tsv` | One row per failure: `stage`, `signature`, `nvr`, `rpm`, tab-separated; fetch rows have no RPM. |

```bash
# Re-run a failure list, including on a machine without the journal:
python3 tarpm-tag-runner.py --from-file rpms/logs/failed-all.txt -j 4
```

Reports and failure lists describe only the invocation that wrote them.
Warnings skipped by `--resume` remain in the journal and old test logs,
not in the resumed report. Invocations that exit without writing a report
leave the prior report/lists intact.

The run log appends a UTC start/argv header and timestamped runner lines
for each invocation, including heartbeats, force-quit messages, and uncaught
tracebacks. Threaded lines are locked and flushed. Fetch completion lines
include RPM count, size, and duration; test lines include progress and
duration. An unavailable run log does not stop the run.

| Exit | Meaning |
| --- | --- |
| `0` | All fetches/tests succeeded (possibly with warnings), or nothing to resume/retry. |
| `1` | A fetch or test failed; see the report. |
| `2` | Invalid arguments or setup/selection error, such as missing tarpm or nothing to test. |
| `130` | Ctrl+C; takes precedence over failures. A force-quit has no new report. |

## Development

Run the unit and regression suite without external tools or network access:

```bash
python3 test-tarpm-tag-runner.py
```

Future checks may make content differences fatal or compare headers;
CentOS Stream/AlmaLinux backends are also planned.
