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
the Fedora hub, and `tarpm`. The runner uses `koji latest-pkg --all --type=rpm`
to list RPM builds and `koji download-build` to fetch RPMs. Koji's build-type
metadata excludes image-only builds such as live spins, disk images, and
container images automatically, without package-name exclusions.

On Fedora with DNF5:

```bash
sudo dnf install koji dnf5-plugins
sudo dnf copr enable dcantrell/tarpm
sudo dnf install tarpm
```

`dnf5-plugins` provides the `dnf copr` command used above.

```bash
# Preview a small selection without downloading RPMs:
python tarpm-tag-runner.py -t f44 -p hello --list

# Fetch and test hello, including its source RPM; keep all artifacts:
python tarpm-tag-runner.py -t f44 -p hello -a x86_64 --keep

# Test two source-package selections, four builds at once:
python tarpm-tag-runner.py -t f44 -p 7zip -p 389-ds-base -j 4 --json results.json

# Download only, then test the local RPMs separately:
python tarpm-tag-runner.py -p hello -a x86_64 --no-test
python tarpm-tag-runner.py --test-only -p hello -a x86_64
```

To update the runner in place from this repository's latest `main` branch:

```bash
# Equivalent: python tarpm-tag-runner.py --update
python tarpm-tag-runner.py -u
```

Use the update option on its own. It requires network access and write
access to the script's directory, but no Koji or tarpm installation. The
download is checked for valid Python and expected runner functions before
atomically replacing the script, preserving its permissions. If launched
through a symlink, the target script is updated. Updates overwrite local
edits to the script; other repository files are not updated. The command
exits after updating, and the next invocation uses the new version. An
identical download reports that the runner is already up to date.

## Options and build selection

Run `python tarpm-tag-runner.py --help` for CLI help. Defaults: tag `f44`,
download directory `./rpms`, report `./report.txt`, one worker, all binary
architectures plus source RPMs. Koji's default debuginfo exclusion applies.

| Option | Effect |
| --- | --- |
| `-u`, `--update` | Update this script from GitHub's `main` branch and exit; use on its own. |
| `-t TAG` | Koji tag to query. |
| `-p PKG` | Select NVRs equal to `PKG` or starting with `PKG-`; repeatable. |
| `-a ARCH` | Select this arch plus `noarch` and `src` (unless `--no-src`); repeatable. |
| `-d DIR` | Put each build in `DIR/<NVR>/`. |
| `-n NUM` | Process the first NUM builds; `0` means no limit. |
| `--list` | Print selected NVRs and exit without downloading RPMs; incompatible with `--test-only`. |
| `--from-file FILE` | Read a build list instead of querying Koji or using the cache. |
| `--refresh` | Replace the cached tag list with a fresh Koji query. |
| `--no-test` | Download only; tarpm is not required. |
| `--no-src` | Skip source tests and discard source RPMs after a successful fetch. |
| `--test-only` | Test local RPMs; `-p`, `-n`, `-a`, and `--no-src` still apply. |
| `--keep` | Retain originals, test work directories, and logs even for passes. |
| `-j N` | Parallel builds in normal runs; parallel RPMs in `--test-only`. |
| `-r FILE` | Text report path. |
| `--json FILE` | Also write per-build fetch and per-RPM test results as JSON. |
| `--compare FILE` | Compare outcomes with a previous JSON report; incompatible with `--list`. |
| `--run-log FILE` | Append runner log lines to FILE; default `DIR/run.log`. |
| `--no-run-log` | Disable the run log. |
| `--heartbeat N` | Log progress every N seconds during external commands; default 60, `0` disables. |
| `--resume` | Continue the active run, skipping its test passes/warnings; download and test the rest afresh. |
| `--retry-failed` | Download and test only journaled failures, starting from clean build directories. |
| `-v` | Echo external commands. |
| `-h`, `--help` | Show help and exit. |

`-p` selects **source-package builds**, including their binary subpackages.
For example, `-p 7zip` selects `7zip` and source packages named `7zip-*`;
`7zip-reduced` binaries within the selected build are included automatically.
A full NVR such as `7zip-25.01-5.fc44` can select that exact build.

