"""Exact reviewed import schema, hashes and fixed reconstruction provenance."""
import base64
import hashlib
import json
import math
import re

import runtime_profile as profile

MANIFEST_FIELDS = {
    'schema_version', 'identity', 'old_source', 'cutover_source', 'baseline_close', 'state_origin',
    'cipher_file', 'cipher_sha256', 'git_blob_oid', 'plaintext_sha256', 'seed_bundle_sha256',
    'evidence_date', 'evidence_bundle_sha256', 'seed_state_sha256', 'seed_artifact_sha256',
    'seed_snapshot_manifest_sha256', 'evidence_snapshot_manifest_sha256', 'native_identity', 'evidence_provenance',
}
PAYLOAD_FIELDS = {
    'schema_version', 'simulation_identity', 'state_origin', 'seed_profile',
    'seed_bundle', 'seed_bundle_sha256', 'evidence_date', 'evidence_bundle', 'evidence_bundle_sha256', 'evidence_provenance',
}


def require_evidence_provenance(value: dict) -> None:
    if (not isinstance(value, dict) or set(value) != {'kind', 'original_bundle_sha256', 'recovery_receipt_sha256'} or
            value['original_bundle_sha256'] != 'e72529cd310c61393f7016d6ce400e7349ec50ae0a32ffb2887e130dd33e1050' or
            value['kind'] not in ('authenticated_original', 'provider_full_history_recovery')):
        raise ValueError('IMPORT_EVIDENCE_PROVENANCE')
    receipt = value['recovery_receipt_sha256']
    if ((value['kind'] == 'authenticated_original' and receipt is not None) or
            (value['kind'] == 'provider_full_history_recovery' and not re.fullmatch('[0-9a-f]{64}', str(receipt)))):
        raise ValueError('IMPORT_EVIDENCE_PROVENANCE')


def strict_json(raw: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('IMPORT_DUPLICATE_JSON_KEY')
            value[key] = item
        return value
    def nonfinite(_):
        raise ValueError('IMPORT_NONFINITE_JSON')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError('IMPORT_DOCUMENT')
    return value


def validate_manifest(manifest: dict, cipher: bytes | None = None) -> None:
    if (set(manifest) != MANIFEST_FIELDS or type(manifest['schema_version']) is not int or
            manifest['schema_version'] != 1 or manifest['identity'] != profile.INHERITED or
            manifest['old_source'] != profile.STATE_ORIGIN['old_source'] or
            manifest['cutover_source'] != profile.SOURCE_SHA or manifest['baseline_close'] != '2026-09-30' or
            manifest['evidence_date'] != '2026-10-08' or manifest['cipher_file'] != 'approved_seed.enc'):
        raise ValueError('IMPORT_MANIFEST_IDENTITY')
    profile.require_origin(manifest['state_origin'])
    require_evidence_provenance(manifest['evidence_provenance'])
    for name in MANIFEST_FIELDS:
        if name.endswith('_sha256') and not re.fullmatch('[0-9a-f]{64}', str(manifest[name])):
            raise ValueError('IMPORT_MANIFEST_HASH')
    if not re.fullmatch('[0-9a-f]{40}', str(manifest['git_blob_oid'])):
        raise ValueError('IMPORT_MANIFEST_BLOB')
    identity = manifest['native_identity']
    if not isinstance(identity, dict) or set(identity) != {'source_sha256', 'requirements_lock_sha256', 'calendar_sha256', 'runtime'}:
        raise ValueError('IMPORT_NATIVE_IDENTITY')
    for name in ('source_sha256', 'requirements_lock_sha256', 'calendar_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', str(identity[name])):
            raise ValueError('IMPORT_NATIVE_IDENTITY')
    runtime = identity['runtime']
    if (not isinstance(runtime, dict) or set(runtime) != {'python', 'numpy', 'pandas'} or
            runtime['python'] != '3.12.14' or any(not isinstance(value, str) for value in runtime.values())):
        raise ValueError('IMPORT_RUNTIME_IDENTITY')
    if cipher is not None:
        blob = hashlib.sha1(f'blob {len(cipher)}\0'.encode() + cipher).hexdigest()
        if hashlib.sha256(cipher).hexdigest() != manifest['cipher_sha256'] or blob != manifest['git_blob_oid']:
            raise ValueError('IMPORT_CIPHER_IDENTITY')


def validate(manifest: dict, plaintext: bytes, cipher: bytes | None = None) -> dict:
    """Called by the reviewed packer and receiver; never print private fields."""
    validate_manifest(manifest, cipher)
    if len(plaintext) > 8 * 1024 * 1024 or hashlib.sha256(plaintext).hexdigest() != manifest['plaintext_sha256']:
        raise ValueError('IMPORT_PLAINTEXT_IDENTITY')
    payload = strict_json(plaintext)
    if (set(payload) != PAYLOAD_FIELDS or type(payload['schema_version']) is not int or payload['schema_version'] != 1 or
            payload['simulation_identity'] != profile.INHERITED or payload['evidence_date'] != manifest['evidence_date']):
        raise ValueError('IMPORT_PAYLOAD_IDENTITY')
    profile.require_origin(payload['state_origin'])
    require_evidence_provenance(payload['evidence_provenance'])
    if payload['evidence_provenance'] != manifest['evidence_provenance']:
        raise ValueError('IMPORT_EVIDENCE_PROVENANCE')
    request = payload['seed_profile']
    if (not isinstance(request, dict) or set(request) != {'start_date', 'capital', 'universe', 'config_fingerprint'} or
            request['start_date'] != '2026-07-01' or type(request['capital']) not in (int, float) or
            not math.isfinite(request['capital']) or request['capital'] <= 0 or
            not isinstance(request['config_fingerprint'], str) or not isinstance(request['universe'], list) or
            len(request['universe']) != 17 or any(not isinstance(row, list) or len(row) != 2 or
                not isinstance(row[0], str) or not re.fullmatch('[0-9]{6}', row[0]) or not isinstance(row[1], str)
                for row in request['universe']) or len({row[0] for row in request['universe']}) != 17):
        raise ValueError('IMPORT_REQUEST_IDENTITY')
    for kind in ('seed', 'evidence'):
        raw = base64.b64decode(payload[kind + '_bundle'], validate=True)
        expected = manifest[kind + '_bundle_sha256']
        if len(raw) > 8 * 1024 * 1024 or payload[kind + '_bundle_sha256'] != expected or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('IMPORT_BUNDLE_IDENTITY')
    return payload
