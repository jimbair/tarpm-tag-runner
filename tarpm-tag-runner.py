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
  --no-src     skip .src.rpm; discard any sources fetched by Koji

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
while waiting force-quits immediately and orphans the in-flight
koji/tarpm — observed behavior is that they die on their next write to
the now-closed output pipe, possibly leaving a truncated RPM, so do not
trust files from a force-quit run (continue with --resume, which also
re-downloads builds with no journal record, or delete their DIR/<NVR>/
directories before re-running).

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
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
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


def error_signature(text: str) -> str:
    """A short, stable key identifying a failure, for grouping.

    Takes the last line starting with "tarpm: " from the failing
    command's output (the prefix stripped); if there is no such line,
    the last non-empty line. Absolute paths are replaced with <path>,
    hex addresses and 4+ digit runs with N, and the result is truncated
    to 120 characters, so the same failure groups together across
    RPMs, paths, and runs.
    """
    lines = text.splitlines()
    msg = None
    for line in lines:
        if line.startswith("tarpm: "):
            msg = line[len("tarpm: "):]
    if msg is None:
        msg = ""
        for line in lines:
            if line.strip():
                msg = line
    msg = msg.strip()
    msg = re.sub(r"((?:^|\s))/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+/?",
                 r"\1<path>", msg)
    msg = re.sub(r"0[xX][0-9A-Fa-f]+", "N", msg)
    msg = re.sub(r"\d{4,}", "N", msg)
    return msg[:120]