Tag lists are cached as `DIR/tag-<TAG>.txt`, containing
`koji latest-pkg --all --type=rpm TAG` output with a comment marking the RPM
build-type filter. Later runs reuse that snapshot until `--refresh`. Older
caches without the marker are refreshed automatically once, including on
`--resume` and `--retry-failed`, so existing image entries are removed before
package, limit, and journal filtering. This refresh requires Koji access;
if it fails, the old cache is preserved and the run stops.

`--from-file` accepts the same output or plain NVRs: the first
whitespace-delimited token on each line is used; headers, blank lines, and
`#` comments are ignored. Explicit files are used as supplied, without a
Koji build-type query; generate them with the filtered command above to
exclude image-only builds. Duplicate NVRs run once. `-n` is applied after
package filtering and before journal filtering. In `--test-only`, it limits
matching build directories in alphabetical order.

Build lists must contain plain `name-version-release` values. Fetch,
recovery, and local testing reject paths and symlinked build directories.

`--list` applies resume/retry journal filters before printing and sends
diagnostics to stderr, so stdout can be saved and reused with `--from-file`.

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

Stage details and size metrics appear on heartbeats only; fast RPM tests
log one completion line. `--verbose` also prints each stage's command.
Test heartbeats combine the RPM position, stage, elapsed test time, and
compact size details:

```text
[2026-10-28 15:19:30 UTC] tarpm-tag-runner.py: still testing ghc-pandoc-devel-3.7.0.2-39.fc44.s390x (140/171, 81.9%; repack ~60.3%, 1m 00s; payload 11.1/18.4 MiB; RPM pending; input 239.5 MiB)
```

The position percentage (`81.9%`) is the current RPM's index divided by the
test count, not the number of completed tests; parallel tests receive their
indices when they start. In the default pipeline, the count covers RPMs in
the current build; in `--test-only`, it covers all selected RPMs.

The stage estimate (`~60.3%`) compares the compressed payload size with the
original RPM size as the expected repack size. The payload is read via
Linux `/proc` when accessible; otherwise, the estimate uses RPM bytes
written. Tarpm builds its payload in an anonymous memory-backed file before
writing the RPM, so `RPM pending` can persist for most of repacking.
`unpack-repacked` compares extracted bytes with the known original unpacked
tree size. The initial `unpack` shows extracted bytes and input RPM size
without an estimate because the unpacked total is not yet known. Listing
stages show their name and elapsed test time without a byte estimate.

Stage percentages are byte estimates, not whole-test or elapsed-time
percentages. They may reach or exceed 100% while a command is still running:
repacked sizes can differ, and checksum passes can continue while sizes stay
unchanged. Missing or zero expected sizes omit the stage percentage. Sizes
count logical file bytes, without following symlinks or counting hardlinks
twice; the completed input tree is measured once, on the first heartbeat
that needs it. Fast tests do not scan the tree for progress.

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
├── run.json                         # active run ID and full build selection
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

Before workers start, each fetch invocation atomically saves its run ID and
full build selection in `DIR/run.json`. Resume reuses that manifest and
skips only passes/warnings recorded for its run ID. Older successes cannot
hide unfinished or queued re-tests. A new normal or retry run replaces the
manifest; use separate directories for simultaneous runs. `--list` and
`--test-only` leave it unchanged.

Finished builds append records to `DIR/journal.jsonl` immediately, so a
process crash preserves completed work. Records contain `run_id`, `nvr`,
`status`, `stage`, `exit_code`, `signature`, `rpms` (failed or warned RPM
basenames), and `ts`
(UTC). The latest record per NVR wins; invalid lines are ignored, and an
incomplete tail is separated before new records are appended.
Short writes are retried; failed appends are rolled back with a warning.
If rollback fails, further journal writes are disabled to prevent corruption.

Statuses are `fetched` (download succeeded without completed tests), `pass`,
`warn`, `fetch-fail`, and `test-fail`. `--test-only` does not update the
journal. Resume with the same architecture/source settings: records are per
build and do not identify the test configuration. Without a manifest, resume
uses legacy journal history and creates a manifest before doing new work;
an invalid manifest is an error. Retry selects the latest failures across
all run IDs.

