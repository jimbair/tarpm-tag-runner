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
  4. tarpm -t on both + compare                      content check (warning;
     a listing that fails to generate is a failed stage)

After a clean pass the work dir, the original RPM, and the test log are
removed. A failure keeps the RPM, work dir, and log for inspection; a pass
with a warning keeps the log (the only copy of the content diff). --keep
retains everything for passing RPMs as well.

Ctrl+C stops gracefully: queued work is dropped, in-flight work
finishes, the partial results are written to the report, and the exit
status is 130 (which takes precedence over 1). External commands run in
their own session, so they survive Ctrl+C and finish; a second Ctrl+C
while waiting force-quits and may orphan in-flight child processes
(they keep running to completion).

Exit status: 0 all ok, 1 some fetches/tests failed, 2 usage/environment,
130 interrupted by Ctrl+C (partial results reported; takes precedence
over 1).

Python 3.9+ (standard library only).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import difflib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
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


def human(n: int) -> str:
    """Binary (1024) human size: '950 KiB', '838 MiB', '2.9 GiB'."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{int(n)} B"
    txt = f"{v:.1f}"
    if txt.endswith(".0"):
        txt = txt[:-2]
    return f"{txt} {units[i]}"


def _dir_bytes(pdir: Path) -> int:
    """Sum of top-level file sizes in a dir (a fetch's 'size so far')."""
    total = 0
    try:
        for p in pdir.iterdir():
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def log(msg: str, err: bool = False) -> None:
    """Emit a UTC-timestamped log line; err=True routes it to stderr."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} UTC] {msg}", file=sys.stderr if err else sys.stdout,
          flush=True)


def _progress(progress) -> str:
    """' ( 7/20)' suffix; numerator right-aligned to the total's width."""
    if not progress:
        return ""
    n, total = progress
    return f" ({n:>{len(str(total))}d}/{total})"


def _test_tail(progress, elapsed: float) -> str:
    """'(n/total, DURATION)' or '(DURATION)' suffix for a test line."""
    parts = []
    if progress:
        n, total = progress
        parts.append(f"{n:>{len(str(total))}d}/{total}")
    parts.append(fmt_duration(elapsed))
    return " (" + ", ".join(parts) + ")"


class Counter:
    """Thread-safe monotonic counter shared across worker threads.

    next() returns 1, 2, ... as it is called; the lock keeps it correct
    when -j runs items on several threads.
    """
    def __init__(self, total: int):
        self.total = total
        self.n = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self.n += 1
            return self.n


def run(cmd: list, cwd: Path | None = None, label: str | None = None,
        heartbeat: int = 0, counter_suffix: str = "", suffix_fn=None,
        start: float | None = None) -> tuple:
    """Run an external command; return (exit_code, combined stdout+stderr).

    The child runs in a new session so a Ctrl+C (SIGINT to the terminal's
    foreground process group) reaches only this process; a force-quit may
    orphan the child. A KeyboardInterrupt while waiting kills and reaps the
    child before re-raising (so an interrupted main-thread command does not
    leave the child running).

    If `heartbeat` > 0 and `label` is set, a line is logged every
    `heartbeat` seconds while the command is still running, so a slow
    command does not look stuck:
        still <label><counter_suffix> (<fmt_duration>)<suffix_fn()>
    `counter_suffix` is the '(n/total)' fetch progress; `suffix_fn()` may
    return a ', <size> so far' suffix. `start` (a monotonic timestamp) is
    the reference for the elapsed time (e.g. the start of a multi-stage test);
    it defaults to now.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    t0 = start if start is not None else time.monotonic()
    try:
        while True:
            try:
                # communicate(timeout) has a hard deadline, so the heartbeat
                # fires even for a silent command; on TimeoutExpired it does
                # not kill the child, and the retry continues where it left
                # off. Only the final call's output is used: each
                # TimeoutExpired carries the cumulative partial output, so
                # accumulating would duplicate lines.
                out, _ = (proc.communicate(timeout=heartbeat)
                          if heartbeat > 0 else proc.communicate())
                break
            except subprocess.TimeoutExpired:
                suffix = suffix_fn() if suffix_fn else ""
                log(f"{PROG}: still {label}{counter_suffix} "
                    f"({fmt_duration(time.monotonic() - t0)}{suffix})")
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return proc.returncode, out or ""


@dataclasses.dataclass
class FetchResult:
    nvr: str
    ok: bool
    exit_code: int | None = None
    log: Path | None = None
    error: str | None = None         # worker exception text, if any


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
    error: str | None = None          # worker exception text, if any


# ---------------------------------------------------------------------------
# fetch phase
# ---------------------------------------------------------------------------

def read_nvrs(args) -> tuple:
    """Return (NVR list, tag-list source) for the run.

    The tag list is cached as <dir>/tag-<TAG>.txt (raw 'koji latest-pkg
    --all' output) and reused on later runs; --refresh forces a re-fetch.
    """
    cache = None
    saved = None
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
            try:
                saved = datetime.fromtimestamp(
                    cache.stat().st_mtime, timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC")
            except OSError:
                saved = "date unknown"
            source = f"cached ({cache}, saved {saved})"
        else:
            if shutil.which("koji") is None:
                raise UsageError("koji client not found in PATH")
            log(f"{PROG}: fetching koji tag {args.tag}")
            t0 = time.monotonic()
            rc, out = run(["koji", "latest-pkg", "--all", args.tag],
                          label=f"fetching koji tag {args.tag}",
                          heartbeat=args.heartbeat)
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
        log(f"{PROG}: tag {args.tag}: using cached list ({len(nvrs)} packages, "
            f"saved {saved})")
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


def fetch_one(nvr: str, args, counter: "Counter | None" = None) -> FetchResult:
    pdir = Path(args.dir) / nvr
    pdir.mkdir(parents=True, exist_ok=True)
    cmd = ["koji", "download-build"]
    if args.arch:
        # koji matches the requested arch exactly, which would drop the
        # noarch and src RPMs; add them back (src unless --no-src) so the
        # fetch matches what the test phase covers.
        wanted = list(args.arch) + ["noarch"]
        if not args.no_src:
            wanted.append("src")
        for arch in dict.fromkeys(wanted):
            cmd += ["--arch", arch]
    cmd.append(nvr)
    if args.verbose:
        log(f"+ (cd {pdir} && {' '.join(cmd)})")
    if counter is not None:
        idx = counter.next()
        counter_suffix = _progress((idx, counter.total))
        log(f"{PROG}: fetching {nvr}{counter_suffix}")
    else:
        counter_suffix = ""
        log(f"{PROG}: fetching {nvr}")
    t0 = time.monotonic()
    rc, out = run(cmd, cwd=pdir,
                  label=f"fetching {nvr}",
                  heartbeat=args.heartbeat,
                  counter_suffix=counter_suffix,
                  suffix_fn=lambda: f", {human(_dir_bytes(pdir))} so far")
    elapsed = time.monotonic() - t0
    logfile = Path(args.dir) / "logs" / f"{nvr}.fetch.log"
    logfile.write_text(out)
    if rc == 0:
        n_rpms = sum(1 for p in pdir.glob("*.rpm") if p.is_file())
        log(f"[fetch]  {nvr} ok ({n_rpms} RPMs, {human(_dir_bytes(pdir))}, "
            f"{fmt_duration(elapsed)})")
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
    """Build RPMs under the download dir, per the pkg/limit/arch/src filters.

    The -p and -n filters apply to the build dirs (NVRs) already under
    DIR, the same way they apply to the tag list in a fetch run.
    """
    root = Path(args.dir)
    units = []
    if not root.is_dir():
        return units
    builds = 0
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or d.name == "logs":
            continue
        if args.pkg and not any(d.name == p or d.name.startswith(p + "-")
                                for p in args.pkg):
            continue
        builds += 1
        if args.limit and builds > args.limit:
            break
        units.extend(build_units(d, args))
    return units


def test_one(rpm: Path, args, progress=None) -> TestResult:
    base = rpm.name[:-4] if rpm.name.endswith(".rpm") else rpm.name
    nvr = rpm.parent.name
    arch = base.rsplit(".", 1)[-1]
    work = rpm.parent / f".tarpm-{base}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    test_t0 = time.monotonic()

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
        rc, out = run(cmd, label=f"testing {nvr}.{arch} ({name})",
                      heartbeat=args.heartbeat, start=test_t0)
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
    # tarpm -t output is noisy). A listing that fails to generate is a
    # failed stage, not a diff to compare.
    warning = None
    if ok:
        if args.verbose:
            log("+ tarpm -t -f " + str(rpm))
        rc, list_orig = run(["tarpm", "-t", "-f", str(rpm)])
        (work / "list-orig.txt").write_text(list_orig)
        if rc != 0:
            ok = False
            failed_stage = "list-original"
            failed_rc = rc
            buf.append("$ tarpm -t -f " + str(rpm))
            buf.append(list_orig.strip() or "(no output)")
    if ok:
        if args.verbose:
            log("+ tarpm -t -f " + str(repacked))
        rc, list_repacked = run(["tarpm", "-t", "-f", str(repacked)])
        (work / "list-repacked.txt").write_text(list_repacked)
        if rc != 0:
            ok = False
            failed_stage = "list-repacked"
            failed_rc = rc
            buf.append("$ tarpm -t -f " + str(repacked))
            buf.append(list_repacked.strip() or "(no output)")
    if ok and list_orig != list_repacked:
        warning = "content-diff"
        buf.append("content check: file lists differ (warning only)")
        buf.append("\n".join(difflib.unified_diff(
            list_orig.splitlines(), list_repacked.splitlines(),
            fromfile="original", tofile="repacked", lineterm="")))

    logfile.write_text("\n".join(buf) + "\n")

    res = TestResult(rpm=str(rpm), nvr=nvr, arch=arch, ok=ok,
                     stage=failed_stage, exit_code=failed_rc,
                     warning=warning, log=logfile)
    tail = _test_tail(progress, time.monotonic() - test_t0)
    if not ok:
        log(f"[test]   {base} FAIL (stage: {failed_stage}, exit {failed_rc}; "
              f"log: {logfile}){tail}")
    elif warning:
        log(f"[test]   {base} ok (warning: {warning}; log: {logfile}){tail}")
    else:
        log(f"[test]   {base} ok{tail}")

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


def process_build(nvr: str, args, fetch_counter=None) -> tuple:
    """One build, start to finish: fetch, tarpm-test its RPMs, clean up.

    Keeps disk use bounded: each build's RPMs are present only from the
    download until its tests finish. Passing RPMs are removed (unless
    --keep); a build whose RPMs all passed is removed entirely. Failures
    keep their RPMs and work dirs.
    """
    fetch = fetch_one(nvr, args, fetch_counter)
    if not fetch.ok:
        # A failed or partial download is not valid test input; the RPMs
        # and the fetch log are kept for inspection and can be tested
        # later with --test-only.
        return fetch, []
    bdir = Path(args.dir) / nvr
    units = build_units(bdir, args)
    total = len(units)
    tests = []
    for i, rpm in enumerate(units, start=1):
        try:
            tests.append(test_one(rpm, args, progress=(i, total)))
        except Exception as exc:
            # A runner-side exception (e.g. disk full) is a failure for
            # this RPM only: the fetch itself succeeded, so record it as
            # a test failure and keep testing the rest.
            base = rpm.name[:-4] if rpm.name.endswith(".rpm") else rpm.name
            log(f"{PROG}: error testing {rpm}: {exc}{_progress((i, total))}",
                err=True)
            tests.append(TestResult(
                rpm=str(rpm), nvr=nvr,
                arch=base.rsplit(".", 1)[-1],
                ok=False, stage="exception", error=repr(exc)))
    if not args.keep and bdir.is_dir() and not any(bdir.iterdir()):
        bdir.rmdir()
    return fetch, tests


def _force_quit(signum, frame) -> None:
    """Second Ctrl+C: die immediately, no cleanup (os._exit).

    In-flight external commands (koji/tarpm) run in their own sessions
    and keep running to completion, orphaned (findable with pgrep).
    """
    sys.stderr.write(
        f"{PROG}: force-quit; in-flight koji/tarpm keep running "
        f"(orphaned)\n")
    sys.stderr.flush()
    os._exit(130)


def run_pool(worker, items, jobs: int, on_error=None) -> tuple:
    """Run worker(item) on a pool of `jobs` workers, collecting results.

    Returns (results, interrupted). Ctrl+C drops the queued items and
    lets the in-flight one(s) finish; a second Ctrl+C while waiting
    force-quits immediately (no report; in-flight children are orphaned
    and keep running).

    If worker(item) raises and on_error is given, on_error(item, exc)
    is called and its return value is used as that item's result, so a
    single bad item cannot abort the whole run.
    """
    def call(item):
        try:
            return worker(item)
        except Exception as exc:
            if on_error is None:
                raise
            log(f"{PROG}: error processing {item}: {exc}", err=True)
            return on_error(item, exc)

    results = []
    interrupted = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(call, item) for item in items]
        collected = set()
        try:
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
                collected.add(fut)
        except KeyboardInterrupt:
            interrupted = True
            # Any further Ctrl+C now force-quits immediately (os._exit)
            # instead of unwinding into the executor's shutdown, which
            # would wait for the in-flight children to finish.
            signal.signal(signal.SIGINT, _force_quit)
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
            if r.exit_code is not None:
                A(f"--- {r.nvr} (exit {r.exit_code}) ---")
            else:
                A(f"--- {r.nvr} ---")
            if r.error:
                A(f"    error: {r.error}")
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
            if r.exit_code is not None:
                A(f"--- {rpm_label(r.rpm)} (stage: {r.stage}, "
                  f"exit {r.exit_code}) ---")
            else:
                A(f"--- {rpm_label(r.rpm)} (stage: {r.stage}) ---")
            if r.error:
                A(f"    error: {r.error}")
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
                    help="fetch: download ARCH plus noarch and the source "
                         "RPM (src unless --no-src); test: only test RPMs "
                         "for ARCH (plus noarch and src). Repeatable.")
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
    ap.add_argument("--heartbeat", type=int, default=60,
                    help="while a command runs longer than this, log a "
                         "progress line every N seconds (0 disables; "
                         "default: 60)")
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
    if args.heartbeat < 0:
        ap.error("--heartbeat expects a non-negative integer")
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

    if do_test and not args.list and shutil.which("tarpm") is None:
        log(f"{PROG}: error: tarpm not found in PATH (needed for the "
              f"test phase; use --no-test to fetch only)", err=True)
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
                  f"tag {args.tag})", err=True)
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
            fetch_counter = Counter(len(selected))
            results, interrupted = run_pool(
                lambda nvr: process_build(nvr, args, fetch_counter),
                selected, args.jobs,
                on_error=lambda nvr, exc: (
                    FetchResult(nvr=nvr, ok=False, error=repr(exc)), []))
            for fetch, tests in results:
                fetch_results.append(fetch)
                test_results.extend(tests)
        else:
            log(f"{PROG}: fetching {len(selected)} build(s) from tag "
                  f"{args.tag} (parallelism: {args.jobs})")
            fetch_counter = Counter(len(selected))
            results, interrupted = run_pool(
                lambda nvr: fetch_one(nvr, args, fetch_counter),
                selected, args.jobs,
                on_error=lambda nvr, exc: FetchResult(nvr=nvr, ok=False,
                                                     error=repr(exc)))
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
        test_counter = Counter(len(units))
        results, interrupted = run_pool(
            lambda rpm: test_one(rpm, args,
                                 progress=(test_counter.next(),
                                           test_counter.total)),
            units, args.jobs,
            on_error=lambda rpm, exc: TestResult(
                rpm=str(rpm), nvr=rpm.parent.name,
                arch=(rpm.name[:-4] if rpm.name.endswith(".rpm")
                      else rpm.name).rsplit(".", 1)[-1],
                ok=False, stage="exception", error=repr(exc)))
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
    if interrupted:
        # An interrupted run exits 130 even if it also had failures.
        return 130
    if any_failed:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log(f"{PROG}: interrupted (Ctrl+C)", err=True)
        sys.exit(130)
