# Agent guide

## Working in this repository

`tarpm-tag-runner.py` is the standalone CLI and the file distributed to
users. Keep it compatible with Python 3.9+ and the standard library.
`parse_args()` defines the CLI and `main()` coordinates selection,
recovery, workers, and output. Keep user workflow examples in the
[README](README.md).

The unit and regression suite runs offline with Python alone.

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_tarpm_tag_runner.py` covers core CLI, subprocesses, selection,
  recovery, cleanup, reporting, and self-update.
- `tests/test_size_sorting.py` covers ordering, metadata lookup, caches,
  and recovery interaction.
- `tests/test_cumulative_reports.py` covers journal replay, cumulative
  outcomes, version attribution, and attempt history.

The real smoke test needs Fedora Koji access, Koji and tarpm on `PATH`,
and a new output directory. It defaults to the host binary architecture
and the runner's default tag. Override with `--arch` or `--tag`.

```bash
python3 tests/smoke_test.py --output /tmp/hello-smoke
```

CI runs both suites in Fedora 44 containers on x86-64 and ARM64 Ubuntu
VMs. The smoke test uses the `hello` build and checks size caching,
failure recovery, an already-completed resume, cleanup, tarpm RPM
metadata, timings, and `--no-src` without an architecture filter.
Listing differences are warnings. Jobs upload `hello-smoke-x86_64` or
`hello-smoke-aarch64` reports and logs even on failure, with seven-day
retention. [CI workflow](.github/workflows/tests.yml)

Previously documented roadmap ideas include making listing differences
fatal, comparing RPM headers, and adding CentOS Stream and AlmaLinux
backends.

## Build selection and caches

- `read_nvrs()` queries `koji latest-pkg --all --type=rpm TAG`. Koji's RPM
  build-type filter excludes image-only builds such as live spins, disk
  images, and container images. Use that metadata to determine build type.
- `DIR/tag-<TAG>.txt` stores stdout with `RPM_TAG_CACHE_HEADER`. Reuse the
  snapshot until `--refresh`. Refresh unmarked snapshots before using
  them for selection, including for `--retry-failed`. Log stderr separately,
  validate plain NVRs before using them in paths, and publish a query
  atomically after validation. A failed or interrupted refresh preserves
  the previous snapshot.
- Deduplicate exact NVRs, apply `-p` filters, optionally sort by size, apply
  `-n` (`0` means unlimited), then apply journal filters. An early
  eligibility check can skip size lookups when recovery has no work.
  `-p 7zip` matches NVRs starting with `7zip-`, including source package
  names that start with that prefix. Selected builds include their binary
  subpackages. A full NVR can select a specific build.
- `koji download-build` matches architectures exactly. When `-a` is set,
  pass the requested architectures plus `noarch` and, with source tests
  enabled, `src`. Without `-a`, Koji can download source RPMs even with
  `--no-src`. Skip their tests and clean up those downloads at build end.
  Koji's default debuginfo/debugsource exclusion applies.
- Keep `--list` stdout to NVRs, with all diagnostics on stderr. Listing
  applies recovery filters, can populate caches, and leaves the active
  manifest intact. A closed stdout pipe exits quietly with status 1.
- Check for Koji and tarpm before downloads and before replacing the
  manifest or appending to the journal. Resolve relative `PATH` entries
  from the invocation directory before changing the child's working
  directory. Cached listing and a resume with nothing pending can work
  without the tools or network.

### Size ordering

`sort_nvrs_by_size()` runs only for `--sort` when there is eligible work.
Sort the tag or saved run selection by total compressed bytes for RPMs
matching the architecture/source filters, with ascending NVR as the
tie-breaker in either direction. Use the same noarch/source and
debuginfo/debugsource rules as selection. Workers may finish out of
order. The limit applies before recovery filters, so a limited selection
can consist entirely of already-passed builds.

`DIR/build-sizes.json` is a lookup snapshot keyed by exact NVR. It stores
the Fedora hub, schema version, debug-exclusion policy, generation time,
and per-build ID and `bytes_by_arch` totals. Multiple tags and older
builds can share it. Tag membership comes from the tag snapshot or run
manifest. Changing architecture/source flags or refreshing membership
reuses valid exact-NVR sizes. A fully cached selection can be listed
offline. Unreadable or malformed JSON is a setup error. Removing it
allows a rebuild. Incompatible schema, hub, or policy triggers a rebuild,
and invalid individual entries are looked up again.

`fetch_size_metadata()` streams XML-RPC responses from the Fedora hub
(`SIZE_HUB`) and keeps just the fields needed for totals. Use
`listTaggedRPMS` for large misses and bounded batches of exact-NVR
`getBuild` / `listRPMs` calls for small selections and any builds the bulk
response omits. Publish each complete batch atomically so earlier batches
survive interruption. Bound network retries and fail if exact size
metadata remains unresolved.

Size logs distinguish generation, updates, and cache hits. Log elapsed
time and cache path after generation or update. Heartbeats cover both
network waits and aggregation, including in list mode.

## Round-trip tests, cleanup, and progress

`process_build()` creates a unique `DIR/.build-*/<NVR>/` download
directory for each attempt. `-j` parallelizes builds. Each worker tests
its build's selected RPMs in sequence. A download with zero eligible
RPMs is a fetch failure.

| Stage | Command |
| --- | --- |
| `unpack` | `tarpm -x -f <original.rpm> -O <work>/ext` |
| `repack` | `tarpm -c -f <work>/<base>-repacked.rpm <work>/ext` |
| `unpack-repacked` | `tarpm -x -f <work>/<base>-repacked.rpm -O <work>/ext2` |
| `list-original` | `tarpm -t -f <original.rpm>` |
| `list-repacked` | `tarpm -t -f <work>/<base>-repacked.rpm` |

- Stop an RPM's cycle when a command exits nonzero and record the failed
  stage. A runner exception records stage `exception`. Other RPMs continue.
- In `test_one()`, compare successful listing stdout byte for byte to
  preserve non-UTF-8 filenames. Log stderr from successful listings
  separately. On failure, retain the command's exit code and diagnostics.
  Differing successful listings produce a `content-diff` warning and diff
  in the test log. Escape invalid UTF-8 bytes in diagnostic text.
- Clean up each tested RPM and its work files even when testing raises.
  Clean up the entire build workspace after the attempt, including
  partial downloads and skipped RPMs. `remove_tree()` retries permission
  failures after restoring owner access only to directories within the
  workspace. Skip symlinks when changing permissions. Persistent per-RPM
  cleanup errors turn successful tests into `cleanup` failures with a
  retained log. Build workspace cleanup errors emit a warning.
- Retain failed fetch logs and failed or warned test logs in
  `DIR/logs/<invocation ID>/<NVR>/`, named `<NVR>.fetch.log` and
  `<base>.test.log`. Keep attempt paths unique so retries preserve earlier
  diagnostics.

`run()` provides heartbeat handling and separate stderr when requested.
Commands default to 60-second heartbeats and size lookups to 15 seconds.
An explicit `--heartbeat N` applies to both, and `0` disables them.
`--verbose` echoes commands. Fast tests emit one completion line and
skip progress scans. Fetch completion includes RPM count, size, and
elapsed time. Test completion includes RPM position and elapsed time.

- Fetch heartbeats identify the build position and current RPM, with
  elapsed time for the whole fetch. `koji_download_progress()` parses the
  latest native Koji progress without extra metadata requests. Show the
  current RPM's download bytes and percentage when available. A new RPM
  resets that progress. Without a usable bar, use the current RPM's local
  size. If Koji has yet to identify an RPM, use an explicitly labeled
  build total.
- Test heartbeats group RPM position and index percentage together, then
  show stage, elapsed test time, and any size details in a second group.
  The position percentage is index divided by RPM count, as in fetch
  heartbeats. The test count covers the current build.
- Repack progress compares compressed payload growth to the original RPM
  size. Read the anonymous payload through Linux `/proc` when available
  and fall back to bytes written to the new RPM. The RPM can be pending
  while tarpm builds the memory-backed payload. Re-unpack compares
  extracted bytes to the original unpacked tree size. Initial unpack
  shows extracted and input bytes with no ratio. Listing stages show
  stage and elapsed time.
- Stage percentages are approximate byte ratios. They can exceed 100% or
  stall during checksum work. Omit the estimate for missing or zero
  totals. Count logical file bytes, skip symlinks, count hardlinks once,
  and measure the original tree lazily on the first heartbeat that
  needs it.

## Run state, recovery, and interrupts

`DIR/run.json` is the active manifest. A fresh normal or retry run
atomically saves a new run ID, full build selection, and original
invocation context before workers start. Keep separate state directories
for simultaneous runs.

`DIR/journal.jsonl` appends one record per finished build immediately.
Records contain `run_id`, `nvr`, `status`, `stage`, `exit_code`,
`signature`, `rpms` (failed or warned RPM basenames), and UTC `ts`.
Versioned `results` preserve fetch and per-RPM outcomes, warnings, errors,
log paths, and timings. `attempt` identifies the invocation, tarpm RPM
version, tag, and architecture/source settings.

- New outcomes are `pass`, `warn`, `fetch-fail`, and `test-fail`.
  Older `fetched` records in the current schema are pending for resume.
  Readers skip torn lines and unsupported records. Invalid detailed
  results in a record used for recovery cause a setup error before
  downloads or cleanup.
- `Journal` separates a tail missing its newline before appending. It
  serializes writes, retries short writes, and rolls back a failed
  append with a warning. It disables further appends if rollback or
  determining the append offset fails.
- `--resume` requires a current manifest and matching tag, architecture,
  and source settings. It uses the saved selection even after a tag
  refresh and skips `pass` and `warn` for the active run ID. Start a
  fresh run if the manifest is missing or incompatible. Resuming with a
  different tarpm version preserves the version attribution of saved
  tests. With no pending work in the selection, resume rebuilds reports
  without running tools or adding journal records.
- `--retry-failed` selects tag builds whose latest journal record across
  all run IDs has status `fetch-fail` or `test-fail`. Its
  default text report is `report-retry-YYYYmmdd-HHMMSS.txt` (UTC).
  Both recovery modes honor `-p` and `-n` and download each selected
  build into a fresh workspace to retest all its selected RPMs. A
  filtered resume preserves the rest of the manifest. Use at most one
  of `--resume`, `--retry-failed`, and `--refresh` per invocation.
- The first Ctrl+C during build processing drops queued work, lets
  active builds finish and clean up, then writes a partial report and
  exits 130. The second forces immediate exit before writing a report,
  with a best-effort nonblocking stderr message. Subprocesses run in
  their own sessions and can outlive a force-quit along with their
  workspaces.

Exit status and console work counts describe the current invocation.
Warnings or no pending recovery work exit 0. A filtered resume can exit
0 while builds outside the filter remain pending. Current failures or
a closed stdout pipe exit 1. Argument, setup, and selection errors exit
2. Ctrl+C takes precedence over failures and exits 130.

## Reports and diagnostics

- Validate text, JSON, and run log destinations before work. Reject
  aliases to protected run state, other outputs, or generated failure
  lists. The default text report is `./report.txt` and custom report
  parents generally need to exist in advance.
- `write_report_file()` publishes each regular text or JSON report in
  full via atomic replacement. Preserve mode bits and resolve symlinks
  to their targets. A failed publish preserves the previous report.
  Pipes and devices receive direct writes.
- `cumulative_results()` uses the latest whole-build outcomes for the
  active run. Replacing one build replaces all of its RPM results.
  Reports, timings, and failure lists retain other completed builds.
  Fresh normal and retry runs start their own cumulative totals. The
  **RUN ATTEMPTS** addendum preserves earlier invocations across run
  IDs, with completed/selected counts, tarpm versions, and failed NVRs
  by phase. Use a fresh run to test every build against a new tarpm.
- Before processing selected builds, query the RPM owning the resolved
  tarpm executable with `rpm -qi --file <resolved-binary>`. Summarize
  name, version, release, architecture, and any nonzero epoch. Keep the
  full RPM metadata for COPR provenance, including build date and
  source RPM. Query errors and the five-second timeout are nonfatal.
  Skip the query if no builds are selected.
- Report session runtime uses a monotonic clock from setup through
  worker completion. Fetch results measure the download command. Test
  durations measure the round-trip before cleanup. `stage_seconds`
  includes measured failures and only stages reached. Results without
  a measurement have a null duration and an empty stage map for tests.
  Stage totals sum parallel commands and can exceed wall time. Means
  use measured commands only. Text summaries show subsecond command
  times in milliseconds, rank stages by total command time, and show
  the five slowest inputs per stage. Retained test logs include
  available stage timings.
- Rank failure groups by count before detailed logs. Group by stage,
  exit code, and signature. Fetch counts are failed builds and test
  counts are failed RPMs. Show the RPM test failure rate overall and
  each group's share of its phase's failures. Include up to three
  examples per group, with the top five groups in the console. Retain
  exception text, signatures, and log tails (15 lines for fetch, 20
  for test). Reports also identify the tag-list source, run log, and
  retained warning logs.
- `error_signature()` takes the last line prefixed by `tarpm: ` and
  strips that prefix, falling back to the last nonempty line. Normalize
  common absolute paths to `<path>`, hex literals and runs of four or
  more digits to `N`, and truncate to 120 characters. Where needed,
  group using normalized exception text or `(no diagnostic output)`.
- `DIR/run.log` appends a UTC start/argv header and timestamped lines,
  including heartbeats and uncaught tracebacks. Writes are locked and
  flushed per line. Log I/O errors are nonfatal. `--run-log FILE`
  overrides the path and `--no-run-log` disables it.

### JSON and baseline comparison

Keep the per-build `fetch` and per-RPM `test` arrays. The other JSON
fields include the following.

| Field | Contents |
| --- | --- |
| `session` | Invocation ID, UTC start/end, numeric `runtime_seconds`, status and interruption flag, invocation counts in `summary`, command argv, mode and settings, system/Python details, and tarpm RPM metadata or query error. `tarpm` is null for an empty selection. |
| `run` | Active run ID, original start/command/settings, build statuses, pending count, cumulative status, versions, and per-invocation `attempts`. |
| `summary` | Cumulative fetch and test counts. Test `ok` includes warnings. `clean_passed` and `warnings` are also recorded. |
| `failure_types` | Ranked fetch and test groups, including count, `percent_of_failures`, and examples. |
| `timings` | Per-stage count, total/mean/max seconds, and up to five slowest builds or RPMs. |
| `comparison` | Baseline identity and per-phase outcome changes when requested, otherwise null. |

`--compare` compares current invocation results before cumulative
merging. The exact keys are build NVR for fetches and (build NVR, RPM
filename) for tests. A new NVR is a new case. Count pass-to-fail as a
regression, fail-to-pass (including warnings) as resolved, and persistent
failures with changes in stage, exit code, or normalized signature as
changed failures. Report new failures with no baseline match as
first-seen, and omitted baseline cases as not rechecked. Use the same
selection and environment to evaluate a tarpm update.

Read and validate the baseline before work or output writes. Older
reports without session metadata are supported. Invalid or ambiguous
results exit 2. If `--compare` and `--json` use the same path, compare
against the previous file contents. Comparison reports baseline path,
generation time, tag, and tarpm version when available. It works in
text-only reports, is incompatible with `--list`, and preserves the
ordinary exit codes.

### Generated failure lists

`write_failure_lists()` refreshes reserved names in `DIR/logs/`
after publishing the text report. Remove stale generated names only
and preserve diagnostic logs and unrelated files. Invocations that
exit before writing a report leave earlier reports and lists available.
NVR lists are sorted and deduplicated, with optional `  # detail`
comments.