class RunLog:
    """Tee every log line to a file, appended across invocations.

    Each line is one write() + flush() under a lock, so lines from
    concurrent worker threads never interleave. An open or write failure
    must never abort the run: if the file cannot be used, the run simply
    proceeds without it.
    """

    def __init__(self) -> None:
        self.path: str | None = None
        self.enabled = False
        self._fh = None
        self._lock = threading.Lock()

    def open(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
            self.path = path
            self.enabled = True
        except OSError:
            self._fh = None
            self.enabled = False

    def write(self, line: str) -> None:
        if self._fh is None:
            return
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


RUNLOG = RunLog()


def log(msg: str, err: bool = False) -> None:
    """Emit a UTC-timestamped log line; err=True routes it to stderr.

    The line is also tee'd to the run log (RUNLOG) when one is enabled.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts} UTC] {msg}"
    print(line, file=sys.stderr if err else sys.stdout, flush=True)
    RUNLOG.write(line)


def _progress(progress) -> str:
    """' (7/20)' suffix for the fetch/heartbeat progress lines."""
    if not progress:
        return ""
    n, total = progress
    return f" ({n}/{total})"


def _test_tail(progress, elapsed: float) -> str:
    """'(n/total, DURATION)' or '(DURATION)' suffix for a test line."""
    parts = []
    if progress:
        n, total = progress
        parts.append(f"{n}/{total}")
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
        start: float | None = None, log_err: bool = False) -> tuple:
    """Run an external command; return (exit_code, combined stdout+stderr).

    The child runs in a new session so a Ctrl+C (SIGINT to the terminal's
    foreground process group) reaches only this process; a force-quit may
    orphan the child. A KeyboardInterrupt while waiting kills and reaps the
    child before re-raising (so an interrupted main-thread command does not
    leave the child running).

    If `heartbeat` > 0 and `label` is set, a line is logged every
    `heartbeat` seconds while the command is still running, so a slow
    command does not look stuck:
        still <label><counter_suffix> (<fmt_duration><suffix_fn(output)>)
    `counter_suffix` is the '(n/total)' fetch progress; `suffix_fn(output)`
    receives cumulative partial output (bytes, str, or None) and returns
    extra progress text. `start` (a monotonic timestamp) is
    the reference for the elapsed time (e.g. the start of a multi-stage test);
    it defaults to now.
    `log_err` routes heartbeat diagnostics to stderr for machine-readable
    stdout, such as --list output.
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
            except subprocess.TimeoutExpired as exc:
                suffix = suffix_fn(exc.output) if suffix_fn else ""
                log(f"{PROG}: still {label}{counter_suffix} "
                    f"({fmt_duration(time.monotonic() - t0)}{suffix})",
                    err=log_err)
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
    signature: str = ""             # error_signature of the failing output


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
    signature: str = ""             # error_signature of the failing output


# ---------------------------------------------------------------------------
# fetch phase
# ---------------------------------------------------------------------------

def validate_nvr(nvr: str) -> None:
    """Require a plain RPM name-version-release, never a filesystem path."""
    if not re.fullmatch(
            r"[A-Za-z0-9_+][A-Za-z0-9_+.-]*-[A-Za-z0-9_+.~^]+-[A-Za-z0-9_+.~^]+",
            nvr):
        raise UsageError(f"invalid build NVR: {nvr!r} "
                         "(expected name-version-release, without paths)")


def build_dir(directory: str, nvr: str) -> Path:
    """Return a build path confined to a direct child of the download root."""
    validate_nvr(nvr)
    root = Path(directory)
    path = root / nvr
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise UsageError(f"unsafe build directory: {path} "
                         "(must be a direct child, not a symlink)")
    return path


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
            log(f"{PROG}: fetching koji tag {args.tag}", err=args.list)
            t0 = time.monotonic()
            rc, out = run(["koji", "latest-pkg", "--all", args.tag],
                          label=f"fetching koji tag {args.tag}",
                          heartbeat=args.heartbeat, log_err=args.list)
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
        # skip comment lines, and the table header lines (title row + dashes)
        if nvr.startswith("#") or nvr.startswith("-") or nvr == "Build":
            continue
        validate_nvr(nvr)
        nvrs.append(nvr)
    # Input files can repeat NVRs. Each build must have exactly one worker:
    # duplicate workers share and delete the same RPMs and work directories.
    nvrs = list(dict.fromkeys(nvrs))
    if source.startswith("cached"):
        log(f"{PROG}: tag {args.tag}: using cached list ({len(nvrs)} packages, "
            f"saved {saved})", err=args.list)
    elif source.startswith("fetched"):
        log(f"{PROG}: tag {args.tag}: success ({len(nvrs)} packages "
            f"in {elapsed:.1f}s)", err=args.list)
        log(f"{PROG}: saved tag list to {cache}", err=args.list)
    else:
        log(f"{PROG}: tag {args.tag}: list from {args.from_file} "
            f"({len(nvrs)} packages)", err=args.list)
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


def koji_download_percent(output: bytes | str | None) -> int | None:
    """Read Koji's latest native progress bar for the current RPM, if any."""
    if not output:
        return None
    # TimeoutExpired.output can be bytes even when Popen uses text=True.
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    start = 0
    for heading in re.finditer(r"(?:^|[\r\n])Downloading(?=[: ])", output):
        start = heading.end()
    # A new Downloading heading resets progress: the preceding RPM's 100%
    # must not appear while waiting for the next RPM's first progress bar.
    latest = None
    for bar in re.finditer(r"(?:^|[\r\n])\[[= ]+\]\s*(\d{1,3}|\?{3})%",
                           output[start:]):
        latest = bar.group(1)
    if latest is None or latest == "???":
        return None
    percent = int(latest)
    return percent if 0 <= percent <= 100 else None


def fetch_progress_suffix(pdir: Path, output: bytes | str | None) -> str:
    size = human(_dir_bytes(pdir))
    percent = koji_download_percent(output)
    if percent is None:
        return f", {size} so far"
    return f", {size}, {percent}% of current RPM so far"


def fetch_one(nvr: str, args, counter: "Counter | None" = None) -> FetchResult:
    pdir = build_dir(args.dir, nvr)
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
                  suffix_fn=lambda output: fetch_progress_suffix(pdir, output))
    elapsed = time.monotonic() - t0
    logfile = Path(args.dir) / "logs" / f"{nvr}.fetch.log"
    logfile.write_text(out)
    if rc == 0:
        # Koji has no exclude-source switch. Without an explicit arch
        # filter it downloads src along with all binary arches; discard
        # those skipped RPMs now so --no-src cannot accumulate them.
        if args.no_src:
            for rpm in pdir.glob("*.src.rpm"):
                rpm.unlink()
        n_rpms = sum(1 for p in pdir.glob("*.rpm") if p.is_file())
        log(f"[fetch]  {nvr} ok ({n_rpms} RPMs, {human(_dir_bytes(pdir))}, "
            f"{fmt_duration(elapsed)})")
        if not args.keep:
            logfile.unlink(missing_ok=True)
    else:
        log(f"[fetch]  {nvr} FAIL (exit {rc}; log: {logfile})")
    return FetchResult(nvr=nvr, ok=(rc == 0), exit_code=rc, log=logfile,
                       signature="" if rc == 0 else error_signature(out))


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
        units.extend(build_units(build_dir(args.dir, d.name), args))
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
    failed_out = ""
    repacked = work / f"{base}-repacked.rpm"

    def stage(name: str, cmd: list) -> bool:
        nonlocal failed_stage, failed_rc, failed_out
        if args.verbose:
            log("+ " + " ".join(str(c) for c in cmd))
        rc, out = run(cmd, label=f"testing {base} ({name})",
                      heartbeat=args.heartbeat, start=test_t0,
                      counter_suffix=_progress(progress))
        buf.append("$ " + " ".join(str(c) for c in cmd))
        if out.strip():
            buf.append(out.rstrip("\n"))
        if rc != 0:
            failed_stage = name
            failed_rc = rc
            failed_out = out
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
        rc, list_orig = run(["tarpm", "-t", "-f", str(rpm)],
                            label=f"testing {base} (list-original)",
                            heartbeat=args.heartbeat, start=test_t0,
                            counter_suffix=_progress(progress))
        (work / "list-orig.txt").write_text(list_orig)
        if rc != 0:
            ok = False
            failed_stage = "list-original"
            failed_rc = rc
            failed_out = list_orig
            buf.append("$ tarpm -t -f " + str(rpm))
            buf.append(list_orig.strip() or "(no output)")
    if ok:
        if args.verbose:
            log("+ tarpm -t -f " + str(repacked))
        rc, list_repacked = run(["tarpm", "-t", "-f", str(repacked)],
                                label=f"testing {base} (list-repacked)",
                                heartbeat=args.heartbeat, start=test_t0,
                                counter_suffix=_progress(progress))
        (work / "list-repacked.txt").write_text(list_repacked)
        if rc != 0:
            ok = False
            failed_stage = "list-repacked"
            failed_rc = rc
            failed_out = list_repacked
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
                     warning=warning, log=logfile,
                     signature="" if ok else error_signature(failed_out))
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
    bdir = build_dir(args.dir, nvr)
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

    In-flight external commands (koji/tarpm) run in their own sessions,
    so they are orphaned: observed behavior is that they die on their
    next write to the now-closed output pipe (BrokenPipeError), which
    can leave a truncated RPM — do not trust files from a force-quit.
    """
    msg = (f"{PROG}: force-quit; in-flight koji/tarpm are orphaned and "
           f"typically die on their next write to the closed pipe, "
           f"possibly leaving a truncated RPM. Do not trust files from "
           f"a force-quit run: continue with --resume (which re-downloads "
           f"unfinished builds too), or delete their DIR/<NVR>/ directories "
           f"before re-running")
    # A signal may interrupt a log write while its lock is held. Never use
    # RUNLOG or buffered stderr here; even a full stderr pipe must not
    # prevent the second Ctrl+C from terminating the process.
    try:
        blocking = os.get_blocking(2)
        os.set_blocking(2, False)
        try:
            os.write(2, (msg + "\n").encode("utf-8", errors="replace"))
        finally:
            os.set_blocking(2, blocking)
    except OSError:
        pass
    finally:
        os._exit(130)


def run_pool(worker, items, jobs: int, on_error=None) -> tuple:
    """Run worker(item) on a pool of `jobs` workers, collecting results.

    Returns (results, interrupted). Ctrl+C drops the queued items and
    lets the in-flight one(s) finish; a second Ctrl+C while waiting
    force-quits immediately (no report; in-flight children are
    orphaned and typically die on their next write to the closed pipe,
    possibly leaving partial files).

    If worker(item) raises and on_error is given, on_error(item, exc)
    is called and its return value is used as that item's result, so a
    single bad item cannot abort the whole run.
    """
    results = []
    result_lock = threading.Lock()
    stopping = threading.Event()

    def call(item):
        # A worker can dequeue before the main thread cancels its future.
        # Do not start more build work once an interrupt has been observed.
        if stopping.is_set():
            return
        try:
            result = worker(item)
        except Exception as exc:
            if on_error is None:
                raise
            log(f"{PROG}: error processing {item}: {exc}", err=True)
            result = on_error(item, exc)
        # Collect in the worker so a signal between submit() and tracking
        # its returned future cannot lose a finished build from the report.
        with result_lock:
            results.append(result)

    interrupted = False
    previous_sigint = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = []
            try:
                # Submission is part of the interruptible work too.
                for item in items:
                    futures.append(pool.submit(call, item))
                for fut in concurrent.futures.as_completed(futures):
                    fut.result()
            except KeyboardInterrupt:
                interrupted = True
                stopping.set()
                # A second Ctrl+C while active work finishes force-quits.
                previous_sigint = signal.signal(signal.SIGINT, _force_quit)
                log(f"{PROG}: interrupted: finishing in-flight work, then "
                      f"stopping (Ctrl+C again to force-quit)", err=True)
                for fut in futures:
                    fut.cancel()
                for fut in futures:
                    if not fut.cancelled():
                        fut.result()
            # The executor also joins a submitted future that a signal
            # interrupted before it could be added to futures above.
    finally:
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
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


def failure_types(fetch: list | None, test: list | None,
                  phase: str | None = None) -> list:
    """Group the failures of a run into (stage, exit code, signature) rows.

    Each row is (stage, exit_code, signature, count, examples) with up
    to 3 example NVRs (fetch) or RPM basenames (test). Rows are sorted
    by count descending, then stage, exit code, and signature, so the
    order is stable. `phase` restricts the grouping to "fetch" or
    "test"; None covers both.
    """
    groups: dict = {}

    def add(stage, exit_code, signature, name):
        groups.setdefault((stage, exit_code, signature), []).append(name)

    if phase in (None, "fetch"):
        for r in fetch or []:
            if not r.ok:
                add("fetch", r.exit_code, r.signature, r.nvr)
    if phase in (None, "test"):
        for r in test or []:
            if not r.ok:
                add(r.stage or "unknown", r.exit_code, r.signature,
                    rpm_label(r.rpm))
    rows = []
    for (stage, exit_code, signature), names in groups.items():
        rows.append((stage, exit_code, signature, len(names),
                     sorted(set(names))[:3]))
    rows.sort(key=lambda row: (-row[3], str(row[0]), str(row[1]), row[2]))
    return rows


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
    A(f"run log:   {args.run_log if args.run_log else '- (disabled)'}")

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

    if fetch is not None and fetch_failed:
        A("")
        A("=== FETCH FAILURES BY TYPE ===")
        for stage, rc, sig, count, examples in failure_types(fetch, None,
                                                             "fetch"):
            A(f"- {count}x stage {stage}, exit {rc}: {sig}")
            A(f"    e.g. {', '.join(examples)}")

    if test is not None and test_failed:
        A("")
        A("=== TEST FAILURES BY TYPE ===")
        for stage, rc, sig, count, examples in failure_types(None, test,
                                                             "test"):
            A(f"- {count}x stage {stage}, exit {rc}: {sig}")
            A(f"    e.g. {', '.join(examples)}")

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
# journal (one JSON line per finished build; basis for --resume and
# --retry-failed)
# ---------------------------------------------------------------------------

def read_run_manifest(path: Path) -> dict | None:
    """Read the active run's selection; an invalid manifest is never ignored."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read run manifest {path}: {exc}") from exc
    if (not isinstance(manifest, dict)
            or not isinstance(manifest.get("run_id"), str)
            or not manifest["run_id"]
            or not isinstance(manifest.get("nvrs"), list)
            or not all(isinstance(nvr, str) for nvr in manifest["nvrs"])):
        raise UsageError(f"invalid run manifest: {path}")
    for nvr in manifest["nvrs"]:
        validate_nvr(nvr)
    return manifest


