"""CLI boundary tests with synthetic read-only source helpers, never a native replay."""
import base64
from contextlib import ExitStack
from datetime import date
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import private_store as store
import publish_report as publisher
import runner
import runtime_profile as profile
from test_independent_series import full_bundle
from test_publication import fixture


def modules_with(definitions):
    modules = {}
    for name, attributes in definitions.items():
        parts = name.split('.')
        for size in range(1, len(parts) + 1):
            key = '.'.join(parts[:size])
            if key not in modules:
                modules[key] = types.ModuleType(key)
                modules[key].__path__ = []
            if size > 1:
                setattr(modules['.'.join(parts[:size - 1])], parts[size - 1], modules[key])
        for key, value in attributes.items():
            setattr(modules[name], key, value)
    return modules


def independent_record(status='DEGRADED', risk=None):
    return {'status': status, 'simulation_identity': profile.INHERITED,
            'strategy_sha': profile.SOURCE_SHA, 'state_origin': profile.STATE_ORIGIN.copy(),
            'risk_state': risk}


class ArchivedAndContextTests(unittest.TestCase):
    def test_blank_runner_and_archived_writers_reject_before_any_api(self):
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': ''}), \
             patch.object(store, 'api') as api, patch.object(store, '_snapshot') as snapshot, \
             patch.object(store, '_seal') as seal, patch.object(runner, 'identity') as identity:
            with self.assertRaisesRegex(ValueError, 'ARCHIVED_SERIES_READ_ONLY'):
                runner.run()
            for route in ('start', 'finish', 'event'):
                with self.subTest(route=route), self.assertRaisesRegex(ValueError, 'ARCHIVED_SERIES_READ_ONLY'):
                    store.request(route, {})
            with self.assertRaisesRegex(ValueError, 'ARCHIVED_SERIES_READ_ONLY'):
                store._commit('runs/2026-09-30.json.enc', {}, 'head', 'tree', {})
        for operation in (api, snapshot, seal, identity):
            operation.assert_not_called()

    def test_archived_result_and_context_remain_readable(self):
        older = {'status': 'DEGRADED', 'run_id': 'old', 'strategy_sha': 'a' * 40,
                 'risk_state': {'schema_version': 1},
                 'report': {'strategy_sha': 'a' * 40}, 'bundle': 'fixture', 'sha256': 'b' * 64}
        old_path = 'runs/2026-09-30.json.enc'
        def read(path, files):
            return older if path == old_path else None
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': ''}), \
             patch.object(store, '_snapshot', return_value=('head', 'tree', {old_path: 'oid'})), \
             patch.object(store, '_read', side_effect=read), patch.object(store, '_commit') as commit:
            self.assertEqual(store.request('result', {'date': '2026-09-30'}), older)
            context = store.request('context', {'date': '2026-10-08'})
        self.assertEqual(context['previous']['date'], '2026-09-30')
        self.assertEqual(context['previous']['risk_state'], older['risk_state'])
        commit.assert_not_called()

    def test_latest_unresolved_or_partial_previous_never_falls_back_to_good(self):
        target = 'runs/2026-10-08.json.enc'
        latest = 'runs/2026-09-30.json.enc'
        older = 'runs/2026-09-29.json.enc'
        good = independent_record(risk={'schema_version': 2})
        cases = [(independent_record(status), 'PREVIOUS_RUN_UNRESOLVED')
                 for status in ('RUNNING', 'FAILED', 'UNKNOWN')]
        cases += [(independent_record(), 'COMPLETE_INHERITED_STATE_REQUIRED'),
                  (independent_record(risk={'schema_version': 1}), 'COMPLETE_INHERITED_STATE_REQUIRED')]
        for broken, reason in cases:
            def read(path, files):
                return {target: None, latest: broken, older: good}[path]
            with self.subTest(status=broken['status'], risk=broken['risk_state']), \
                 patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
                 patch.object(store, '_snapshot', return_value=('head', 'tree', {latest: 'latest', older: 'older'})), \
                 patch.object(store, '_read', side_effect=read) as read_call, \
                 patch.object(store, '_commit') as commit:
                with self.assertRaisesRegex(ValueError, reason):
                    store.request('context', {'date': '2026-10-08'})
                self.assertEqual([call.args[0] for call in read_call.call_args_list], [target, latest])
                commit.assert_not_called()

    def test_same_day_unresolved_is_returned_without_selecting_any_previous(self):
        path = 'runs/2026-10-08.json.enc'
        for status in ('RUNNING', 'FAILED', 'UNKNOWN'):
            value = independent_record(status)
            with self.subTest(status=status), \
                 patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
                 patch.object(store, '_snapshot', return_value=('head', 'tree', {path: 'oid', 'runs/2026-09-30.json.enc': 'old'})), \
                 patch.object(store, '_read', return_value=value) as read:
                self.assertEqual(store.request('context', {'date': '2026-10-08'}),
                                 {'existing': value, 'previous': None, 'comparison': None})
                read.assert_called_once_with(path, {path: 'oid', 'runs/2026-09-30.json.enc': 'old'})


