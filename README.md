# tarpm-tag-runner

Fetch the latest builds from a Fedora Koji tag and test each downloaded RPM
with **tarpm**: unpack → repack → unpack again → compare file listings.
Tests include source RPMs by default. Failures go into one report; listing
differences are non-fatal warnings.

The runner processes up to `-j` builds at once. Each build uses a temporary
workspace for downloads and test files, which is removed when the build
finishes. The journal, reports, and failure and warning logs preserve the
results. A single 1.5 GiB RPM can need over 10 GiB during its round-trip.

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
python tarpm-tag-runner.py -t f45 -p hello --list

# Preview the ten smallest builds for x86_64, including noarch and src:
python tarpm-tag-runner.py -t f45 -a x86_64 --sort asc -n 10 --list

# Fetch and test hello, including its source RPM:
python tarpm-tag-runner.py -t f45 -p hello -a x86_64

# Test two source-package selections, four builds at once:
python tarpm-tag-runner.py -t f45 -p 7zip -p 389-ds-base -j 4 --json results.json

# Test the whole tag, then resume failed or unfinished builds:
python tarpm-tag-runner.py -t f45 -j 2 --json results.json
python tarpm-tag-runner.py -t f45 -j 2 --resume --json results.json
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

Run `python tarpm-tag-runner.py --help` for CLI help. The default tag is
`f45`. The runner uses `./rpms`, writes `./report.txt`, and runs one worker.
It selects all binary architectures and source RPMs by default. Koji's
default debuginfo exclusion applies.

| Option | Effect |
| --- | --- |
| `-u`, `--update` | Update this script from GitHub's `main` branch and exit; use on its own. |
| `-t TAG` | Koji tag to query. |
| `-p PKG` | Select NVRs equal to `PKG` or starting with `PKG-`; repeatable. |
| `-a ARCH` | Select this arch plus `noarch` and `src` (unless `--no-src`); repeatable. |
| `-d DIR` | Directory for run state, logs, and temporary build workspaces. |
| `-n NUM` | Process the first NUM builds; `0` means no limit. |
| `--sort asc\|desc` | Order builds by selected RPM bytes, smallest/largest first, before `-n`. |
| `--list` | Print selected NVRs and exit without downloading RPMs. |
| `--refresh` | Replace the cached tag list with a fresh Koji query. |
| `--no-src` | Skip source RPM tests. |
| `-j N` | Number of builds to process in parallel. |
| `-r FILE` | Text report path. |
| `--json FILE` | Also write per-build fetch and per-RPM test results as JSON. |
| `--compare FILE` | Compare outcomes with a previous JSON report; incompatible with `--list`. |
| `--run-log FILE` | Append runner log lines to FILE; default `DIR/run.log`. |
| `--no-run-log` | Disable the run log. |
| `--heartbeat N` | Log progress every N seconds; default 60 for commands and 15 for size lookups. An explicit value overrides both; `0` disables. |
| `--resume` | Continue the run in `DIR/run.json`. Skip its test passes and warnings and download the rest afresh. |
| `--retry-failed` | Start a new run of the journaled failures in the tag selection. |
| `-v` | Echo external commands. |
| `-h`, `--help` | Show help and exit. |

`-p` selects **source-package builds**, including their binary subpackages.
For example, `-p 7zip` selects `7zip` and source packages named `7zip-*`;
`7zip-reduced` binaries within the selected build are included automatically.
A full NVR such as `7zip-25.01-5.fc44` can select that exact build.

Tag lists are cached as `DIR/tag-<TAG>.txt`, containing
`koji latest-pkg --all --type=rpm TAG` output with a comment marking the RPM
build-type filter. Later runs reuse that snapshot until `--refresh`. Older
caches without the marker are refreshed automatically once when used as a
build list, including for `--retry-failed`. This removes image entries before
package, limit, and journal filtering. The refresh requires Koji access.
On failure, the runner preserves the old cache and stops.
The runner validates the query's stdout and replaces the cache atomically.
Warnings on stderr go to diagnostics. Interrupted or failed cache writes
preserve the previous snapshot.

Before downloads start, the runner checks for Koji and tarpm. A missing
client exits `2` before replacing the run manifest or writing the journal.
Cached `--list` output and a resume with nothing pending work offline.
Relative `PATH` entries are resolved from the invocation directory before
a download changes its working directory.

Duplicate NVRs run once. `-n` is applied after package filtering and optional
size sorting, before journal filtering.

### Size ordering

`--sort asc` processes the smallest builds first; `--sort desc` processes
the largest first. Size is the sum of compressed RPM bytes selected by
`-a` and `--no-src`, including the automatic noarch/source selections and
excluding Koji's default debuginfo/debugsource packages. Equal sizes use
ascending NVR order. These are input sizes, not estimates of unpacked disk
space or test duration. Parallel workers may finish in a different order.

