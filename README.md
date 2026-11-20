# tarpm-tag-runner

Fetch RPMs from a Fedora Koji tag and test them with tarpm. The runner
unpacks each RPM, repacks it, unpacks the result, and compares the two
file listings. Listing differences are recorded as warnings.

## Setup

You need Python 3.9 or later, a Koji client configured for Fedora, and
tarpm. On Fedora with DNF5, enable the tarpm COPR before installing tarpm.

```bash
sudo dnf install python3 koji dnf5-plugins
sudo dnf copr enable dcantrell/tarpm
sudo dnf install tarpm
```

Run the examples from the directory containing
[tarpm-tag-runner.py](tarpm-tag-runner.py).

## Select and test builds

```bash
# Preview the hello builds without downloading RPMs.
python3 tarpm-tag-runner.py -t f45 -p hello --list

# Test hello for x86_64 and save a JSON report.
python3 tarpm-tag-runner.py -t f45 -p hello -a x86_64 --json results.json

# Preview the ten smallest builds for x86_64.
python3 tarpm-tag-runner.py -t f45 -a x86_64 --sort asc -n 10 --list
```

By default, the runner tests the latest RPM builds from `f45` with one
worker, including all binary architectures and source RPMs. Repeat `-p`
to select several source packages and their binary subpackages. A package
filter matches a build's name-version-release (NVR) exactly or as a prefix
followed by a hyphen. Use `-a ARCH` to select an architecture plus `noarch`
and source RPMs. Repeat it for multiple architectures and use `--no-src`
to skip source tests.

`--sort asc` or `--sort desc` orders builds by the compressed size of
their selected RPMs before `-n` limits the selection. The runner reuses
the cached tag list until you pass `--refresh`. Run
`python3 tarpm-tag-runner.py --help` for all options.

## Run a tag and resume

```bash
python3 tarpm-tag-runner.py -t f45 -j 2
python3 tarpm-tag-runner.py -t f45 -j 2 --resume
```

`-j` controls how many builds run in parallel. Each build gets a temporary
workspace that is cleaned up when it finishes. A 1.5 GiB RPM can need
over 10 GiB of working space during a round-trip.

The first Ctrl+C lets active builds finish and saves a partial report.
A second Ctrl+C can leave temporary workspaces behind. Resume with the
same state directory, tag, architecture, and source settings to retest
failed or unfinished builds. `--retry-failed` starts a new run for
selected builds whose latest recorded attempts failed. Use `--refresh`
in a separate invocation from either recovery mode.

## Read and compare results

The default text report is `./report.txt`. Retry runs use a timestamped
`report-retry-*.txt` by default. Use `-r FILE` to set the text report path
and `--json FILE` to also save machine-readable results. Progress goes to
`rpms/run.log` by default. Diagnostic logs and failure lists are in
`rpms/logs/`. Use `-d DIR` to change the state directory and give
simultaneous runs separate state directories and report paths.

After updating tarpm, repeat the same selection with `--compare` to
check outcomes against a saved JSON report. Comparisons cover work
performed in the current invocation.

```bash
python3 tarpm-tag-runner.py -t f45 -p hello -a x86_64 \
    --compare results.json --json current.json
```

Exit codes are `0` for success (including warnings), `1` for a fetch or
test failure, `2` for a setup or usage error, and `130` for Ctrl+C.

## Update the runner

Run `python3 tarpm-tag-runner.py --update` on its own to replace the
script with the latest copy from this repository's `main` branch.
The update needs network access and a writable script directory, and
overwrites local edits. The next invocation uses the new copy.

[Development and maintenance notes](AGENTS.md)