def saved_complete_fixture(change=None):
    """Transport/publication fixture; its economic state is deliberately synthetic."""
    close = '2026-09-30'
    report = {**fixture(), 'target_date': close, 'data_date': close,
              'previous_trading_date': '2026-09-29', 'strategy_sha': profile.SOURCE_SHA,
              'simulation_identity': profile.INHERITED, 'state_origin': profile.STATE_ORIGIN.copy(),
              'continuation': {'mode': 'native_full_checkpoint_seed', 'previous_close': close}}
    def complete(files):
        state = json.loads(files['output/risk_state.json'])
        state['checkpoint']['state'] = {
            'schema_version': 1, 'last_completed_close': close, 'dates': [close],
            'initial_capital': 100, 'engine_cfg': {}, 'engine_policy': {}, 'effective_policy': {},
            'account_risk_policy': {}, 'engine': {}, 'tail_policies': {}, 'sleeves': [],
            'portfolio_risk': {}, 'run': {}, 'overlay': None, 'last_opinion': None,
            'last_agreement': None, 'controller': {}, 'budget': {},
        }
        files['output/risk_state.json'] = json.dumps(state).encode()
        pointer = json.loads(files['output/latest_success.json'])
        pointer['state_sha256'] = store.digest(files['output/risk_state.json'])
        files['output/latest_success.json'] = json.dumps(pointer).encode()
        input_path = 'market_data/x.csv'
        input_bytes = files[f'output/snapshots/{close}/{input_path}']
        manifest = {'end_date': close, 'evidence': [{'path': input_path,
                    'bytes': len(input_bytes), 'sha256': store.digest(input_bytes)}]}
        manifest_path = f'output/snapshots/{close}/manifest.json'
        files[manifest_path] = json.dumps(manifest).encode()
        files[f'output/snapshots/{close}/manifest.sha256'] = store.digest(files[manifest_path]).encode()
        files['output/daily_report.json'] = json.dumps(report).encode()
        if change:
            change(files)
    bundle, files = full_bundle(complete)
    saved = {**independent_record(risk=json.loads(files['output/risk_state.json'])),
             'run_id': str(report['actions_run_id']), 'report': report,
             'bundle': base64.b64encode(bundle).decode(), 'sha256': store.digest(bundle), 'bytes': len(bundle)}
    return saved, bundle, files