Size sorting augments the existing tag/file selection. Without `--sort`,
the runner does not read the size cache or query size metadata. `--list`
prints the chosen NVR order with diagnostics on stderr. Sorting precedes
`-n`; resume/retry journal filters still follow `-n`, so a limited selection
whose builds already passed may have nothing pending.

Metadata is cached as `DIR/build-sizes.json`: a JSON snapshot containing
the Fedora hub URL, schema version, debug-exclusion policy, generation
time, and a `builds` map keyed by exact NVR. Entries store the build ID and
`bytes_by_arch`, allowing architecture/source filters to change without a
new lookup. Tag membership remains in the existing tag snapshot; the size
cache can contain builds from several tags and retained older builds.
It is a lookup snapshot, separate from the append-only JSONL journal.

Large uncached tag selections use Fedora's `listTaggedRPMS` API. Small
selections, explicit files, and missing older NVRs use bounded batches of
exact-build lookups. The standard-library metadata reader queries
`https://koji.fedoraproject.org/kojihub` directly and streams the XML
response, retaining totals rather than all RPM records in memory.
These queries do not download RPMs or change which builds were selected.

The runner logs `Generating build sizes` for a new cache, `Updating build
sizes` for missing entries, or `using cached build sizes` when all entries
are available. Generation and updates emit a heartbeat every 15 seconds by
default, covering both network waits and aggregation; `--heartbeat` can
override or disable it. Completion reports elapsed time and the cache path.
Complete lookup batches are saved atomically, so interrupted updates keep
earlier batches and preserve the previous snapshot. Network retries are
bounded; an unresolved size is an error, never an assumed zero.

`--refresh` refreshes tag membership; sizes already cached for exact NVRs
remain reusable, and only missing entries need updating. A corrupt cache
is a setup error; remove it to rebuild. An incompatible hub/schema/policy
is reported and rebuilt when sizes are needed. Fully cached selections can
be listed offline.

Build lists must contain plain `name-version-release` values.

`--list` applies resume/retry journal filters before printing and sends
diagnostics to stderr.
If a pager such as `more` exits early, the runner exits quietly with status
1 when it detects the closed stdout pipe.

Koji matches architectures exactly. When `-a` is supplied, the runner adds
`noarch` and, with source tests enabled, `src`. With `--no-src` and no
architecture filter, Koji can still download sources. The runner skips
their tests and discards the downloads during build cleanup.

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
Test heartbeats group the RPM position and its percentage in one set of
parentheses, followed by the stage, elapsed test time, and compact size
details in another:

```text
[2026-10-28 15:19:30 UTC] tarpm-tag-runner.py: still testing ghc-pandoc-devel-3.7.0.2-39.fc44.s390x (RPM 140/171, 81.9%) (repack ~60.3%, 1m 00s; payload 11.1/18.4 MiB; RPM pending; input 239.5 MiB)
```

The position percentage (`81.9%`) is the current RPM's index divided by the
test count, not the number of completed tests; parallel tests receive their
indices when they start. In the default pipeline, the count covers RPMs in
the current build.

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

The two listing commands' stdout streams are compared with Python's
`difflib`. Stderr is logged separately. A difference
is a `content-diff` warning, not a failure; failure to generate a listing
is a failed stage. This compares listings, not file bytes or all metadata.
Listings are saved and compared as raw bytes, including non-UTF-8 filenames.
Diagnostic logs and displayed diffs escape invalid UTF-8 bytes as `\xff`
instead of failing to decode command output.

Each attempt downloads into a unique workspace under `DIR/.build-*/`.
It removes RPMs and extracted files after each test, including tests that
raise a runner exception. The remaining workspace is removed when the
build finishes. This includes partial
downloads after a failed fetch and RPMs excluded from testing. Diagnostic
logs for failures and listing warnings have separate paths for each
invocation and build so cumulative reports can reference them.

Cleanup retries read-only artifact directories with owner access restored,
without following symlinks. If a passed test's work directory still cannot
be removed, the result fails at stage `cleanup` and retains its log.

```text
rpms/
├── tag-f45.txt                      # cached tag list
├── build-sizes.json                 # optional per-NVR size cache (--sort)
├── journal.jsonl                    # finished-build records
├── run.json                         # active run ID and full build selection
├── run.log                          # append-only runner log
├── .build-*/                       # temporary workspace while a build is active
│   └── <NVR>/                     # downloaded RPMs and test work files
└── logs/
    ├── <invocation ID>/<NVR>/
    │   ├── <NVR>.fetch.log          # kept on fetch failure
    │   └── <base>.test.log          # kept on failure or warning
    └── failed-*                     # failure lists described below
```

