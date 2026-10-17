#!/usr/bin/env python3
"""tarpm-tag-runner — list builds in a Koji tag, fetch their RPMs, and
run each RPM through a tarpm unpack/repack/unpack test.

The default run pipelines each build — fetch, test, clean up — with -j
builds in flight, so disk use stays bounded (passed RPMs are removed
after their test; failures are kept).

The tarpm test runs by default on every fetched RPM, including the
source RPM (maximum coverage by default):
  --no-test    fetch only, skip the test
  --test-only  skip fetching, test the RPMs already downloaded
  --no-src     skip .src.rpm in the test (they are tested by default)

The tag list is cached as <dir>/tag-<TAG>.txt and reused on later runs
(--refresh re-fetches from Koji; --from-file reads an explicit list).
Structured results, optional JSON report, threaded fetch/test.

External commands (system level, matching the CLI workflow):
  koji latest-pkg --all <tag>   list the latest build per package in a tag
  koji download-build <nvr>     fetch a build (all arches + src by default)
  tarpm -x / -c / -t            test round-trip per RPM

Test cycle per RPM (.src.rpm included by default; --no-src to skip):
  1. tarpm -x -f in.rpm -O <work>/ext                unpack
  2. tarpm -c -f <work>/<base>-repacked.rpm <work>/ext   repack
  3. tarpm -x -f <repacked.rpm> -O <work>/ext2       unpack the repacked RPM
  4. tarpm -t on both + compare                      content check (warning)

After a clean pass the work dir, the original RPM, and the test log are
removed. A failure keeps the RPM, work dir, and log for inspection; a pass
with a warning keeps the log (the only copy of the content diff). --keep
retains everything for passing RPMs as well.

Ctrl+C stops gracefully: queued work is dropped, in-flight work
finishes, the partial results are written to the report, and the exit
status is 130. A second Ctrl+C while waiting force-quits.

Exit status: 0 all ok, 1 some fetches/tests failed, 2 usage/environment,
130 interrupted by Ctrl+C (partial results reported).

Python 3.9+ (standard library only).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import difflib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROG = Path(sys.argv[0]).name or "tarpm-tag-runner"


class UsageError(Exception):
    """Fatal setup/usage problem; leads to exit status 2."""


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_duration(seconds: float) -> str:
    """Human-friendly duration: '42.3s', '2m 05s', '1h 04m 05s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def log(msg: str, err: bool = False) -> None:
    """Emit a UTC-timestamped log line; err=True routes it to stderr."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} UTC] {msg}", file=sys.stderr if err else sys.stdout,
          flush=True)


def run(cmd: list, cwd: Path | None = None) -> tuple:
    """Run an external command; return (exit_code, combined stdout+stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.returncode, proc.stdout or ""
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"


@dataclasses.dataclass
class FetchResult:
    nvr: str
    ok: bool
    exit_code: int | None = None
    log: Path | None = None


@dataclasses.dataclass
class TestResult:
    rpm: str
    nvr: str
    arch: str
    ok: bool
    stage: str | None = None          # stage that failed, if any
    exit_code: int | None = None
    warning: str | None = None        # e.g. "content-diff" (non-fatal)
    log: Path | None = None


# ---------------------------------------------------------------------------
# fetch phase
# ---------------------------------------------------------------------------

def read_nvrs(args) -> tuple:
    """Return (NVR list, tag-list source) for the run.

    The tag list is cached as <dir>/tag-<TAG>.txt (raw 'koji latest-pkg
    --all' output) and reused on later runs; --refresh forces a re-fetch.
    """
    cache = None
    if args.from_file:
        try:
            text = Path(args.from_file).read_text()
        except OSError as exc:
            raise UsageError(f"cannot read --from-file input: {exc}")
        source = f"from file ({args.from_file})"
    else:
        cache = Path(args.dir) / f"tag-{args.tag}.txt"
        if cache.is_file() and not args.refresh:
            try:
                text = cache.read_text()
            except OSError as exc:
                raise UsageError(f"cannot read cached tag list: {exc}")
            source = f"cached ({cache})"
        else:
            if shutil.which("koji") is None:
                raise UsageError("koji client not found in PATH")
            log(f"{PROG}: fetching koji tag {args.tag}")
            t0 = time.monotonic()
            rc, out = run(["koji", "latest-pkg", "--all", args.tag])
            elapsed = time.monotonic() - t0
            if rc != 0:
                log(f"{PROG}: tag {args.tag}: failed (after {elapsed:.1f}s)",
                    err=True)
                raise UsageError(
                    f"'koji latest-pkg --all {args.tag}' failed (exit {rc}):\n{out}")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(out)
            source = f"fetched from Koji ({elapsed:.1f}s)"
            text = out
    nvrs = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        nvr = parts[0]
        # skip the table header lines (title row + dashes)
        if nvr.startswith("-") or nvr == "Build":
            continue
        nvrs.append(nvr)
    if source.startswith("cached"):
        log(f"{PROG}: tag {args.tag}: using cached list ({len(nvrs)} packages)")
    elif source.startswith("fetched"):
        log(f"{PROG}: tag {args.tag}: success ({len(nvrs)} packages "
            f"in {elapsed:.1f}s)")
        log(f"{PROG}: saved tag list to {cache}")
    else:
        log(f"{PROG}: tag {args.tag}: list from {args.from_file} "
            f"({len(nvrs)} packages)")
    return nvrs, source


def select_nvrs(nvrs: list, pkgs: list) -> list:
    """Filter NVRs by package names (name or name- prefix; exact NVR match)."""
    if not pkgs:
        return list(nvrs)
    out = []
    for nvr in nvrs:
        for p in pkgs:
            if nvr == p or nvr.startswith(p + "-"):
                out.append(nvr)
                break
    return out


def fetch_one(nvr: str, args) -> FetchResult:
    pdir = Path(args.dir) / nvr
    pdir.mkdir(parents=True, exist_ok=True)
    cmd = ["koji", "download-build"]
    for arch in args.arch:
        cmd += ["--arch", arch]
    cmd.append(nvr)
    if args.verbose:
        log(f"+ (cd {pdir} && {' '.join(cmd)})")
    log(f"{PROG}: fetching {nvr}")
    t0 = time.monotonic()
    rc, out = run(cmd, cwd=pdir)
    elapsed = time.monotonic() - t0
    logfile = Path(args.dir) / "logs" / f"{nvr}.fetch.log"
    logfile.write_text(out)
    if rc == 0:
        n_rpms = sum(1 for p in pdir.glob("*.rpm") if p.is_file())
        log(f"[fetch]  {nvr} ok ({n_rpms} RPMs, {elapsed:.1f}s)")
        if not args.keep:
            logfile.unlink(missing_ok=True)
    else:
        log(f"[fetch]  {nvr} FAIL (exit {rc}; log: {logfile})")
    return FetchResult(nvr=nvr, ok=(rc == 0), exit_code=rc, log=logfile)


# ---------------------------------------------------------------------------
# test phase (tarpm round-trip)
# ---------------------------------------------------------------------------

def arch_selected(arch: str, archs: list) -> bool:
    if not archs:
        return True
    if arch in archs:
        return True
    # noarch (and src) are not real arches
    return arch in ("noarch", "src")


def build_units(bdir: Path, args) -> list:
    """RPMs in one build dir (DIR/<NVR>/), per the arch/src filters."""
    units = []
    if not bdir.is_dir():
        return units
    for rpm in sorted(bdir.glob("*.rpm"), key=lambda p: p.name):
        if not rpm.is_file():
            continue
        if rpm.name.endswith(".src.rpm") and args.no_src:
            continue
        arch = rpm.name[:-4].rsplit(".", 1)[-1]
        if arch_selected(arch, args.arch):
            units.append(rpm)
    return units


def collect_test_units(args) -> list:
    """All RPMs under the download directory, per the arch/src filters."""
    root = Path(args.dir)
    units = []
    if not root.is_dir():
        return units
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or d.name == "logs":
            continue
        units.extend(build_units(d, args))
    return units


def test_one(rpm: Path, args) -> TestResult:
    base = rpm.name[:-4] if rpm.name.endswith(".rpm") else rpm.name
    nvr = rpm.parent.name
    arch = base.rsplit(".", 1)[-1]
    work = rpm.parent / f".tarpm-{base}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    # Key the log name on the RPM basename so subpackages of one build
    # (7zip, 7zip-reduced, ...) get separate logs.
    logfile = Path(args.dir) / "logs" / f"{base}.test.log"
    buf = [
        f"rpm:     {rpm}",
        f"tarpm:   {shutil.which('tarpm') or 'tarpm'}",
        f"started: {now_utc()}",
        "-" * 40,
    ]
    failed_stage = None
    failed_rc = None
    repacked = work / f"{base}-repacked.rpm"

    def stage(name: str, cmd: list) -> bool:
        nonlocal failed_stage, failed_rc
        if args.verbose:
            log("+ " + " ".join(str(c) for c in cmd))
        rc, out = run(cmd)
        buf.append("$ " + " ".join(str(c) for c in cmd))
        if out.strip():
            buf.append(out.rstrip("\n"))
        if rc != 0:
            failed_stage = name
            failed_rc = rc
            return False
        return True

    ok = stage("unpack", ["tarpm", "-x", "-f", str(rpm), "-O", str(work / "ext")])
    if ok:
        ok = stage("repack", ["tarpm", "-c", "-f", str(repacked), str(work / "ext")])
    if ok:
        ok = stage("unpack-repacked",
                   ["tarpm", "-x", "-f", str(repacked), "-O", str(work / "ext2")])

    # Extra check: file list of original vs repacked (warning only, in case
    # tarpm -t output is noisy).
    warning = None
    if ok:
        _, list_orig = run(["tarpm", "-t", "-f", str(rpm)])
        _, list_repacked = run(["tarpm", "-t", "-f", str(repacked)])
        (work / "list-orig.txt").write_text(list_orig)
        (work / "list-repacked.txt").write_text(list_repacked)
        if list_orig != list_repacked:
            warning = "content-diff"
            buf.append("content check: file lists differ (warning only)")
            buf.append("\n".join(difflib.unified_diff(
                list_orig.splitlines(), list_repacked.splitlines(),
                fromfile="original", tofile="repacked", lineterm="")))

    logfile.write_text("\n".join(buf) + "\n")

    res = TestResult(rpm=str(rpm), nvr=nvr, arch=arch, ok=ok,
                     stage=failed_stage, exit_code=failed_rc,
                     warning=warning, log=logfile)
    if not ok:
        log(f"[test]   {base} FAIL (stage: {failed_stage}, exit {failed_rc}; "
              f"log: {logfile})")
    elif warning:
        log(f"[test]   {base} ok (warning: {warning}; log: {logfile})")
    else:
        log(f"[test]   {base} ok")

    # On success drop the work dir and the original RPM unless --keep was
    # requested. The test log goes with them on a clean pass; it is kept
    # when it records something to investigate — a failure (commands and
    # output) or a warning (the only copy of the content diff).
    if ok and not args.keep:
        if not warning:
            logfile.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        rpm.unlink(missing_ok=True)
    return res


def process_build(nvr: str, args) -> tuple:
    """One build, start to finish: fetch, tarpm-test its RPMs, clean up.

    Keeps disk use bounded: each build's RPMs are present only from the
    download until its tests finish. Passing RPMs are removed (unless
    --keep); a build whose RPMs all passed is removed entirely. Failures
    keep their RPMs and work dirs.
    """
    fetch = fetch_one(nvr, args)
    bdir = Path(args.dir) / nvr
    tests = [test_one(rpm, args) for rpm in build_units(bdir, args)]
    if not args.keep and bdir.is_dir() and not any(bdir.iterdir()):
        bdir.rmdir()
    return fetch, tests


def run_pool(worker, items, jobs: int) -> tuple:
    """Run worker(item) on a pool of `jobs` workers, collecting results.

    Returns (results, interrupted). Ctrl+C drops the queued items and
    lets the in-flight one(s) finish; a second Ctrl+C while waiting
    exits immediately (no report).
    """
    results = []
    interrupted = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(worker, item) for item in items]
        collected = set()
        try:
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
                collected.add(fut)
        except KeyboardInterrupt:
            interrupted = True
            log(f"{PROG}: interrupted: finishing in-flight work, then "
                  f"stopping (Ctrl+C again to force-quit)", err=True)
            for f in futures:
                f.cancel()
            for f in futures:
                if f in collected or f.cancelled():
                    continue
                try:
                    results.append(f.result())
                except KeyboardInterrupt:
                    raise
    return results, interrupted


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def tail(text: str, n: int) -> str:
    lines = text.splitlines()
    if not lines:
        return "    (empty)"
    return "\n".join("    " + line for line in lines[-n:])