def read_only_source_fixture():
    """Mock source modules expose only read-only validators, not an engine."""
    def validate(state):
        return None if state.get('schema_version') == 2 else 'FIXTURE_RISK_SCHEMA'
    def completed(directory, state, target):
        output = Path(directory)
        pointer = json.loads((output / 'latest_success.json').read_bytes())
        if (pointer['scan_date'] != target or pointer['run_id'] != state['run_id'] or
                pointer['state_sha256'] != store.digest((output / 'risk_state.json').read_bytes()) or
                pointer['artifact_sha256'] != store.digest((output / pointer['file']).read_bytes())):
            raise ValueError('FIXTURE_PUBLICATION_IDENTITY')
    def snapshot(directory):
        directory = Path(directory)
        raw = (directory / 'manifest.json').read_bytes()
        if store.digest(raw) != (directory / 'manifest.sha256').read_text():
            raise ValueError('FIXTURE_SNAPSHOT_SIGNATURE')
        manifest = json.loads(raw)
        for item in manifest['evidence']:
            raw = (directory / item['path']).read_bytes()
            if len(raw) != item['bytes'] or store.digest(raw) != item['sha256']:
                raise ValueError('FIXTURE_SNAPSHOT_INPUT')
        return manifest
    calls = {name: Mock(name=name) for name in ('prepare_scan', 'freeze_scan', 'probe_market',
             'run_simulation', 'build_argument_parser', 'build_scan_artifact', 'publish_scan')}
    calls.update(validate_risk_state=Mock(side_effect=validate),
                 require_completed_publication=Mock(side_effect=completed),
                 verify_frozen_snapshot=Mock(side_effect=snapshot))
    symbols = {f'{i:06}': f'Fixture {i}' for i in range(17)}
    modules = modules_with({
        'quantfusion.config.daily': {'SYMBOLS': symbols},
        'quantfusion.config.universe': {'SYMBOL_NAMES': symbols, 'ORDERED_SYMBOLS': tuple(symbols)},
        'quantfusion.application.daily_scan': {'build_argument_parser': calls['build_argument_parser']},
        'quantfusion.data.sessions': {'load_calendar': Mock(return_value=object()), 'resolve_scan_dates': Mock()},
        'quantfusion.application.daily_context': {'ScanRequest': Mock(), 'ScanContext': Mock()},
        'quantfusion.application.daily_input': {name: calls[name] for name in ('prepare_scan', 'freeze_scan')},
        'quantfusion.application.daily_market': {'probe_market': calls['probe_market']},
        'quantfusion.application.daily_replay': {'run_simulation': calls['run_simulation']},
        'quantfusion.application.daily_output': {'build_scan_artifact': calls['build_scan_artifact']},
        'quantfusion.application.daily_publication': {'publish_scan': calls['publish_scan']},
        'quantfusion.io.state_store': {'load_prev_risk_state': Mock(), 'save_risk_state': Mock(),
            **{name: calls[name] for name in ('validate_risk_state', 'require_completed_publication')}},
        'quantfusion.data.snapshot': {'verify_frozen_snapshot': calls['verify_frozen_snapshot']},
    })
    return modules, calls


