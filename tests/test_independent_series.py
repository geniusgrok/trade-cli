import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import private_store as store
import public_report
import publish_report as publisher
import runtime_profile as profile
from test_publication import fixture


def full_bundle(change=None):
    close = '2026-09-30'
    state = {'schema_version': 2, 'scan_date': close, 'run_id': 'native-seed',
             'checkpoint': {'state': {'last_completed_close': close}}}
    artifact = {'scan_date': close, 'run_id': 'native-seed', 'risk_state_saved': True}
    files = {'output/risk_state.json': json.dumps(state).encode(),
             f'output/signals_{close}.json': json.dumps(artifact).encode(),
             f'output/snapshots/{close}/manifest.json': b'{}',
             f'output/snapshots/{close}/market_data/x.csv': b'date,close\n2026-09-30,1\n',
             'cache/x.csv': b'cache', 'output/daily_report.md': b'old public view'}
    files['output/latest_success.json'] = json.dumps({'file': f'signals_{close}.json',
        'scan_date': close, 'run_id': 'native-seed',
        'artifact_sha256': store.digest(files[f'output/signals_{close}.json']),
        'state_sha256': store.digest(files['output/risk_state.json'])}).encode()
    if change:
        change(files)
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as archive:
        for path, data in files.items():
            item = tarfile.TarInfo(path); item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return out.getvalue(), files