def rpm_label(rpm: str) -> str:
    """Display name for an RPM result: filename without the .rpm suffix."""
    name = Path(rpm).name
    return name[:-4] if name.endswith(".rpm") else name


def write_report(args, fetch: list | None, test: list | None,
                 runtime: float, tag_source: str | None = None,
                 interrupted: bool = False, total_selected: int = 0) -> Path:
    report = Path(args.report)
    fetch_failed = [r for r in (fetch or []) if not r.ok]
    test_failed = [r for r in (test or []) if not r.ok]
    test_warn = [r for r in (test or []) if r.ok and r.warning]

    L = []
    A = L.append
    A(f"=== {PROG} report ===")
    A(f"generated: {now_utc()}")
    A(f"tag:       {args.tag}")
    A(f"runtime:   {fmt_duration(runtime)}")
    if interrupted:
        A("interrupted: yes (Ctrl+C; partial results)")
    if fetch is not None:
        A("--- fetch ---")
        if tag_source:
            A(f"tag list: {tag_source}")
        A(f"selected: {total_selected or len(fetch)} build(s)")
        if interrupted:
            A(f"completed: {len(fetch)} build(s)")
        A(f"ok:       {len(fetch) - len(fetch_failed)}")
        A(f"failed:   {len(fetch_failed)}")
    if test is not None:
        A("--- test (tarpm unpack -> repack -> unpack) ---")
        A(f"rpms:     {len(test)}")
        A(f"ok:       {len(test) - len(test_failed)}")
        A(f"failed:   {len(test_failed)}")
        A(f"warnings: {len(test_warn)}")
        if not args.keep:
            A(f"removed:  {len(test) - len(test_failed)} passed RPM(s) "
              f"(failures kept)")
    A(f"downloads: {args.dir}")

    if fetch is not None and fetch_failed:
        A("")
        A("=== FETCH FAILURES ===")
        for r in fetch_failed:
            A(f"--- {r.nvr} (exit {r.exit_code}) ---")
            if r.log is not None and r.log.is_file():
                A(f"log: {r.log}")
                A(tail(r.log.read_text(), 15))
            else:
                A("    (no log file recorded)")
            A("")

    if test is not None and test_failed:
        A("")
        A("=== TEST FAILURES ===")
        for r in test_failed:
            A(f"--- {rpm_label(r.rpm)} (stage: {r.stage}, exit {r.exit_code}) ---")
            if r.log is not None and r.log.is_file():
                A(f"log: {r.log}")
                A(tail(r.log.read_text(), 20))
            else:
                A("    (no log file recorded)")
            A("")

    if test is not None and test_warn:
        A("")
        A("=== TEST WARNINGS (non-fatal) ===")
        for r in test_warn:
            A(f"- {rpm_label(r.rpm)}: {r.warning} (see {r.log})")

    report.write_text("\n".join(L) + "\n")
    return report


