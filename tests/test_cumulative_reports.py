"""Resumed reports retain run outcomes and per-version attempt history."""
import json
import os
import subprocess
import sys
from unittest.mock import patch

from test_tarpm_tag_runner import RunnerTestCase, _ROOT, mod


class CumulativeReportTests(RunnerTestCase):
    def setUp(self):
        super().setUp()
        self.nvrs = ['aaa-1-1', 'bbb-1-1', 'ccc-1-1']
        self.listfile.write_text('\n'.join(self.nvrs) + '\n')
        self.json_path = self.root / 'results.json'
        self.argv += ['--json', str(self.json_path)]

    def package(self, version):
        return {'executable': '/fake/tarpm', 'package': {
            'name': 'tarpm', 'version': version, 'release': '1.fc44',
            'arch': 'x86_64'}, 'rpm_info': f'Version : {version}', 'error': None}

    def build(self, nvr, outcomes=(True,), warning=False, seconds=1):
        if outcomes is None:
            return mod.FetchResult(nvr, False, 1, signature='download failed',
                                   duration_seconds=seconds), []
        return mod.FetchResult(nvr, True, duration_seconds=seconds), [
            mod.TestResult(str(self.downloads / nvr / f'{nvr}-{i}.noarch.rpm'),
                           nvr, 'noarch', ok, stage=None if ok else 'repack',
                           exit_code=None if ok else 1,
                           warning='content-diff' if warning and i == 0 and ok else None,
                           signature='' if ok else 'bad payload',
                           duration_seconds=seconds, stage_seconds={'unpack': seconds})
            for i, ok in enumerate(outcomes)]

    def attempt(self, builds, *options, version='1'):
        with patch.object(mod, 'tarpm_package_info', return_value=self.package(version)), \
                patch.object(mod, 'process_build', side_effect=lambda nvr, args, counter:
                             builds[nvr]) as process:
            status = self.run_main(*options)
        return status, [call.args[0] for call in process.call_args_list]

    def initial_failures(self):
        status, called = self.attempt({
            self.nvrs[0]: self.build(self.nvrs[0], (True, True), warning=True),
            self.nvrs[1]: self.build(self.nvrs[1], (True, False)),
            self.nvrs[2]: self.build(self.nvrs[2], None)})
        self.assertEqual((status, set(called)), (1, set(self.nvrs)))
        return self.data()

    def data(self):
        return json.loads(self.json_path.read_text())

    def test_resume_replaces_failed_builds_and_retains_passes_and_version_history(self):
        original = self.initial_failures()
        status, called = self.attempt({
            self.nvrs[1]: self.build(self.nvrs[1], (True, True), seconds=2),
            self.nvrs[2]: self.build(self.nvrs[2], seconds=3)}, '--resume', version='2')
        self.assertEqual((status, set(called)), (0, set(self.nvrs[1:])))
        data = self.data()
        self.assertEqual(data['summary']['fetch'], {'total': 3, 'ok': 3, 'failed': 0})
        self.assertEqual(data['summary']['test'], {
            'total': 5, 'ok': 5, 'failed': 0, 'warnings': 1, 'clean_passed': 4})
        self.assertEqual(data['session']['summary']['test']['total'], 3)
        self.assertEqual(data['session']['selected_builds'], 2)
        self.assertEqual(data['run']['id'], original['run']['id'])
        self.assertEqual(data['run']['started'], original['session']['started'])
        self.assertEqual(data['run']['command'], original['session']['command'])
        self.assertEqual(data['run']['build_statuses'], {'warn': 1, 'pass': 2})
        self.assertEqual(data['run']['pending_builds'], 0)
        self.assertEqual(data['run']['status'], 'passed')
        self.assertEqual(data['run']['summary_only_builds'], [])
        self.assertEqual(data['timings']['unpack']['total_seconds'], 9)
        self.assertEqual(data['timings']['unpack']['count'], 5)
        self.assertEqual(data['failure_types'], {'fetch': [], 'test': []})
        self.assertFalse((self.downloads / 'logs/failed-all.txt').exists())
        attempts = data['run']['attempts']
        self.assertEqual([a['build_count'] for a in attempts], [3, 2])
        self.assertEqual([a['selected_builds'] for a in attempts], [3, 2])
        self.assertEqual(attempts[0]['fetch_failed'], [self.nvrs[2]])
        self.assertEqual(attempts[0]['test_failed'], [self.nvrs[1]])
        self.assertEqual(attempts[1]['fetch_failed'], [])
        self.assertEqual(attempts[1]['test_failed'], [])
        self.assertEqual([a['tarpm'] for a in attempts],
                         ['tarpm-1-1.fc44.x86_64', 'tarpm-2-1.fc44.x86_64'])
        records = list(mod.journal_records(str(self.downloads / 'journal.jsonl')))
        self.assertEqual(len(records), 5)
        self.assertEqual([r['status'] for r in records if r['nvr'] == self.nvrs[1]],
                         ['test-fail', 'pass'])
        self.assertTrue(all('results' in r and 'attempt' in r for r in records))
        report = (self.root / 'report.txt').read_text()
        self.assertIn('selected: 3 build(s)', report)
        self.assertIn('rpms:     5', report)
        self.assertIn('saved test versions: tarpm-1-', report)
        self.assertIn('removed:  3 passed RPM(s) (this invocation;', report)
        self.assertNotIn('=== TEST FAILURES ===', report)
        addendum = report.split('=== RUN ATTEMPTS ===')[1]
        self.assertIn('test failures: bbb-1-1', addendum)
        self.assertIn('fetch failures: ccc-1-1', addendum)
        self.assertIn('tarpm-2-1.fc44.x86_64', addendum)

    def test_filtered_resume_keeps_other_failures_and_failure_lists(self):
        self.initial_failures()
        status, called = self.attempt({self.nvrs[1]: self.build(self.nvrs[1])},
                                     '--resume', '-p', 'bbb', version='2')
        self.assertEqual((status, called), (0, [self.nvrs[1]]))
        data = self.data()
        self.assertEqual(data['run']['status'], 'failed')
        self.assertEqual(data['run']['pending_builds'], 1)
        self.assertEqual(data['session']['status'], 'passed')
        self.assertEqual(data['summary']['fetch']['failed'], 1)
        self.assertEqual(data['summary']['test']['total'], 3)
        self.assertIn('ccc-1-1', (self.downloads / 'logs/failed-all.txt').read_text())
        self.assertNotIn('bbb-1-1', (self.downloads / 'logs/failed-all.txt').read_text())

    def test_no_pending_resume_rebuilds_reports_without_tools_or_journal_changes(self):
        self.attempt({nvr: self.build(nvr) for nvr in self.nvrs})
        manifest = self.downloads / 'run.json'
        journal = self.downloads / 'journal.jsonl'
        before = manifest.read_bytes(), journal.read_bytes()
        original_attempts = self.data()['run']['attempts']
        self.json_path.unlink()
        (self.root / 'report.txt').unlink()
        with patch.object(mod.shutil, 'which', return_value=None), \
                patch.object(mod, 'process_build') as process, \
                patch.object(mod, 'tarpm_package_info') as package, \
                patch.object(mod.JOURNAL, 'open') as open_journal:
            self.assertEqual(self.run_main('--resume'), 0)
        process.assert_not_called()
        package.assert_not_called()
        open_journal.assert_not_called()
        self.assertEqual((manifest.read_bytes(), journal.read_bytes()), before)
        self.assertEqual(self.data()['summary']['test']['total'], 3)
        self.assertEqual(self.data()['session']['summary']['test']['total'], 0)
        self.assertEqual(self.data()['run']['attempts'], original_attempts)
        self.assertIn('no tests this invocation', (self.root / 'report.txt').read_text())

    def test_fresh_run_resets_totals_but_keeps_prior_attempts_in_addendum(self):
        first = self.initial_failures()
        self.attempt({nvr: self.build(nvr) for nvr in self.nvrs}, version='3')
        data = self.data()
        self.assertNotEqual(data['run']['id'], first['run']['id'])
        self.assertEqual(data['summary']['test']['total'], 3)
        self.assertEqual(data['run']['tarpm_versions'], ['tarpm-3-1.fc44.x86_64'])
        self.assertEqual(len(data['run']['attempts']), 2)
        self.assertEqual(data['run']['attempts'][0]['run_id'], first['run']['id'])
        self.assertEqual(data['run']['attempts'][0]['test_failed'], [self.nvrs[1]])

    def test_cli_rebuilds_completed_journal_without_external_tools(self):
        self.attempt({nvr: self.build(nvr) for nvr in self.nvrs})
        empty_path = self.root / 'empty-path'
        empty_path.mkdir()
        result = subprocess.run(
            [sys.executable, str(_ROOT / 'tarpm-tag-runner.py'), *self.argv, '--resume'],
            env={**os.environ, 'PATH': str(empty_path)}, capture_output=True, text=True,
            timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no pending builds', result.stdout)
        self.assertEqual(self.data()['summary']['test']['total'], 3)
        self.assertEqual(self.data()['session']['selected_builds'], 0)

    def test_retry_failed_starts_separate_run_and_retains_original_attempt_history(self):
        first = self.initial_failures()
        status, called = self.attempt({nvr: self.build(nvr) for nvr in self.nvrs[1:]},
                                     '--retry-failed', version='2')
        self.assertEqual((status, set(called)), (0, set(self.nvrs[1:])))
        data = self.data()
        self.assertNotEqual(data['run']['id'], first['run']['id'])
        self.assertEqual(data['run']['selected_builds'], 2)
        self.assertEqual(data['summary']['test']['total'], 2)
        self.assertEqual(len(data['run']['attempts']), 2)
        self.assertEqual(data['run']['attempts'][0]['test_failed'], [self.nvrs[1]])

    def test_interrupted_run_preserves_completed_results_for_next_report(self):
        def interrupted_pool(worker, items, jobs, on_error=None):
            return [worker(items[0])], True

        with patch.object(mod, 'run_pool', side_effect=interrupted_pool):
            status, called = self.attempt({self.nvrs[0]: self.build(self.nvrs[0])})
        self.assertEqual((status, called), (130, [self.nvrs[0]]))
        first = self.data()
        self.assertEqual(first['run']['build_statuses'], {'pass': 1, 'unattempted': 2})
        self.assertEqual(first['run']['status'], 'incomplete')
        self.assertEqual(first['summary']['test']['total'], 1)
        self.assertIn('completed: 1 build(s)', (self.root / 'report.txt').read_text())
        self.attempt({nvr: self.build(nvr) for nvr in self.nvrs[1:]}, '--resume')
        data = self.data()
        self.assertEqual(data['summary']['test']['total'], 3)
        self.assertEqual(data['run']['attempts'][0]['build_count'], 1)
        self.assertEqual(data['run']['attempts'][0]['selected_builds'], 3)

    def test_crash_before_report_does_not_lose_detailed_journal_results(self):
        def crash(worker, items, jobs, on_error=None):
            worker(items[0])
            raise KeyboardInterrupt

        with patch.object(mod, 'run_pool', side_effect=crash), \
                self.assertRaises(KeyboardInterrupt):
            self.attempt({self.nvrs[0]: self.build(self.nvrs[0], (True, True))})
        self.assertFalse(self.json_path.exists())
        self.attempt({nvr: self.build(nvr) for nvr in self.nvrs[1:]}, '--resume')
        self.assertEqual(self.data()['summary']['test']['total'], 4)

    def legacy_journal(self, manifest=True):
        self.downloads.mkdir()
        records = [
            {'nvr': self.nvrs[0], 'status': 'pass', 'exit_code': None},
            {'nvr': self.nvrs[1], 'status': 'test-fail', 'stage': 'repack',
             'exit_code': 1, 'signature': 'old payload error', 'rpms': ['one', 'two']},
            {'nvr': self.nvrs[2], 'status': 'fetch-fail', 'stage': 'fetch',
             'exit_code': 1, 'signature': 'old network error'}]
        if manifest:
            mod.write_run_manifest(self.downloads / 'run.json', self.nvrs, 'old-run')
            for rec in records:
                rec['run_id'] = 'old-run'
        (self.downloads / 'journal.jsonl').write_text(
            ''.join(json.dumps(rec) + '\n' for rec in records))

    def test_legacy_records_keep_build_counts_without_inventing_rpm_totals(self):
        self.legacy_journal()
        status, called = self.attempt({self.nvrs[2]: self.build(self.nvrs[2])},
                                     '--resume', '-p', 'ccc', version='2')
        self.assertEqual((status, called), (0, [self.nvrs[2]]))
        data = self.data()
        self.assertEqual(data['run']['summary_only_builds'], self.nvrs[:2])
        self.assertEqual(data['summary']['fetch'], {'total': 3, 'ok': 3, 'failed': 0})
        self.assertEqual(data['summary']['test']['total'], 1)
        self.assertEqual(data['run']['build_statuses'], {'pass': 2, 'test-fail': 1})
        self.assertEqual(data['run']['status'], 'failed')
        report = (self.root / 'report.txt').read_text()
        self.assertIn('2 build(s) have summary-only journal records', report)
        self.assertIn('older RPM results cannot be reconstructed', report)
        self.assertIn('bbb-1-1: first failure stage repack', report)
        self.assertIn('bbb-1-1', (self.downloads / 'logs/failed-all.txt').read_text())
        self.assertIn('old payload error', (self.downloads / 'logs/failed-index.tsv').read_text())

    def test_legacy_journal_without_manifest_adopts_skipped_successes_once(self):
        self.legacy_journal(manifest=False)
        self.attempt({nvr: self.build(nvr) for nvr in self.nvrs[1:]}, '--resume')
        data = self.data()
        self.assertEqual(data['run']['selected_builds'], 3)
        self.assertEqual(data['run']['summary_only_builds'], self.nvrs[:1])
        latest = mod.read_journal(str(self.downloads / 'journal.jsonl'), data['run']['id'])
        self.assertEqual(set(latest), set(self.nvrs))
        before = (self.downloads / 'journal.jsonl').read_bytes()
        self.assertEqual(self.attempt({}, '--resume')[1], [])
        self.assertEqual((self.downloads / 'journal.jsonl').read_bytes(), before)
        self.assertEqual(self.data()['run']['summary_only_builds'], self.nvrs[:1])

    def test_legacy_upgrade_preserves_old_reports_once_before_overwriting(self):
        self.legacy_journal()
        report_path = self.root / 'report.txt'
        report_path.write_text('Original report: 139437 RPMs\n')
        self.json_path.write_text('{"summary": {"test": {"total": 139437}}}\n')
        original_report = report_path.read_bytes()
        original_json = self.json_path.read_bytes()
        self.attempt({self.nvrs[2]: self.build(self.nvrs[2])}, '--resume', '-p', 'ccc')
        text_backups = list(self.root.glob('report.txt.before-resume-*.bak'))
        json_backups = list(self.root.glob('results.json.before-resume-*.bak'))
        self.assertEqual(len(text_backups), 1)
        self.assertEqual(len(json_backups), 1)
        self.assertEqual(text_backups[0].read_bytes(), original_report)
        self.assertEqual(json_backups[0].read_bytes(), original_json)
        self.assertIn(str(text_backups[0]), report_path.read_text())
        self.attempt({}, '--resume', '-p', 'ccc')
        self.assertEqual(list(self.root.glob('report.txt.before-resume-*.bak')), text_backups)
        self.assertEqual(list(self.root.glob('results.json.before-resume-*.bak')), json_backups)

    def test_incompatible_resume_settings_fail_before_touching_results(self):
        self.initial_failures()
        before = (self.downloads / 'journal.jsonl').read_bytes(), self.json_path.read_bytes()
        for options in (('--no-src',), ('-a', 'x86_64'), ('-t', 'f43')):
            with self.subTest(options=options):
                status, called = self.attempt({}, '--resume', *options)
                self.assertEqual((status, called), (2, []))
                self.assertEqual(((self.downloads / 'journal.jsonl').read_bytes(),
                                  self.json_path.read_bytes()), before)

    def test_failed_legacy_backup_does_not_overwrite_original_report(self):
        self.legacy_journal()
        report_path = self.root / 'report.txt'
        report_path.write_text('Original report: 139437 RPMs\n')
        with patch.object(mod.tempfile, 'NamedTemporaryFile', side_effect=PermissionError), \
                self.assertRaises(PermissionError):
            self.attempt({self.nvrs[2]: self.build(self.nvrs[2])}, '--resume', '-p', 'ccc')
        self.assertEqual(report_path.read_text(), 'Original report: 139437 RPMs\n')

    def test_corrupt_details_never_restore_a_superseded_failure(self):
        self.initial_failures()
        record = mod.journal_record(self.nvrs[1], *self.build(self.nvrs[1]),
                                    run_id=self.data()['run']['id'])
        record['results']['test'][0]['stage_seconds'] = {'unpack': 'bad'}
        mod.JOURNAL.append(record)
        # No work for this passed build; only summary-level knowledge is retained.
        self.attempt({}, '--resume', '-p', 'bbb')
        data = self.data()
        self.assertEqual(data['run']['summary_only_builds'], [self.nvrs[1]])
        self.assertEqual(data['run']['build_statuses'], {'warn': 1, 'pass': 1, 'fetch-fail': 1})
        self.assertEqual(data['summary']['test']['failed'], 0)
        self.assertNotIn('bbb-1-1', (self.downloads / 'logs/failed-all.txt').read_text())

    def test_report_includes_current_results_if_journal_persistence_fails(self):
        self.initial_failures()
        with patch.object(mod.os, 'write', side_effect=OSError('disk full')):
            self.attempt({nvr: self.build(nvr) for nvr in self.nvrs[1:]}, '--resume')
        self.assertEqual(self.data()['summary']['fetch']['failed'], 0)
        self.assertEqual(self.data()['run']['status'], 'passed')
        # The stored journal still correctly requires another retry.
        latest = mod.read_journal(str(self.downloads / 'journal.jsonl'), self.data()['run']['id'])
        self.assertEqual(latest[self.nvrs[1]]['status'], 'test-fail')

    def test_comparison_does_not_claim_saved_passes_were_rechecked(self):
        self.initial_failures()
        baseline = self.root / 'baseline.json'
        baseline.write_bytes(self.json_path.read_bytes())
        self.attempt({nvr: self.build(nvr, (True, True)) for nvr in self.nvrs[1:]},
                     '--resume', '--compare', str(baseline))
        data = self.data()
        self.assertEqual(data['summary']['test']['total'], 6)
        self.assertEqual(data['comparison']['test']['rechecked'], 2)
        self.assertEqual(data['comparison']['test']['not_rechecked'], 2)
        self.assertEqual(len(data['comparison']['test']['resolved']), 1)
        self.assertEqual(data['comparison']['fetch']['rechecked'], 2)
        self.assertEqual(data['comparison']['fetch']['not_rechecked'], 1)