## Resume, retry, and interrupts

Before workers start, a fresh run atomically saves its run ID,
full build selection, and original invocation context in `DIR/run.json`.
Resume reuses that manifest and skips only passes/warnings recorded for
its run ID. Older successes cannot
hide unfinished or queued re-tests. A new normal or retry run replaces the
manifest. Use separate directories for simultaneous runs. `--list` leaves
the active run intact.

Finished builds append records to `DIR/journal.jsonl` immediately, so a
process crash preserves completed work. Records contain `run_id`, `nvr`,
`status`, `stage`, `exit_code`, `signature`, `rpms` (failed or warned RPM
basenames), and `ts` (UTC). Each record includes full fetch and per-RPM
`results`, including warnings, errors, log paths, and timings. Their
`attempt` metadata identifies the invocation, tarpm RPM version, tag, and
architecture/source settings. This records which version passed or failed
each NVR. The latest current-format record per NVR supplies cumulative
results. Earlier attempts remain available for history. Torn lines and
unsupported records are skipped. Invalid result details in a record used
for recovery cause a setup error before downloads or cleanup begin.
An incomplete tail is separated before new records are appended.
Short writes are retried; failed appends are rolled back with a warning.
If rollback fails, further journal writes are disabled to prevent corruption.

New outcomes are `pass`, `warn`, `fetch-fail`, and `test-fail`.
An existing `fetched` record is eligible for testing on resume. Resume
requires a current manifest and the original tag,
architecture, and source settings. Start a fresh run if the manifest
is missing or uses an older format. Resume can use a different tarpm
version. Passed builds keep their earlier results and version attribution.
Retry selects the latest failures across all run IDs.

```bash
# Continue a run by retrying failed and unfinished builds:
python tarpm-tag-runner.py -t f45 -j 2 --resume

# Re-run recorded failures after a tarpm update:
python tarpm-tag-runner.py -t f45 -j 2 --retry-failed
```

Both modes download each selected build into a fresh workspace and test
all its selected RPMs. `--resume` uses the saved manifest even after a tag
cache refresh. `--retry-failed` uses the tag selection. The `-p` and `-n`
filters apply in both modes. Temporary resume filters leave other pending
builds in the manifest.
A resume with nothing pending rebuilds the cumulative reports
without downloading, testing, or adding journal records. Nothing to do
exits 0. Exit status and console work counts describe the current invocation;
a filtered resume can succeed while other builds remain pending in the
cumulative report.
`--retry-failed` defaults to `report-retry-YYYYmmdd-HHMMSS.txt` unless `-r`
is supplied. Both recovery modes are incompatible with `--refresh` and
with each other.

The first Ctrl+C drops queued work, lets active builds finish and clean up,
writes a partial report, and exits 130. A second Ctrl+C force-quits and can
leave in-flight commands and their temporary workspaces behind. Resume
retries unfinished builds in fresh workspaces.

## Reports, logs, and failure lists

The text report defaults to `./report.txt`, **outside** the download
directory. Create custom report directories in advance. The runner checks
report destinations before starting work and rejects paths that overlap
run state, other outputs, or generated failure lists.

The runner replaces each regular text or JSON report atomically after
writing it in full. If a write fails, the previous file stays readable.

Fetch runs and their resumes rebuild the report from the latest
completed attempt for each build in the active run. A successful retry
replaces that build's previous results, including all its RPMs; other
builds' passes, warnings, and failures remain in the totals. This also applies
to JSON output and failure lists. A fresh normal or `--retry-failed` run
starts separate cumulative totals.

The opening summary records UTC session start/end times,
wall-clock runtime, completion/interruption status, the invocation command,
mode, worker count, architecture/source selections, package filters, and
system/Python versions. Runtime covers the current invocation through worker
completion, including setup and downloads; it is not cumulative across
`--resume` or retry invocations and does not sum parallel worker times.
Test counts distinguish clean passes, passes with warnings, and failures,
and include the percentage of tested RPMs that failed.

The cumulative section identifies the original run start/command, build
statuses, pending count, and tarpm versions used by its saved tests. Passing
a resumed tag does not mean every build was retested with the latest tarpm:
start a fresh run for that validation. The **RUN ATTEMPTS** addendum lists
completed/selected build counts and failed NVRs for each recorded invocation
and tarpm version in the download directory, including earlier fresh runs.
Download failures and test failures are listed separately. Past failures
remain in this history after they have been fixed in the cumulative totals.

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
| `session` | Current invocation ID, start/end, numeric `runtime_seconds`, status, counts in `summary`, interruption flag, command argument list, settings, system details, and `tarpm` package fields/full `rpm_info`/query error (`tarpm` is null when the invocation runs no tests). |
| `run` | Active run ID, original start/command/settings, build statuses, pending count, cumulative status, versions used, and per-invocation `attempts` history. |
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
runtime; averages include only measured commands. Cumulative timings retain
each build's latest attempt, excluding superseded attempts. Per-test logs
also retain stage timings when the log is kept. Subsecond command times display in
milliseconds in the text summary.