class SameDayRunnerTests(unittest.TestCase):
    def run_saved(self, saved):
        modules, calls = read_only_source_fixture()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, modules))
            stack.enter_context(patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED,
                'RUNNER_TEMP': directory, 'TRADE_PREFETCHED_SOURCE': directory, 'TARGET_DATE': '2026-09-30'}, clear=True))
            stack.enter_context(patch.object(sys, 'path', sys.path.copy()))
            stack.enter_context(patch.object(runner, 'identity', return_value={
                'strategy_sha': profile.SOURCE_SHA, 'trigger': 'workflow_dispatch', 'actions_run_id': '90000'}))
            stack.enter_context(patch.object(runner, 'resolve_target', return_value=('2026-09-30', '2026-09-29', 'READY')))
            api = stack.enter_context(patch.object(store, 'api', side_effect=AssertionError('NO_HTTP')))
            request = stack.enter_context(patch.object(store, 'request',
                side_effect=lambda route, payload: {'existing': saved} if route == 'context' else {'saved': True}))
            restore = stack.enter_context(patch.object(store, 'restore', wraps=store.restore))
            verify = stack.enter_context(patch.object(publisher, 'verified_report', wraps=publisher.verified_report))
            result = runner.run()
            log = (Path(directory) / 'trade-runtime/logs/production.log').read_text()
            api.assert_not_called()
        for name in ('prepare_scan', 'probe_market', 'run_simulation', 'build_argument_parser', 'publish_scan'):
            calls[name].assert_not_called()
        self.assertNotIn('start', [call.args[0] for call in request.call_args_list])
        return result, calls, restore, verify, request, log

    def test_same_day_complete_result_verifies_and_restores_without_preparation_or_replay(self):
        saved, bundle, _ = saved_complete_fixture()
        result, calls, restore, verify, request, _ = self.run_saved(saved)
        self.assertEqual(result, (0, 'EXISTING_RESULT_DEGRADED'))
        self.assertEqual([call.args[0] for call in request.call_args_list], ['context'])
        self.assertTrue(restore.call_args.kwargs['complete'])
        self.assertEqual(restore.call_args.args[0], bundle)
        verify.assert_called_once_with(saved, '2026-09-30', bundle)
        for name in ('validate_risk_state', 'require_completed_publication', 'verify_frozen_snapshot'):
            calls[name].assert_called_once()

    def test_same_day_unresolved_never_restores_or_prepares(self):
        for status in ('RUNNING', 'FAILED', 'UNKNOWN'):
            with self.subTest(status=status):
                result, _, restore, verify, _, log = self.run_saved(independent_record(status))
                self.assertEqual(result, (1, 'PRIVATE_DAILY_TASK_FAILED:EXISTING_RESULT_UNUSABLE'))
                self.assertIn('EXISTING_RESULT_UNUSABLE:' + status, log)
                restore.assert_not_called()
                verify.assert_not_called()

    def test_same_day_missing_budget_or_bad_pointer_is_rejected(self):
        def missing_budget(files):
            state = json.loads(files['output/risk_state.json'])
            state['checkpoint']['state'].pop('budget')
            files['output/risk_state.json'] = json.dumps(state).encode()
            pointer = json.loads(files['output/latest_success.json'])
            pointer['state_sha256'] = store.digest(files['output/risk_state.json'])
            files['output/latest_success.json'] = json.dumps(pointer).encode()
        def bad_pointer(files):
            pointer = json.loads(files['output/latest_success.json'])
            pointer['state_sha256'] = '0' * 64
            files['output/latest_success.json'] = json.dumps(pointer).encode()
        for change, reason in ((missing_budget, 'COMPLETE_EXISTING_RESULT_REQUIRED'),
                               (bad_pointer, 'COMPLETE_PUBLICATION_IDENTITY')):
            with self.subTest(reason=reason):
                saved, _, _ = saved_complete_fixture(change)
                result, calls, _, _, _, log = self.run_saved(saved)
                self.assertEqual(result, (1, 'PRIVATE_DAILY_TASK_FAILED:' + reason))
                self.assertIn(reason, log)
                calls['require_completed_publication'].assert_not_called()
                calls['verify_frozen_snapshot'].assert_not_called()

    def test_same_day_report_must_equal_the_json_inside_its_saved_bundle(self):
        saved, _, _ = saved_complete_fixture()
        saved['report'] = {**saved['report'], 'phase': 'different'}
        result, _, restore, _, _, log = self.run_saved(saved)
        self.assertEqual(result, (1, 'PRIVATE_DAILY_TASK_FAILED:PRODUCTION_VALIDATION_FAILED'))
        self.assertIn('SAVED_REPORT_READBACK', log)
        restore.assert_not_called()

    def test_partial_previous_close_rejects_before_provider_preparation_or_engine(self):
        for missing in ('budget', 'controller'):
            def partial(files):
                state = json.loads(files['output/risk_state.json'])
                state['checkpoint']['state'].pop(missing)
                files['output/risk_state.json'] = json.dumps(state).encode()
                pointer = json.loads(files['output/latest_success.json'])
                pointer['state_sha256'] = store.digest(files['output/risk_state.json'])
                files['output/latest_success.json'] = json.dumps(pointer).encode()
            saved, bundle, _ = saved_complete_fixture(partial)
            previous = {**saved, 'date': '2026-09-30', 'bundle_sha256': saved['sha256'],
                        'profile': {'start_date': '2026-07-01', 'capital': 100}}
            modules, calls = read_only_source_fixture()
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, modules))
                stack.enter_context(patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED,
                    'RUNNER_TEMP': directory, 'TRADE_PREFETCHED_SOURCE': directory,
                    'TARGET_DATE': '2026-10-08'}, clear=True))
                stack.enter_context(patch.object(sys, 'path', sys.path.copy()))
                stack.enter_context(patch.object(runner, 'identity', return_value={
                    'strategy_sha': profile.SOURCE_SHA, 'trigger': 'workflow_dispatch', 'actions_run_id': '90000'}))
                stack.enter_context(patch.object(runner, 'resolve_target',
                    return_value=('2026-10-08', '2026-09-30', 'READY')))
                api = stack.enter_context(patch.object(store, 'api', side_effect=AssertionError('NO_HTTP')))
                request = stack.enter_context(patch.object(store, 'request', side_effect=lambda route, payload:
                    {'existing': None, 'previous': previous} if route == 'context' else {'saved': True}))
                restore = stack.enter_context(patch.object(store, 'restore', wraps=store.restore))
                providers = [stack.enter_context(patch.object(runner, name))
                             for name in ('prepare_regime_inputs', 'prepare_risk_inputs', 'prepare_native_with_retry')]
                self.assertEqual(runner.run(),
                    (1, 'PRIVATE_DAILY_TASK_FAILED:COMPLETE_INHERITED_STATE_REQUIRED'))
                self.assertIn('COMPLETE_INHERITED_STATE_REQUIRED',
                    (Path(directory) / 'trade-runtime/logs/production.log').read_text())
                restore.assert_called_once_with(bundle, Path(directory) / 'trade-runtime', complete=True)
                calls['validate_risk_state'].assert_called_once_with(previous['risk_state'])
                self.assertEqual([call.args[0] for call in request.call_args_list], ['context', 'event'])
                api.assert_not_called()
            for operation in providers:
                operation.assert_not_called()
            for name in ('build_argument_parser', 'prepare_scan', 'probe_market', 'run_simulation',
                         'freeze_scan', 'build_scan_artifact', 'publish_scan', 'verify_frozen_snapshot'):
                calls[name].assert_not_called()


