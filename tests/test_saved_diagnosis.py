"""Saved-state diagnostics expose only approved metadata and never write state."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import types
import unittest
from unittest.mock import Mock, patch

import diagnose_saved as diagnosis

SYMBOLS = {f'{n:06}': 'fixture' for n in range(1, 18)}
OLD = 'start=2026-07-01|indicator=warm|capital=2000000.0|warmup=365|deployment=auto'
CURRENT = OLD + '|admission=systemic'
SHA = 'a' * 40


def identity(symbols, config):
    return hashlib.sha256(('trade|' + str(len(symbols)) + '|' + ','.join(sorted(symbols)) + '|' + config).encode()).hexdigest()[:16]


def saved(day, config, *, tamper=False, absent=False):
    state = {'scan_date': '2026-09-30', 'symbols_hash': identity(SYMBOLS, OLD),
        'terminal_risk_lock': True, 'sector_guard_active': False, 'final_assets': 9876543.21}
    report = {'target_date': day, 'status': 'DEGRADED', 'strategy_sha': SHA,
        'actions_run_id': '123', 'account': 'PRIVATE_ACCOUNT_MUST_NOT_APPEAR',
        'profile': {'start_date': '2026-07-01', 'capital': 2_000_000., 'config_fingerprint': config}}
    raw = ('date,close\n2025-12-31,10\n2026-01-05,11\n' + day + ',12\n').encode()
    manifest = {'end_date': day, 'evidence': [] if absent else [{'path': 'market_data/920045.csv', 'bytes': len(raw),
        'sha256': '0' * 64 if tamper else hashlib.sha256(raw).hexdigest()}]}
    manifest_raw = json.dumps(manifest).encode()
    digest = hashlib.sha256(manifest_raw).hexdigest()
    native = {'scan_date': day, 'symbols': SYMBOLS, 'warmup_health': {'required_days': 240},
        'deployment': {'snapshot_manifest_sha256': digest}}
    prefix = 'output/snapshots/' + day + '/'
    entries = {'output/signals_' + day + '.json': json.dumps(native).encode(),
        'output/daily_report.json': json.dumps(report).encode(),
        'output/risk_state.json': json.dumps(state).encode(),
        prefix + 'manifest.json': manifest_raw, prefix + 'manifest.sha256': digest.encode(),
        prefix + 'market_data/920045.csv': raw}
    if absent:
        entries.pop(prefix + 'market_data/920045.csv')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(name); member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    bundle = stream.getvalue()
    return {'status': 'DEGRADED', 'run_id': '123', 'report': report,
        'risk_state': state if config == OLD else None, 'bundle': base64.b64encode(bundle).decode(),
        'bytes': len(bundle), 'sha256': hashlib.sha256(bundle).hexdigest()}


class SavedDiagnosisTests(unittest.TestCase):
    def test_date_history_and_manifest_are_actual_bundle_facts(self):
        facts = diagnosis.inspect_saved(saved('2026-10-08', CURRENT), '2026-10-08', ['920045'])
        self.assertEqual(facts['coverage'][0]['pre_start_bars'], 2)
        self.assertTrue(facts['coverage'][0]['warmup_short'])
        self.assertEqual(facts['coverage'][0]['first_date'], '2025-12-31')
        with self.assertRaisesRegex(ValueError, 'INVALID_SAVED_EVIDENCE'):
            diagnosis.inspect_saved(saved('2026-10-08', CURRENT, tamper=True), '2026-10-08', ['920045'])

    def test_unrecorded_old_reference_is_unknown_instead_of_fabricated_zero_history(self):
        facts = diagnosis.inspect_saved(saved('2026-09-30', OLD, absent=True), '2026-09-30', ['920045'])
        self.assertFalse(facts['coverage'][0]['present'])
        self.assertIsNone(facts['coverage'][0]['pre_start_bars'])
        self.assertIsNone(facts['coverage'][0]['warmup_short'])

    def run_diagnosis(self, snapshots):
        records = {'runs/2026-09-30.json.enc': saved('2026-09-30', OLD),
            'runs/2026-10-08.json.enc': saved('2026-10-08', CURRENT)}
        files = {name: 'b' * 40 for name in records}
        context = types.ModuleType('quantfusion.application.daily_context')
        context.ScanRequest = Mock(return_value=object())
        context.ScanContext = Mock(return_value=types.SimpleNamespace(config_fingerprint=CURRENT))
        daily = types.ModuleType('quantfusion.config.daily'); daily.SYMBOLS = SYMBOLS
        overlay = types.ModuleType('quantfusion.config.overlay'); overlay.RISK_BASKET = ('920045',)
        state_store = types.ModuleType('quantfusion.io.state_store'); state_store.compute_identity_hash = identity
        modules = {'quantfusion': types.ModuleType('quantfusion'),
            'quantfusion.application': types.ModuleType('quantfusion.application'),
            'quantfusion.config': types.ModuleType('quantfusion.config'),
            'quantfusion.io': types.ModuleType('quantfusion.io'),
            context.__name__: context, daily.__name__: daily, overlay.__name__: overlay,
            state_store.__name__: state_store}
        def read(path, listing):
            self.assertIs(listing, files)
            return records[path]
        with patch.dict('sys.modules', modules), patch('sys.path', __import__('sys').path.copy()), \
             patch.object(diagnosis.store, '_snapshot', side_effect=[(head, 'tree', files) for head in snapshots]), \
             patch.object(diagnosis.store, '_read', side_effect=read), \
             patch.object(diagnosis.store, '_commit', side_effect=AssertionError('must never write')), \
             patch.object(diagnosis.store, 'request', side_effect=AssertionError('must never submit')), \
             patch.object(diagnosis.subprocess, 'check_output', return_value=SHA):
            return diagnosis.diagnose(Path('read-only-source'), '2026-10-08')

    def test_lock_and_config_metadata_are_preserved_without_funds_or_identity_rewrite(self):
        proof = self.run_diagnosis(['c' * 40, 'c' * 40])
        text = json.dumps(proof)
        self.assertNotIn('PRIVATE_ACCOUNT_MUST_NOT_APPEAR', text)
        self.assertNotIn('9876543.21', text)
        self.assertNotIn('2000000', text)
        self.assertTrue(proof['config_fields_equal']['capital'])
        self.assertFalse(proof['config_fields_equal']['admission'])
        self.assertTrue(proof['previous_hash_matches_saved_config'])
        self.assertTrue(proof['target_keeps_previous_risk_state'])
        self.assertEqual(proof['previous_locks'], {'terminal_risk_lock': True, 'sector_guard_active': False})
        self.assertFalse(proof['business_writes'])

    def test_branch_advance_retains_the_initial_immutable_evidence_snapshot(self):
        proof = self.run_diagnosis(['c' * 40, 'd' * 40])
        self.assertEqual(proof['state_commit'], 'c' * 40)
        self.assertEqual(proof['state_commit_after_read'], 'd' * 40)
        self.assertTrue(proof['branch_advanced_during_read'])


if __name__ == '__main__':
    unittest.main()