def write_json(path: Path, args, fetch: list | None, test: list | None) -> None:
    def ser(results):
        return [dataclasses.asdict(r) for r in (results or [])]

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "tag": args.tag,
        "downloads": args.dir,
        "fetch": ser(fetch),
        "test": ser(test),
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog=PROG,
        allow_abbrev=False,
        description="List builds in a Koji tag, fetch their RPMs, and "
                    "tarpm-test each one.",
        epilog="examples:\n"
               "  %(prog)s --list -t f44 -p 7zip              # list the NVRs that would be fetched\n"
               "  %(prog)s -t f44 -j 4 -p 7zip -p 389-ds-base # test runs by default\n"
               "  %(prog)s -t f44 -j 4 -p 7zip --no-test      # fetch only\n"
               "  %(prog)s -t f44 -j 4 -p 7zip --no-src       # skip .src.rpm\n"
               "  %(prog)s --test-only -a x86_64 -j 8\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # fetch
    ap.add_argument("-t", "--tag", default="f44",
                    help="Koji tag to query (default: f44)")
    ap.add_argument("-p", "--pkg", action="append", metavar="PKG",
                    help="include builds whose NVR equals PKG or starts with "
                         "PKG- (repeatable; default: every package)")
    ap.add_argument("-a", "--arch", action="append", metavar="ARCH",
                    help="fetch: restrict koji download-build to ARCH; test: "
                         "only test RPMs for ARCH (plus noarch and src; "
                         "repeatable)")
    ap.add_argument("-n", "--limit", type=int, default=0,
                    help="only process the first N builds (smoke test)")
    ap.add_argument("--list", action="store_true",
                    help="print the NVRs that would be fetched and exit "
                         "without downloading")
    ap.add_argument("--from-file", metavar="FILE",
                    help="read the NVR list from FILE (same format as "
                         "'koji latest-pkg --all TAG') instead of the "
                         "cached list or Koji")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch the tag list from Koji, even if a "
                         "cached copy (DIR/tag-<TAG>.txt) exists")
    # test
    ap.add_argument("--no-test", dest="no_test", action="store_true",
                    help="fetch only; skip the tarpm test phase")
    ap.add_argument("--test-only", action="store_true",
                    help="skip fetching; test the RPMs already in DIR")
    ap.add_argument("--no-src", dest="no_src", action="store_true",
                    help="skip .src.rpm files in the test (they are "
                         "tested by default)")
    ap.add_argument("--keep", action="store_true",
                    help="keep per-RPM work dirs, logs, and original RPMs "
                         "even for passing RPMs (default: removed on a "
                         "clean pass; kept on failure or warning)")
    # common
    ap.add_argument("-d", "--dir", default="rpms",
                    help="download directory (default: ./rpms)")
    ap.add_argument("-r", "--report", default="report.txt",
                    help="report file (default: ./report.txt)")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="number of parallel fetches/tests (default: 1)")
    ap.add_argument("--json", metavar="FILE",
                    help="also write the results as JSON to FILE")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo each external command")
    args = ap.parse_args(argv)
    args.pkg = args.pkg or []
    args.arch = args.arch or []
    if args.jobs < 1:
        ap.error("-j/--jobs expects a positive integer")
    if args.limit < 0:
        ap.error("-n/--limit expects a non-negative integer")
    if args.no_test and args.test_only:
        ap.error("--no-test is incompatible with --test-only")
    if args.refresh and args.from_file:
        ap.error("--refresh is incompatible with --from-file")
    return args