| File | Contents |
| --- | --- |
| `failed-all.txt` | All failed builds. |
| `failed-fetch.txt` | Failed downloads. |
| `failed-<stage>.txt` | Failed builds for each test stage. |
| `failed-sig-<slug>-<hash>.txt` | Up to 20 top signatures, with six hex hash digits in each name. |
| `failed-index.tsv` | One tab-separated row per failure with stage, signature, NVR, and RPM. Fetch rows have an empty RPM field. |

## Self-update

`-u` or `--update` must be the only option. Handle it before run setup,
independently of Koji and tarpm. It needs network access and a writable
script directory. `update_self()` downloads the single script from
this repository's `main`, bounds and validates the download, compiles it
and checks for the expected `main()` and `parse_args()` definitions,
then replaces the resolved script atomically with existing mode bits.
Preserve the launching symlink and the previous script on failures.
Identical content reports that the script is up to date. An update
overwrites local script edits and takes effect on the next invocation.

## Commits

Use conventional commit subjects that match the repository's existing
prefixes. Wrap body paragraphs at about 72 characters and separate
them with blank lines. Use `git commit -s` for the user's sign-off.

For AI-assisted commits, including shell commits, use the active
client's native AI-attribution trailer unchanged as the sole AI credit.
Keep the client's default format, identity, and any model or
reasoning-effort labels it includes by default. Verify the final message
with `git log -1 --format=%B`.