Use a saved JSON report as a baseline to see what changed after updating
tarpm:

```bash
python tarpm-tag-runner.py -t f45 -p hello -a x86_64 --json baseline.json
# After updating tarpm, repeat the same selection:
python tarpm-tag-runner.py -t f45 -p hello -a x86_64 \
    --compare baseline.json --json current.json
```

Comparison works in text reports without requiring a new `--json` output.
It compares only work performed in the current invocation; cumulative
results carried over from earlier attempts are not counted as rechecked.
It identifies a fetch by exact build NVR, and a test by exact build NVR and
RPM filename, regardless of download directory. Its summary separates:

- **Regressions:** previously passed, now failed.
- **Resolved:** previously failed, now passed (possibly with a warning).
- **Still failing:** failed both times; changed stage, exit code, or
  normalized signature is reported separately within these failures.
- **First-seen failures:** failed now with no matching baseline result.
- **Not rechecked:** baseline results absent from the current invocation.

Filtered, resumed, and interrupted runs cannot establish that
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

Every report replaces the generated failure lists in `DIR/logs/`. A report
with no failures removes those lists and preserves other files in the log
directory. The list filenames below are reserved for the runner. Lists
contain sorted, deduplicated NVRs,
optionally followed by `  # detail`:

| File | Contents |
| --- | --- |
| `failed-all.txt` | Every failed build. |
| `failed-fetch.txt` | Fetch failures. |
| `failed-<stage>.txt` | Builds with a failure at that stage. |
| `failed-sig-<slug>-<hash>.txt` | Builds for each of the top 20 signatures; six hex hash digits distinguish filenames. |
| `failed-index.tsv` | One row per failure: `stage`, `signature`, `nvr`, `rpm`, tab-separated; fetch rows have no RPM. |

Reports and failure lists cover the active run's latest build outcomes,
including warnings skipped by `--resume`. The attempt addendum retains
historical failures. Invocations that exit before writing a report leave
the prior report and failure lists intact.

The run log appends a UTC start/argv header and timestamped runner lines
for each invocation, including heartbeats and uncaught tracebacks. A
force-quit attempts a nonblocking message to stderr only, so logging cannot
delay termination. Threaded log lines are locked and flushed. Fetch
completion lines include RPM count, size, and duration; test lines include progress and
duration. An unavailable run log does not stop the run.

Fetch starts and completions identify the build. Heartbeats identify the
current RPM, its position and position percentage within the build, and
the build's position in the run. Like test heartbeats, the position
percentage is the current RPM's index divided by the count of RPMs in the
build. The separate download percentage and sizes describe bytes fetched
for that RPM, using Koji's native output without extra metadata requests.
Elapsed time is for the build's fetch, for example:

```text
still fetching demo-1-1.noarch.rpm (RPM 1/2, 50.0%) (build 3/20) (5m 00s, 913.00 MiB / 1.46 GiB, 61% so far)
```

Each new RPM gets its own label and progress. Without a usable progress
bar, the heartbeat shows its local file size. If Koji hasn't identified an
RPM, it falls back to the build label and explicitly labels the build total.

| Exit | Meaning |
| --- | --- |
| `0` | All fetches/tests succeeded (possibly with warnings), or nothing to resume/retry. |
| `1` | A fetch or test failed (see the report), or the stdout pipe closed early. |
| `2` | Invalid arguments or setup/selection error, such as missing tarpm or nothing to test. |
| `130` | Ctrl+C; takes precedence over failures. A force-quit has no new report. |

## Development

Run the unit and regression suite without external tools or network access:

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs independent jobs on native x86-64 and ARM64 Ubuntu VMs,
each using a Fedora 44 container with Koji and tarpm. Both run that suite
and smoke-test Fedora's `hello` build from the runner's default Koji tag
on their native RPM architecture (`x86_64` or `aarch64`). They check failure
recovery with `--resume`, cleanup after failed and successful tests, tarpm
version metadata, and `--no-src` without an architecture filter. Reports
and logs are uploaded as
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

## Commit conventions

Sign off every commit with `git commit -s`.

Follow the active client's native AI-attribution defaults unchanged. Do not
override its trailer format or identity, add a second credit trailer, or
add model names or reasoning-effort labels unless the client includes them
by default.