class IndependentSeriesTests(unittest.TestCase):
    def test_local_input_paths_use_step_available_runner_context(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/trade-inherited.yml').read_text()
        job_env = workflow.split('    env:\n', 1)[1].split('    steps:\n', 1)[0]
        self.assertNotIn('${{ runner.', job_env)
        execution = workflow.split('      - name: 原生续跑并核验完整原件保存\n', 1)[1].split('      - name:', 1)[0]
        self.assertIn('        env:\n', execution)
        for kind in ('MARKET', 'REGIME'):
            self.assertIn('TRADE_LOCAL_' + kind + '_DIR: ${{ runner.temp }}/trade-inherited-evidence/', execution)

    def test_independent_workflow_is_manual_main_only_and_uses_existing_credentials(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/trade-inherited.yml').read_text()
        self.assertIn("github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch'", workflow)
        self.assertNotIn('  schedule:', workflow)
        self.assertNotIn('  push:', workflow)
        self.assertIn('group: trade-a92-inherited-v1', workflow)
        self.assertIn('STATE_SEAL_KEY: ${{ secrets.TRADE_STATE_KEY }}', workflow)
        self.assertIn('TRADE_SIMULATION_IDENTITY: a92-inherited-v1', workflow)
        self.assertIn("TRADE_SIMULATION_IDENTITY: ''", workflow)
        self.assertLess(workflow.index('run: python bootstrap.py'), workflow.index('run: python seed_inherited.py'))
        self.assertLess(workflow.index('run: python seed_inherited.py'), workflow.index('run: python preflight.py'))
        self.assertIn('run: python runner.py', workflow)
        self.assertIn('run: python publish_report.py', workflow)

    def test_unknown_identity_never_falls_back_to_legacy(self):
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': 'typo'}):
            for operation in (profile.state_branch, lambda: profile.report_path('2026-10-08'),
                              lambda: profile.require_record(None)):
                with self.assertRaisesRegex(ValueError, 'UNKNOWN_SIMULATION_IDENTITY'):
                    operation()

    def test_paths_and_authenticated_envelopes_are_isolated(self):
        env = {'STATE_SEAL_KEY': 'fixture-key-' * 4, 'TRADE_SIMULATION_IDENTITY': ''}
        path = 'runs/2026-09-30.json.enc'
        with patch.dict(os.environ, env):
            sealed_legacy = store._seal({'status': 'SUCCESS'}, path)
            self.assertEqual(profile.state_branch(), 'runtime-state')
            self.assertEqual(profile.report_path('2026-09-30'), 'reports/2026-09-30.md')
            self.assertEqual(store._aad(path), path.encode())
        with patch.dict(os.environ, {**env, 'TRADE_SIMULATION_IDENTITY': profile.INHERITED}):
            self.assertEqual(profile.state_branch(), 'runtime-state-a92-inherited-v1')
            self.assertEqual(profile.report_path('2026-09-30'), 'reports/a92-inherited-v1/2026-09-30.md')
            with self.assertRaisesRegex(ValueError, 'STATE_DECRYPT_FAILED'):
                store._open(sealed_legacy, path)
            sealed_new = store._seal({'status': 'SUCCESS'}, path)
            self.assertEqual(store._open(sealed_new, path), {'status': 'SUCCESS'})
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(ValueError, 'STATE_DECRYPT_FAILED'):
                store._open(sealed_new, path)

    def test_store_rejects_cross_series_records_and_wrong_producer(self):
        good = {'simulation_identity': profile.INHERITED, 'strategy_sha': profile.SOURCE_SHA,
                'state_origin': profile.STATE_ORIGIN,
                'report': {'simulation_identity': profile.INHERITED, 'strategy_sha': profile.SOURCE_SHA,
                           'state_origin': profile.STATE_ORIGIN}}
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}):
            profile.require_record(good)
            for change in ({'simulation_identity': ''}, {'strategy_sha': 'b' * 40},
                           {'report': {'simulation_identity': ''}}):
                with self.assertRaisesRegex(ValueError, 'INDEPENDENT_'):
                    profile.require_record({**good, **change})
            with self.assertRaisesRegex(ValueError, 'INDEPENDENT_SOURCE_IDENTITY'):
                profile.require_source('b' * 40)

    def test_complete_restore_keeps_native_publication_and_snapshot_bytes(self):
        data, files = full_bundle()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store.restore(data, root, complete=True)
            for path, raw in files.items():
                if path != 'output/daily_report.md':
                    self.assertEqual((root / path).read_bytes(), raw)
            self.assertFalse((root / 'output/daily_report.md').exists())

    def test_unknown_previous_terminal_status_never_starts_a_new_close(self):
        unknown = {'status': 'UNKNOWN', 'simulation_identity': profile.INHERITED,
                   'strategy_sha': profile.SOURCE_SHA, 'state_origin': profile.STATE_ORIGIN}
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
             patch('private_store._snapshot', return_value=('head', 'tree', {'runs/2026-09-30.json.enc': 'blob'})), \
             patch('private_store._read', side_effect=[None, unknown]), \
             patch('private_store._commit') as commit:
            with self.assertRaisesRegex(ValueError, 'PREVIOUS_RUN_UNRESOLVED'):
                store.request('start', {'date': '2026-10-08', 'strategy_sha': profile.SOURCE_SHA,
                                        'previous_date': '2026-09-30'})
        commit.assert_not_called()

    def test_incomplete_or_tampered_publication_cannot_restore_partial_state(self):
        cases = [lambda files: files.pop('output/latest_success.json'),
                 lambda files: files.pop('output/signals_2026-09-30.json'),
                 lambda files: files.pop('output/snapshots/2026-09-30/manifest.json'),
                 lambda files: files.update({'output/risk_state.json': b'{"schema_version":1,"scan_date":"2026-09-30"}'}),
                 lambda files: files.update({'output/signals_2026-09-30.json': b'{"scan_date":"2026-09-30","run_id":"other","risk_state_saved":true}'})]
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                data, _ = full_bundle(change)
                with self.assertRaises(ValueError):
                    store.restore(data, Path(directory), complete=True)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_publication_only_creates_the_independent_report_path(self):
        value = {**fixture(), 'simulation_identity': profile.INHERITED,
                 'state_origin': profile.STATE_ORIGIN,
                 'continuation': {'mode': 'native_full_checkpoint_resume', 'previous_close': '2026-09-21'}}
        text = public_report.markdown(value)
        self.assertIn('a92-inherited-v1', text)
        self.assertNotIn('生产固定起点模拟', text)
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
             patch('publish_report.read_file', return_value=None), \
             patch('publish_report.api', return_value={'content': {'sha': publisher.blob_id(text.encode())},
                                                      'commit': {'sha': 'c' * 40}}) as api, \
             patch('publish_report.verify_file', return_value={}) as verify:
            publisher.publish('2026-09-22', text)
        self.assertEqual(api.call_args.args[1], '/contents/reports/a92-inherited-v1/2026-09-22.md')
        self.assertEqual(verify.call_args.args[0], 'reports/a92-inherited-v1/2026-09-22.md')

    def test_prefetched_source_requires_exact_pin_without_network_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'; source.mkdir()
            (source / 'requirements-lock.txt').write_text('')
            env = {'RUNNER_TEMP': directory, 'TRADE_SIMULATION_IDENTITY': profile.INHERITED,
                   'TRADE_PREFETCHED_SOURCE': str(source)}
            with patch.dict(os.environ, env, clear=True), \
                 patch('bootstrap.sys.version_info', (3, 12, 14)), \
                 patch('bootstrap.subprocess.check_output', side_effect=[profile.SOURCE_SHA + '\n', '']), \
                 patch('bootstrap.subprocess.run') as run, patch('builtins.print'):
                self.assertEqual(bootstrap.main(), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][-2:], ['-r', str(source / 'requirements-lock.txt')])


if __name__ == '__main__':
    unittest.main()