```bash
# Continue an interrupted run; re-run failures and unfinished/fetched builds:
python tarpm-tag-runner.py -t f44 -j 2 --resume

# Re-run recorded failures after a tarpm update:
python tarpm-tag-runner.py -t f44 -j 2 --retry-failed
```

Both modes clear each selected build directory before downloading. Normal
tag/file selection, `-p`, and `-n` still apply; resume also restricts work to
the manifest's selection, preserving pending builds outside a temporary
filter. They test every selected RPM of a build, not just its previously
failing RPMs. Nothing to do exits 0.
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
directory. Its opening summary records UTC session start/end times,
wall-clock runtime, completion/interruption status, the invocation command,
mode, worker count, architecture/source selections, package filters, and
system/Python versions. Runtime covers the current invocation through worker
completion, including setup and downloads; it is not cumulative across
`--resume` or retry invocations and does not sum parallel worker times.
Test counts distinguish clean passes, passes with warnings, and failures,
and include the percentage of tested RPMs that failed.

Before testing, the runner identifies the `tarpm` executable selected from
`PATH` and queries its owning RPM with `rpm -qi --file <resolved-binary>`.
The summary shows its package name, version, release, and architecture
(including a nonzero epoch). The full RPM metadata is included at the end
of the report, preserving COPR build details such as build date and source
RPM. It does not use `tarpm --version`. A missing `rpm` command, unowned
executable, failed query, or five-second query timeout is reported without
preventing tests. Fetch-only runs skip the query.

Failure rollups appear **before individual failure logs**, sorted by count,
and group by stage, exit code, and error signature. Each group shows its
count, percentage of failures in that phase, and up to three example builds
or RPMs; the console prints the five most common types. Counts represent
failed build downloads for fetches and failed RPM tests for tests. Detailed
failures retain log tails (15 lines for fetches, 20 for tests), signatures,
and runner exception text. The report also records the tag-list source,
run-log path, and retained warning logs.

`--json` retains the individual `fetch` and `test` result arrays and adds:

| Field | Contents |
| --- | --- |
| `session` | Start/end, numeric `runtime_seconds`, status, interruption flag, command argument list, settings, system details, and `tarpm` package fields/full `rpm_info`/query error (`tarpm` is null for fetch-only). |
| `summary` | Counts for each enabled phase; test counts include `clean_passed` and `warnings` (`ok` includes warnings). |
| `failure_types` | Ranked `fetch` and `test` groups with stage, exit code, signature, count, `percent_of_failures`, and examples. |
| `timings` | Per-command-stage counts, total/mean/max seconds, and up to five slowest builds or RPMs. |
| `comparison` | Baseline identity and per-phase outcome changes when `--compare` is used; otherwise null. |

Each fetch result includes `duration_seconds` for its download command.
Each test result includes `duration_seconds` for its round-trip before
cleanup and a `stage_seconds` map for the commands reached: `unpack`,
`repack`, `unpack-repacked`, `list-original`, and `list-repacked`. Timings
use a monotonic clock and include commands that returned a failure.
Unreached stages have no entry; results without a completed measurement
(such as runner exceptions) have a null duration and an empty stage map.
The text report ranks stages by total command time and names the five
slowest inputs for each. Totals sum parallel workers and can exceed session
runtime; averages include only measured commands. Per-test logs also retain
stage timings when the log is kept. Subsecond command times display in
milliseconds in the text summary.

Use a saved JSON report as a baseline to see what changed after updating
tarpm:

```bash
python tarpm-tag-runner.py -t f44 -p hello -a x86_64 --json baseline.json
# After updating tarpm, repeat the same selection:
python tarpm-tag-runner.py -t f44 -p hello -a x86_64 \
    --compare baseline.json --json current.json
```

Comparison works in text reports without requiring a new `--json` output.
It identifies a fetch by exact build NVR, and a test by exact build NVR and
RPM filename, regardless of download directory. Its summary separates:

- **Regressions:** previously passed, now failed.
- **Resolved:** previously failed, now passed (possibly with a warning).
- **Still failing:** failed both times; changed stage, exit code, or
  normalized signature is reported separately within these failures.
- **First-seen failures:** failed now with no matching baseline result.
- **Not rechecked:** baseline results absent from the current invocation.

Filtered, resumed, interrupted, and fetch-only runs cannot establish that
omitted tests passed. An RPM from a newer build is a new case rather than
evidence that an older build's failure was resolved. Comparisons report
observed outcome changes without attributing them to tarpm alone; use the
same selection and environment when evaluating a tarpm update. The baseline
path, generation time, tag, and tarpm package (when available) appear in the
report. Older JSON reports without session metadata are supported.

The baseline is read and validated before work begins, so invalid or
ambiguous results fail with exit status 2 before downloads or cleanup.
Using the same file for `--compare` and `--json` compares against its old
contents before replacing it. Comparison does not alter the usual exit
status: current failures still exit 1, and interruption still exits 130.

An error signature takes the last `tarpm: ` output line (prefix removed),
or the last nonempty line otherwise. Common absolute paths become
`<path>`; hex addresses and runs of four or more digits become `N`. The
result is truncated to 120 characters for grouping. If no signature was
captured, grouping uses normalized runner exception text or the explicit
`(no diagnostic output)` label.

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
python tarpm-tag-runner.py --from-file rpms/logs/failed-all.txt -j 4
```

Reports and failure lists describe only the invocation that wrote them.
Warnings skipped by `--resume` remain in the journal and old test logs,
not in the resumed report. Invocations that exit without writing a report
leave the prior report/lists intact.

The run log appends a UTC start/argv header and timestamped runner lines
for each invocation, including heartbeats and uncaught tracebacks. A
force-quit attempts a nonblocking message to stderr only, so logging cannot
delay termination. Threaded log lines are locked and flushed. Fetch
completion lines include RPM count, size, and duration; test lines include progress and
duration. An unavailable run log does not stop the run.

Fetch starts and completions identify the build. Heartbeats identify the
current RPM, its position within the build, and the build's position in the
run. Sizes and percentages both describe that RPM, using Koji's native
output without extra metadata requests. Elapsed time is for the build's
fetch, for example:

```text
still fetching demo-1-1.noarch.rpm (RPM 1/2) (build 3/20) (5m 00s, 913.00 MiB / 1.46 GiB, 61% so far)
```

Each new RPM gets its own label and progress. Without a usable progress
bar, the heartbeat shows its local file size. If Koji hasn't identified an
RPM, it falls back to the build label and explicitly labels the build total.

| Exit | Meaning |
| --- | --- |
| `0` | All fetches/tests succeeded (possibly with warnings), or nothing to resume/retry. |
| `1` | A fetch or test failed; see the report. |
| `2` | Invalid arguments or setup/selection error, such as missing tarpm or nothing to test. |
| `130` | Ctrl+C; takes precedence over failures. A force-quit has no new report. |

## Development

Run the unit and regression suite without external tools or network access:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs independent jobs on native x86-64 and ARM64 Ubuntu VMs,
each using a Fedora 44 container with Koji and tarpm. Both run that suite
and use Fedora's `hello` build for real download/round-trip smoke tests with
their native RPM architecture (`x86_64` or `aarch64`). They check fetch-only
→ resume, source and binary RPMs, `--keep`, test-only cleanup, and `--no-src`
without an architecture filter. Reports, logs, and artifacts are uploaded as
`hello-smoke-x86_64` and `hello-smoke-aarch64` for seven days, including on
failure. To run the same smoke tests locally after installing the tools:

```bash
python tests/smoke_test.py --output /tmp/hello-smoke
```

The output directory must be new. The binary architecture defaults to the
host architecture; use `--arch ARCH` to override it. Smoke tests require
network access; listing-difference warnings remain non-fatal, as in normal
runs.

Future checks may make content differences fatal or compare headers;
CentOS Stream/AlmaLinux backends are also planned.
