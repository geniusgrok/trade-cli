import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import private_store as store
import publish_report as publisher
import report as reporting
import runtime_profile as profile
import seed_contract
import seed_inherited as importer
from test_independent_series import full_bundle
from test_publication import fixture


def contract_fixture():
    bundle = b'synthetic bundle bytes; native validation is separate'
    provenance = {'kind': 'authenticated_original',
        'original_bundle_sha256': 'e72529cd310c61393f7016d6ce400e7349ec50ae0a32ffb2887e130dd33e1050',
        'recovery_receipt_sha256': None}
    payload = {'schema_version': 1, 'simulation_identity': profile.INHERITED, 'state_origin': profile.STATE_ORIGIN.copy(),
        'seed_profile': {'start_date': '2026-07-01', 'capital': 100.0, 'config_fingerprint': 'synthetic request',
                         'universe': [[f'{i:06}', '测试'] for i in range(17)]},
        'seed_bundle': base64.b64encode(bundle).decode(), 'seed_bundle_sha256': store.digest(bundle),
        'evidence_date': '2026-10-08', 'evidence_bundle': base64.b64encode(bundle).decode(),
        'evidence_bundle_sha256': store.digest(bundle), 'evidence_provenance': provenance}
    plaintext = json.dumps(payload).encode(); cipher = b'synthetic sealed bytes'
    manifest = {name: '1' * 64 for name in seed_contract.MANIFEST_FIELDS if name.endswith('_sha256')}
    manifest.update(schema_version=1, identity=profile.INHERITED, old_source=profile.STATE_ORIGIN['old_source'],
        cutover_source=profile.SOURCE_SHA, baseline_close='2026-09-30', state_origin=profile.STATE_ORIGIN.copy(),
        cipher_file='approved_seed.enc', cipher_sha256=store.digest(cipher), git_blob_oid=publisher.blob_id(cipher),
        plaintext_sha256=store.digest(plaintext), seed_bundle_sha256=store.digest(bundle), evidence_date='2026-10-08',
        evidence_bundle_sha256=store.digest(bundle), evidence_provenance=provenance,
        native_identity={'source_sha256': '2'*64, 'requirements_lock_sha256': '3'*64, 'calendar_sha256': '4'*64,
                         'runtime': {'python': '3.12.14', 'numpy': '2.5.1', 'pandas': '3.0.5'}})
    return manifest, payload, plaintext, cipher


def seed_record(actions_id):
    data, original = full_bundle()
    report = {**fixture(), 'target_date': '2026-09-30', 'data_date': '2026-09-30',
        'strategy_sha': profile.SOURCE_SHA, 'actions_run_id': str(actions_id),
        'actions_run_url': f'https://github.com/geniusgrok/trade-cli/actions/runs/{actions_id}', 'trigger': 'workflow_dispatch',
        'simulation_identity': profile.INHERITED, 'state_origin': profile.STATE_ORIGIN.copy(), 'profile': {'synthetic': True},
        'continuation': {'mode': 'native_full_checkpoint_seed', 'previous_close': '2026-09-30'}}
    markdown = reporting.markdown(report)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); store.restore(data, root, complete=True)
        (root / 'output/daily_report.json').write_text(json.dumps(report))
        (root / 'output/daily_report.md').write_text(markdown)
        bundle = store.pack(root)
    return {'status': 'DEGRADED', 'run_id': str(actions_id), 'strategy_sha': profile.SOURCE_SHA,
        'simulation_identity': profile.INHERITED, 'state_origin': profile.STATE_ORIGIN.copy(), 'report': report,
        'markdown': markdown, 'risk_state': json.loads(original['output/risk_state.json']),
        'bundle': base64.b64encode(bundle).decode(), 'bytes': len(bundle), 'sha256': store.digest(bundle),
        'seed_import': {'plaintext_sha256': '1'*64, 'manifest_sha256': '2'*64,
            'seed_bundle_sha256': '3'*64, 'seed_state_sha256': store.digest(original['output/risk_state.json']),
            'seed_artifact_sha256': store.digest(original['output/signals_2026-09-30.json']),
            'seed_snapshot_manifest_sha256': store.digest(original['output/snapshots/2026-09-30/manifest.json'])}}