class FakeFrame:
    """Only the date slicing used by the CLI; no pandas or financial calculations."""
    def __init__(self, dates):
        self.index = [date.fromisoformat(day) for day in dates]
    @property
    def loc(self):
        return self
    @property
    def empty(self):
        return not self.index
    def __getitem__(self, key):
        if isinstance(key, slice):
            return FakeFrame([day.isoformat() for day in self.index if day.isoformat() <= key.stop])
        if key == 'date':
            return list(self.index)
        raise KeyError(key)


class ResumeIntervalTests(unittest.TestCase):
    def setUp(self):
        self.days = ('2026-09-22', '2026-09-23', '2026-09-24')
        self.calendar = types.SimpleNamespace(sessions=('2026-09-21', *self.days))
        self.ctx = types.SimpleNamespace(request=types.SimpleNamespace(end_date=self.days[-1], regime_data_dir='/fixture'),
            snapshot_frames={code: FakeFrame(self.days) for code in ('T', 'NEW', 'RISK', 'REG')},
            symbols={'T': 'trading', 'NEW': 'candidate'}, tradable={'T': 'trading'}, scan_dates={})
        self.indices = {code: FakeFrame(self.days) for code in ('000300', '000682')}
        self.listed_from = {}
        self.suspensions = {}
        self.coverage_calls = []
        def eligibility(code, dates):
            return {'status': 'PRE_LISTING' if dates['requested_as_of'] < self.listed_from.get(code, '0000-00-00') else 'SESSION_REQUIRED'}
        def coverage(frame, dates, code, *, allow_certified_suspension):
            day = dates['required_evidence_date']
            self.coverage_calls.append((code, day, dates['requested_as_of'], allow_certified_suspension))
            if day in [stamp.isoformat() for stamp in frame.index]:
                return day
            known = self.suspensions.get((code, day))
            if allow_certified_suspension and known and known <= dates['requested_as_of']:
                return frame.index[-1].isoformat() if frame.index else None
            raise ValueError('FIXTURE_MISSING_BAR:' + code + ':' + day)
        pandas = types.ModuleType('pandas')
        pandas.read_csv = Mock(side_effect=lambda path: self.indices[Path(path).stem])
        pandas.DatetimeIndex = list
        config = modules_with({
            'quantfusion.data.sessions': {'require_frame_coverage': coverage, 'stock_eligibility': eligibility},
            'quantfusion.data.contracts': {'_normalize_index_frame': lambda frame, end_date: frame},
            'quantfusion.config.overlay': {'RISK_BASKET': ('RISK',)},
            'quantfusion.config.portfolio': {'PortfolioPolicy': lambda: types.SimpleNamespace(regime_symbols=('REG',))},
        })
        config['pandas'] = pandas
        self.csv_read = pandas.read_csv
        self.modules = patch.dict(sys.modules, config)
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def validate(self):
        return runner.require_resume_interval(self.ctx, self.calendar, '2026-09-21')

    def test_complete_interval_checks_each_session_and_both_indices(self):
        self.assertEqual(self.validate(), {'previous_close': '2026-09-21', 'sessions': list(self.days),
                                         'stock_count': 4, 'index_count': 2})
        self.assertEqual([Path(call.args[0]).stem for call in self.csv_read.call_args_list], ['000300', '000682'])
        for code in ('INDEX:000300', 'INDEX:000682'):
            self.assertEqual([call for call in self.coverage_calls if call[0] == code],
                             [(code, day, day, False) for day in self.days])

    def test_missing_middle_bar_cannot_be_hidden_by_a_current_tail(self):
        self.ctx.snapshot_frames['T'] = FakeFrame((self.days[0], self.days[-1]))
        with self.assertRaisesRegex(ValueError, 'RESUME_HISTORY_GAP'):
            self.validate()

    def test_certified_suspension_is_accepted_only_when_known_as_of_the_session(self):
        self.ctx.snapshot_frames['T'] = FakeFrame((self.days[0], self.days[-1]))
        self.suspensions[('T', self.days[1])] = self.days[1]
        self.validate()
        self.coverage_calls.clear()
        self.suspensions[('T', self.days[1])] = self.days[-1]
        with self.assertRaisesRegex(ValueError, 'RESUME_HISTORY_GAP'):
            self.validate()
        self.assertIn(('T', self.days[1], self.days[1], True), self.coverage_calls)

    def test_prelisting_empty_prefix_is_allowed_but_a_prelisting_bar_is_not(self):
        self.listed_from['NEW'] = self.days[-1]
        self.ctx.snapshot_frames['NEW'] = FakeFrame((self.days[-1],))
        self.validate()
        self.assertEqual([call[1] for call in self.coverage_calls if call[0] == 'NEW'], [self.days[-1]])
        self.ctx.snapshot_frames['NEW'] = FakeFrame((self.days[1], self.days[-1]))
        with self.assertRaisesRegex(ValueError, 'RESUME_PRE_LISTING_CONFLICT'):
            self.validate()

    def test_each_index_requires_the_middle_session_without_suspension_waiver(self):
        for code in ('000300', '000682'):
            with self.subTest(index=code):
                self.indices = {name: FakeFrame(self.days) for name in ('000300', '000682')}
                self.indices[code] = FakeFrame((self.days[0], self.days[-1]))
                self.suspensions[('INDEX:' + code, self.days[1])] = self.days[1]
                with self.assertRaisesRegex(ValueError, 'RESUME_HISTORY_GAP'):
                    self.validate()

    def test_off_calendar_or_future_evidence_is_rejected(self):
        self.calendar.sessions = ('2026-09-21', self.days[0], self.days[-1])
        with self.assertRaisesRegex(ValueError, 'RESUME_UNEXPECTED_EVIDENCE_DATE'):
            self.validate()
        self.calendar.sessions = ('2026-09-21', *self.days)
        self.ctx.snapshot_frames['T'] = FakeFrame((*self.days, '2026-09-25'))
        with self.assertRaisesRegex(ValueError, 'RESUME_UNEXPECTED_EVIDENCE_DATE'):
            self.validate()

    def test_empty_missing_or_unexpected_inventory_fails_before_index_reads(self):
        complete = self.ctx.snapshot_frames.copy()
        cases = [{}, {key: value for key, value in complete.items() if key != 'RISK'},
                 {key: value for key, value in complete.items() if key != 'REG'},
                 {**complete, 'UNEXPECTED': FakeFrame(self.days)}]
        for frames in cases:
            with self.subTest(inventory=sorted(frames)):
                self.ctx.snapshot_frames = frames
                with self.assertRaisesRegex(ValueError, 'RESUME_FRAME_INVENTORY'):
                    self.validate()
        self.csv_read.assert_not_called()

    def test_suspension_coverage_does_not_allow_the_account_clock_to_skip_a_session(self):
        for code in ('T', 'REG'):
            self.ctx.snapshot_frames[code] = FakeFrame((self.days[0], self.days[-1]))
            self.suspensions[(code, self.days[1])] = self.days[1]
        with self.assertRaisesRegex(ValueError, 'RESUME_ECONOMIC_SESSION_MISSING'):
            self.validate()


if __name__ == '__main__':
    unittest.main()