def write_run_manifest(path: Path, nvrs: list, run_id: str) -> None:
    """Atomically replace run state before any worker can modify build files."""
    manifest = {"run_id": run_id, "nvrs": nvrs, "ts": now_utc()}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, prefix=".run-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(manifest, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Journal:
    """Append-only journal of finished builds (one JSON line each).

    Each complete line is written under a lock, retrying short writes.
    A failed append rolls back to its starting offset. If rollback fails,
    further appends are disabled so they cannot join a partial record.
    """

    def __init__(self) -> None:
        self.path: str | None = None
        self._fh = None
        self._lock = threading.Lock()

    def open(self, path: str) -> None:
        self._disable()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "a+b", buffering=0)
            # A crash can leave a partial final line. Separate it from the
            # next record so the reader can discard it without also losing
            # the first completed build of the new run. Preserve a valid
            # final record that merely lacks its newline, too.
            if self._fh.seek(0, os.SEEK_END):
                self._fh.seek(-1, os.SEEK_END)
                if self._fh.read(1) != b"\n":
                    self._write_all(b"\n")
            self.path = path
        except OSError as exc:
            self._disable()
            self.path = None
            log(f"{PROG}: warning: journal disabled: cannot open {path}: {exc}",
                err=True)

    def _disable(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _write_all(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = os.write(self._fh.fileno(), remaining)
            if written <= 0:
                raise OSError("journal write made no progress")
            remaining = remaining[written:]

    def append(self, record: dict) -> None:
        data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        warning = None
        with self._lock:
            if self._fh is None:
                return
            offset = None
            try:
                offset = os.lseek(self._fh.fileno(), 0, os.SEEK_END)
                self._write_all(data)
            except OSError as exc:
                warning = f"could not append journal record for {record.get('nvr')}: {exc}"
                if offset is None:
                    self._disable()
                    warning += "; journal disabled"
                else:
                    try:
                        os.ftruncate(self._fh.fileno(), offset)
                    except OSError as repair_exc:
                        self._disable()
                        warning += f"; journal disabled: rollback failed: {repair_exc}"
        if warning:
            log(f"{PROG}: warning: {warning}", err=True)


JOURNAL = Journal()


def read_journal(path: str, run_id: str | None = None) -> dict:
    """Latest journal record per NVR.

    Lines are processed in file order, so the last valid record for an
    NVR wins; torn or invalid lines (e.g. a truncated final line) are
    skipped. With run_id, only records belonging to that run are considered.
    """
    latest = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("nvr"), str):
                    continue
                if run_id is not None and rec.get("run_id") != run_id:
                    continue
                latest[rec["nvr"]] = rec
    except OSError:
        pass
    return latest


def journal_record(nvr: str, fetch: "FetchResult | None", tests: list,
                   run_id: str | None = None) -> dict:
    """One journal line for a finished build.

    status is fetched, pass, warn, fetch-fail, or test-fail; stage/exit_code/
    signature come from the failing fetch or the first failing test;
    rpms lists the failing (or, for warn, warning) RPM basenames.
    """
    rec = {
        "nvr": nvr,
        # Fetch-only runs (and builds with no selected test RPMs) must
        # remain eligible for testing by a subsequent --resume.
        "status": "pass" if tests else "fetched",
        "stage": "",
        "exit_code": None,
        "signature": "",
        "rpms": [],
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if run_id is not None:
        rec["run_id"] = run_id
    if fetch is not None and not fetch.ok:
        rec.update(status="fetch-fail", stage="fetch",
                   exit_code=fetch.exit_code, signature=fetch.signature)
        return rec
    failed = [t for t in tests if not t.ok]
    if failed:
        first = failed[0]
        rec.update(status="test-fail", stage=first.stage or "unknown",
                   exit_code=first.exit_code, signature=first.signature,
                   rpms=sorted({rpm_label(t.rpm) for t in failed}))
        return rec
    warned = [t for t in tests if t.ok and t.warning]
    if warned:
        rec.update(status="warn",
                   rpms=sorted({rpm_label(t.rpm) for t in warned}))
    return rec


# ---------------------------------------------------------------------------
# failure lists (plain NVR files under DIR/logs, feedable to --from-file)
# ---------------------------------------------------------------------------

def _sig_slug(signature: str) -> str:
    """Filename slug for a signature: sanitized lowercase alnum/hyphens
    (capped at 32 chars) plus 6 hex chars of its SHA-256, so distinct
    signatures never map to the same file."""
    base = re.sub(r"[^a-z0-9]+", "-", signature.lower()).strip("-")
    base = base[:32].rstrip("-") or "sig"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


def failure_rows(fetch: list | None, test: list | None) -> list:
    """One row per failure in the run: (stage, signature, nvr, rpm, exit).

    `rpm` is the failing RPM's basename for test failures and "" for
    fetch failures (a fetch has no single RPM).
    """
    rows = []
    for r in fetch or []:
        if not r.ok:
            rows.append(("fetch", r.signature, r.nvr, "", r.exit_code))
    for r in test or []:
        if not r.ok:
            rows.append((r.stage or "unknown", r.signature, r.nvr,
                         rpm_label(r.rpm), r.exit_code))
    return rows


def write_failure_lists(args, fetch: list | None, test: list | None) -> None:
    """Write the run's failure lists to DIR/logs (or remove stale ones).

    Called whenever the report is written. Each list is one build NVR
    per line — sorted, deduplicated, no header lines, each optionally
    followed by '  # detail' — so a list can be fed straight back to
    --from-file. Files:
      failed-all.txt           every build with a fetch or test failure
      failed-fetch.txt         the fetch failures
      failed-<stage>.txt       one per failing stage (e.g. failed-repack.txt)
      failed-sig-<slug>.txt    one per signature, top 20 by failure count
      failed-index.tsv         stage<TAB>signature<TAB>nvr<TAB>rpm per failure
                               (fetch rows leave the rpm column empty)
    A run without failures removes any lists left by an earlier run, so
    the files never describe a stale run.
    """
    logdir = Path(args.dir) / "logs"
    try:
        if logdir.is_dir():
            for p in logdir.glob("failed-*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
        rows = failure_rows(fetch, test)
        if not rows:
            return
        logdir.mkdir(parents=True, exist_ok=True)

        def detail(stage, exit_code, signature):
            d = f"stage {stage}, exit {exit_code}"
            if signature:
                d += f": {signature}"
            return d

        def nvr_text(nvr_map: dict) -> str:
            lines = [nvr if not nvr_map[nvr]
                     else f"{nvr}  # {nvr_map[nvr]}"
                     for nvr in sorted(nvr_map)]
            return "\n".join(lines) + "\n"

        all_map = {}
        for stage, sig, nvr, rpm, rc in rows:
            all_map.setdefault(nvr, detail(stage, rc, sig))
        (logdir / "failed-all.txt").write_text(nvr_text(all_map))

        by_stage = {}
        for stage, sig, nvr, rpm, rc in rows:
            by_stage.setdefault(stage, {}).setdefault(nvr, detail(stage, rc, sig))
        for stage in sorted(by_stage):
            (logdir / f"failed-{stage}.txt").write_text(nvr_text(by_stage[stage]))

        sig_count = {}
        by_sig = {}
        for stage, sig, nvr, rpm, rc in rows:
            if not sig:
                continue
            sig_count[sig] = sig_count.get(sig, 0) + 1
            by_sig.setdefault(sig, {}).setdefault(nvr, detail(stage, rc, sig))
        top = sorted(sig_count.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        for sig, _ in top:
            (logdir / f"failed-sig-{_sig_slug(sig)}.txt").write_text(
                nvr_text(by_sig[sig]))

        (logdir / "failed-index.tsv").write_text(
            "".join("\t".join((stage, sig, nvr, rpm)) + "\n"
                    for stage, sig, nvr, rpm, rc in
                    sorted(rows, key=lambda r: (r[0], r[2], r[3]))))
    except OSError as exc:
        log(f"{PROG}: warning: could not write failure lists to "
            f"{logdir}: {exc}", err=True)


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
                         "without downloading (incompatible with --test-only)")
    ap.add_argument("--from-file", metavar="FILE",
                    help="read the NVR list from FILE (same format as "
                         "'koji latest-pkg --all TAG'; blank lines and "
                         "'#' comments ignored) instead of the cached "
                         "list or Koji")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch the tag list from Koji, even if a "
                         "cached copy (DIR/tag-<TAG>.txt) exists")
    # test
    ap.add_argument("--no-test", dest="no_test", action="store_true",
                    help="fetch only; skip the tarpm test phase")
    ap.add_argument("--test-only", action="store_true",
                    help="skip fetching; test the RPMs already in DIR")
    ap.add_argument("--no-src", dest="no_src", action="store_true",
                    help="skip testing .src.rpm files and discard sources "
                         "after a successful fetch (tested by default)")
    ap.add_argument("--keep", action="store_true",
                    help="keep per-RPM work dirs, logs, and original RPMs "
                         "even for passing RPMs (default: passing RPMs and "
                         "work dirs removed; warning logs retained)")
    # common
    ap.add_argument("-d", "--dir", default="rpms",
                    help="download directory (default: ./rpms)")
    ap.add_argument("-r", "--report",
                    help="report file (default: ./report.txt)")
    ap.add_argument("--run-log", metavar="FILE",
                    help="append the run log (every log line, plus one "
                         "header line per invocation) to FILE (default: "
                         "DIR/run.log)")
    ap.add_argument("--no-run-log", action="store_true",
                    help="do not write a run log")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="number of parallel fetches/tests (default: 1)")
    ap.add_argument("--heartbeat", type=int, default=60,
                    help="while a command runs longer than this, log a "
                         "progress line every N seconds (0 disables; "
                         "default: 60)")
    ap.add_argument("--resume", action="store_true",
                    help="continue DIR/run.json, skipping that run's test "
                         "passes/warnings (legacy journal fallback if absent)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-run only the builds whose latest journal "
                         "record is fetch-fail or test-fail; each starts "
                         "from a clean DIR/<NVR>/. Default report: "
                         "report-retry-YYYYmmdd-HHMMSS.txt")
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
    if args.list and args.test_only:
        ap.error("--list is incompatible with --test-only")
    if args.refresh and args.from_file:
        ap.error("--refresh is incompatible with --from-file")
    if args.resume and args.retry_failed:
        ap.error("--resume and --retry-failed are mutually exclusive")
    if args.resume or args.retry_failed:
        if args.test_only:
            ap.error("--resume/--retry-failed cannot be combined with "
                     "--test-only")
        if args.refresh:
            ap.error("--resume/--retry-failed cannot be combined with "
                     "--refresh")
        if args.no_test:
            ap.error("--resume/--retry-failed cannot be combined with "
                     "--no-test")
    if args.report is None:
        args.report = (f"report-retry-"
                       f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.txt"
                       if args.retry_failed else "report.txt")
    if args.no_run_log:
        args.run_log = None
    elif args.run_log is None:
        args.run_log = str(Path(args.dir) / "run.log")
    return args


def main(argv=None) -> int:
    t0 = time.monotonic()
    args = parse_args(argv)
    if args.run_log:
        RUNLOG.open(args.run_log)
    # One header per invocation (a no-op when the run log is disabled);
    # argv identifies exactly which command produced the following lines.
    RUNLOG.write(f"=== {PROG} start {now_utc()}: {' '.join(sys.argv)} ===")
    do_fetch = not args.test_only
    # The tarpm test is the default; opt out with --no-test.
    do_test = args.test_only or (not args.no_test)
    if do_fetch:
        # One record per finished build; --test-only records nothing.
        JOURNAL.open(str(Path(args.dir) / "journal.jsonl"))

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
    manifest = None
    run_id = None
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

        journal_path = str(Path(args.dir) / "journal.jsonl")
        if args.retry_failed:
            # Select the builds whose latest journal record is a failure;
            # -p and -n above have already shaped the candidate list.
            latest = read_journal(journal_path)
            failed = {n for n, r in latest.items()
                      if r.get("status") in ("fetch-fail", "test-fail")}
            selected = [n for n in selected if n in failed]
            if not selected:
                log(f"{PROG}: nothing to do: no failed builds in "
                      f"{journal_path}", err=args.list)
                return 0

        if args.resume:
            # Only passes from this run can satisfy its selection. Older
            # successes must not hide unfinished or queued re-test attempts.
            try:
                manifest = read_run_manifest(Path(args.dir) / "run.json")
            except UsageError as exc:
                log(f"{PROG}: error: {exc}", err=True)
                return 2
            if manifest is not None:
                run_id = manifest["run_id"]
                run_builds = set(manifest["nvrs"])
                selected = [n for n in selected if n in run_builds]
            latest = read_journal(journal_path, run_id=run_id)
            before = len(selected)
            selected = [n for n in selected
                        if latest.get(n, {}).get("status")
                        not in ("pass", "warn")]
            log(f"{PROG}: resume: skipping {before - len(selected)} of "
                  f"{before} build(s) already passed (journal: "
                  f"{journal_path})", err=args.list)
            if not selected:
                log(f"{PROG}: nothing to do: no pending builds in this selection "
                      f"(journal: {journal_path})", err=args.list)
                return 0

        if args.list:
            for nvr in selected:
                print(nvr)
            log(f"{PROG}: {len(selected)} build(s) selected from tag "
                  f"{args.tag}", err=True)
            return 0

        if manifest is None:
            run_id = uuid.uuid4().hex
            try:
                write_run_manifest(Path(args.dir) / "run.json", selected, run_id)
            except OSError as exc:
                log(f"{PROG}: error: cannot save run manifest: {exc}", err=True)
                return 2

        (Path(args.dir) / "logs").mkdir(parents=True, exist_ok=True)
        total_selected = len(selected)

        def fail(nvr, exc):
            # A worker exception is journaled like a fetch failure so
            # --retry-failed can pick the build up again.
            fetch = FetchResult(nvr=nvr, ok=False, error=repr(exc),
                                signature="exception")
            JOURNAL.append(journal_record(nvr, fetch, [], run_id=run_id))
            return fetch

        if do_test:
            # Default: pipeline per build — fetch, test, clean up — with
            # -j builds in flight, so disk use stays bounded to the builds
            # in flight plus the failures.
            log(f"{PROG}: fetching and testing {len(selected)} build(s) "
                  f"from tag {args.tag} (parallelism: {args.jobs})")
            fetch_counter = Counter(len(selected))

            def work(nvr):
                # Both recovery modes need a fresh download. If cleanup
                # fails, fail this build rather than reuse untrusted RPMs.
                if args.retry_failed or args.resume:
                    bdir = build_dir(args.dir, nvr)
                    if bdir.exists():
                        shutil.rmtree(bdir)
                fetch, tests = process_build(nvr, args, fetch_counter)
                # Journal the build as it finishes, so a run that is
                # interrupted or crashes still records what completed.
                JOURNAL.append(journal_record(fetch.nvr, fetch, tests, run_id=run_id))
                return fetch, tests

            results, interrupted = run_pool(
                work, selected, args.jobs,
                on_error=lambda nvr, exc: (fail(nvr, exc), []))
            for fetch, tests in results:
                fetch_results.append(fetch)
                test_results.extend(tests)
        else:
            log(f"{PROG}: fetching {len(selected)} build(s) from tag "
                  f"{args.tag} (parallelism: {args.jobs})")
            fetch_counter = Counter(len(selected))

            def fetch_worker(nvr):
                result = fetch_one(nvr, args, fetch_counter)
                JOURNAL.append(journal_record(nvr, result, [], run_id=run_id))
                return result

            results, interrupted = run_pool(
                fetch_worker, selected, args.jobs, on_error=fail)
            for r in results:
                fetch_results.append(r)
    elif do_test:
        # --test-only: test the RPMs already under DIR.
        (Path(args.dir) / "logs").mkdir(parents=True, exist_ok=True)
        try:
            units = collect_test_units(args)
        except UsageError as exc:
            log(f"{PROG}: error: {exc}", err=True)
            return 2
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
                if (d.is_dir() and not d.is_symlink() and d.name != "logs"
                        and not any(d.iterdir())):
                    d.rmdir()

    fetch_for_report = fetch_results if do_fetch else None
    test_for_report = test_results if do_test else None
    report = write_report(args, fetch_for_report, test_for_report,
                          time.monotonic() - t0, tag_source,
                          interrupted=interrupted,
                          total_selected=total_selected)
    write_failure_lists(args, fetch_for_report, test_for_report)
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
        top = failure_types(fetch_for_report, test_for_report)[:5]
        if top:
            log(f"{PROG}: failure types (top {len(top)}):")
            for stage, rc, sig, count, _ in top:
                log(f"{PROG}:   {count}x stage {stage}, exit {rc}: {sig}")
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
    except Exception:
        # Uncaught: record the traceback in the run log, then re-raise.
        for line in traceback.format_exc().splitlines():
            RUNLOG.write(line)
        raise