class SeedImportTests(unittest.TestCase):
    def test_approved_hashes_and_fixed_origin_are_all_required(self):
        manifest, payload, plaintext, cipher = contract_fixture()
        self.assertEqual(seed_contract.validate(manifest, plaintext, cipher), payload)
        for field in ('cipher_sha256', 'git_blob_oid', 'plaintext_sha256', 'seed_bundle_sha256'):
            changed = copy.deepcopy(manifest); changed[field] = '0' * len(changed[field])
            with self.subTest(field=field), self.assertRaises(ValueError):
                seed_contract.validate(changed, plaintext, cipher)
        changed = copy.deepcopy(manifest); changed['state_origin']['capture_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'INDEPENDENT_STATE_ORIGIN'):
            seed_contract.validate(changed, plaintext, cipher)

    def test_payload_relabeling_or_partial_schema_is_rejected_even_with_new_hash(self):
        manifest, payload, _, cipher = contract_fixture()
        for change in (lambda p: p.update(simulation_identity='legacy'),
                       lambda p: p['state_origin'].update(admission_change='unchanged'),
                       lambda p: p.pop('seed_profile'),
                       lambda p: p['seed_profile'].update(capital=True)):
            modified = copy.deepcopy(payload); change(modified); plaintext = json.dumps(modified).encode()
            changed_manifest = copy.deepcopy(manifest); changed_manifest['plaintext_sha256'] = store.digest(plaintext)
            with self.subTest(change=change), self.assertRaises(ValueError):
                seed_contract.validate(changed_manifest, plaintext, cipher)

    def test_duplicate_json_keys_cannot_override_reviewed_fields(self):
        with self.assertRaisesRegex(ValueError, 'IMPORT_DUPLICATE_JSON_KEY'):
            seed_contract.strict_json(b'{"identity":"a","identity":"b"}')

    def test_safe_retry_with_new_actions_id_keeps_the_original_seed(self):
        original = seed_record('123'); expected = seed_record('456')
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}):
            importer.verify_existing(original, expected)
            with patch('private_store.api', return_value={'object': {'sha': 'head'}}) as api, \
                 patch('private_store._snapshot', return_value=('head', 'tree', {'runs/2026-09-30.json.enc': 'blob'})), \
                 patch('private_store._read', return_value=original):
                importer.initialize(expected)
        self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))
        self.assertEqual(original['run_id'], '123')

    def test_unknown_or_changed_seed_never_writes(self):
        expected = seed_record('123')
        for original in (None, {**expected, 'status': 'UNKNOWN'}, {**expected, 'seed_import': {}}):
            with self.subTest(original=original is None), \
                 patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
                 patch('private_store.api', return_value={'object': {'sha': 'head'}}) as api, \
                 patch('private_store._snapshot', return_value=('head', 'tree', {'runs/2026-09-30.json.enc': 'blob'})), \
                 patch('private_store._read', return_value=original):
                with self.assertRaises(ValueError): importer.initialize(expected)
            self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))

    def test_import_requires_explicit_independent_identity_before_api_access(self):
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': ''}), patch('private_store.api') as api:
            with self.assertRaisesRegex(ValueError, 'EXPLICIT_INDEPENDENT_IDENTITY_REQUIRED'):
                importer.initialize({})
        api.assert_not_called()

    def test_recovery_receipt_must_be_in_the_authenticated_bundle(self):
        provenance = {'kind': 'provider_full_history_recovery',
            'original_bundle_sha256': 'e72529cd310c61393f7016d6ce400e7349ec50ae0a32ffb2887e130dd33e1050',
            'recovery_receipt_sha256': '1'*64}
        data, _ = full_bundle()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'IMPORT_RECOVERY_RECEIPT_REQUIRED'):
                importer.extract_snapshot(data, Path(directory), '2026-10-08', provenance)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_previous_context_carries_only_the_verified_origin(self):
        previous = seed_record('123')
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
             patch('private_store._snapshot', return_value=('head', 'tree', {'runs/2026-09-30.json.enc': 'blob'})), \
             patch('private_store._read', side_effect=[None, previous, previous]):
            context = store.request('context', {'date': '2026-10-08', 'compare_date': '2026-09-30'})
        self.assertEqual(context['previous']['state_origin'], profile.STATE_ORIGIN)
        self.assertIsNot(context['previous']['state_origin'], previous['state_origin'])


if __name__ == '__main__':
    unittest.main()