def main(argv=None) -> int:
    t0 = time.monotonic()
    args = parse_args(argv)
    do_fetch = not args.test_only
    # The tarpm test is the default; opt out with --no-test.
    do_test = args.test_only or (not args.no_test)

    if args.test_only and not Path(args.dir).is_dir():
        log(f"{PROG}: error: --test-only: no download directory to test: "
              f"{args.dir}", err=True)
        return 2

    if do_test and shutil.which("tarpm") is None:
        log(f"{PROG}: error: tarpm not found in PATH (needed for the "
              f"test phase; use --no-test to fetch only", err=True)
        return 2

    tag_source = None
    fetch_results: list = []
    test_results: list = []
    interrupted = False
    total_selected = 0
    if do_fetch:
        try:
            nvrs, tag_source = read_nvrs(args)
        except UsageError as exc:
            log(f"{PROG}: error: {exc}", err=True)
            return 2
        if not nvrs:
            log(f"{PROG}: error: no builds found (empty package list for "
                  f"tag {args.tag}", err=True)
            return 2
        selected = select_nvrs(nvrs, args.pkg)
        if not selected:
            log(f"{PROG}: error: no builds matched the -p filters: "
                  f"{' '.join(args.pkg)}", err=True)
            return 2
        if args.limit > 0:
            selected = selected[:args.limit]

        if args.list:
            for nvr in selected:
                print(nvr)
            log(f"{PROG}: {len(selected)} build(s) selected from tag "
                  f"{args.tag}", err=True)
            return 0

        (Path(args.dir) / "logs").mkdir(parents=True, exist_ok=True)
        total_selected = len(selected)
        if do_test:
            # Default: pipeline per build — fetch, test, clean up — with
            # -j builds in flight, so disk use stays bounded to the builds
            # in flight plus the failures.
            log(f"{PROG}: fetching and testing {len(selected)} build(s) "
                  f"from tag {args.tag} (parallelism: {args.jobs})")
            results, interrupted = run_pool(
                lambda nvr: process_build(nvr, args), selected, args.jobs)
            for fetch, tests in results:
                fetch_results.append(fetch)
                test_results.extend(tests)
        else:
            log(f"{PROG}: fetching {len(selected)} build(s) from tag "
                  f"{args.tag} (parallelism: {args.jobs})")
            results, interrupted = run_pool(
                lambda nvr: fetch_one(nvr, args), selected, args.jobs)
            fetch_results.extend(results)
    elif do_test:
        # --test-only: test the RPMs already under DIR.
        (Path(args.dir) / "logs").mkdir(parents=True, exist_ok=True)
        units = collect_test_units(args)
        if not units:
            log(f"{PROG}: error: no RPMs found to test under {args.dir}",
                  err=True)
            return 2
        log(f"{PROG}: testing {len(units)} RPM(s) with tarpm "
              f"(parallelism: {args.jobs})")
        results, interrupted = run_pool(
            lambda rpm: test_one(rpm, args), units, args.jobs)
        test_results.extend(results)

        # Remove build dirs whose RPMs all passed and were removed above.
        if not args.keep:
            for d in sorted(Path(args.dir).iterdir(), key=lambda p: p.name):
                if d.is_dir() and d.name != "logs" and not any(d.iterdir()):
                    d.rmdir()

    fetch_for_report = fetch_results if do_fetch else None
    test_for_report = test_results if do_test else None
    report = write_report(args, fetch_for_report, test_for_report,
                          time.monotonic() - t0, tag_source,
                          interrupted=interrupted,
                          total_selected=total_selected)
    if args.json:
        write_json(Path(args.json), args, fetch_for_report, test_for_report)

    any_failed = False
    if do_fetch:
        nf = sum(1 for r in fetch_results if not r.ok)
        any_failed = any_failed or nf > 0
        if interrupted:
            log(f"{PROG}: fetch: {len(fetch_results)} of "
                  f"{total_selected} build(s) completed, "
                  f"{len(fetch_results) - nf} ok, {nf} failed "
                  f"(interrupted).")
        else:
            log(f"{PROG}: fetch: {len(fetch_results)} build(s) selected, "
                  f"{len(fetch_results) - nf} ok, {nf} failed.")
    if do_test:
        nt = sum(1 for r in test_results if not r.ok)
        nw = sum(1 for r in test_results if r.ok and r.warning)
        any_failed = any_failed or nt > 0
        log(f"{PROG}: test:  {len(test_results)} RPM(s), "
              f"{len(test_results) - nt} ok, {nt} failed, {nw} warning(s).")
        if not args.keep and len(test_results) - nt:
            log(f"{PROG}: removed {len(test_results) - nt} passed RPM(s) "
                  f"from {args.dir} (failures kept)")
    log(f"{PROG}: report: {report}")

    if any_failed:
        for r in fetch_for_report or []:
            if not r.ok:
                log(f"{PROG}: fetch failure: {r.nvr}")
        for r in test_for_report or []:
            if not r.ok:
                log(f"{PROG}: test failure:  {r.rpm}")
        return 1
    if interrupted:
        return 130
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log(f"{PROG}: interrupted (Ctrl+C)", err=True)
        sys.exit(130)
