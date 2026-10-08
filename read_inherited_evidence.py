"""GET-only authentication and recipient sealing of two pinned paper records."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import tarfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import private_store as store
import public_report
import publish_report as publisher
import report as reporting
import runtime_profile as profile
from seed_contract import strict_json

LEGACY = '9cbb3d2a7648715422ff41739de621e9d4c12b5f'
RECIPIENT_SHA = '668be6f304597fb7cf8e9ab3ca79da3b340db5273d6e10e8caa91f9c0c8ae284'
STATE_FIELDS = {'schema_version', 'last_completed_close', 'dates', 'initial_capital', 'engine_cfg', 'engine_policy',
    'effective_policy', 'account_risk_policy', 'engine', 'tail_policies', 'sleeves', 'portfolio_risk', 'run', 'overlay',
    'last_opinion', 'last_agreement', 'controller', 'budget'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def validate_expected(expected):
    if (set(expected) != {'schema_version', 'identity', 'source', 'workflow_sha', 'legacy_commit', 'state_commit', 'records',
                         'native_identity', 'config_fingerprint_sha256', 'public_report', 'seed_import', 'input_evidence'} or
            type(expected['schema_version']) is not int or expected['schema_version'] != 1 or
            expected['identity'] != profile.INHERITED or expected['source'] != profile.SOURCE_SHA or expected['legacy_commit'] != LEGACY):
        raise ValueError('READBACK_EXPECTED_IDENTITY')
    if not re.fullmatch('[0-9a-f]{40}', expected['state_commit']):
        raise ValueError('READBACK_UNPINNED_COMMIT')
    if not re.fullmatch('[0-9a-f]{64}', expected['config_fingerprint_sha256']):
        raise ValueError('READBACK_UNPINNED_PROFILE')
    if not re.fullmatch('[0-9a-f]{40}', expected['workflow_sha']):
        raise ValueError('READBACK_UNPINNED_WORKFLOW')
    if not isinstance(expected['records'], list) or [item.get('date') for item in expected['records']] != ['2026-09-30', '2026-10-08']:
        raise ValueError('READBACK_RECORD_SET')
    for item in expected['records']:
        if (set(item) != {'date', 'cipher_oid', 'cipher_sha256', 'cipher_bytes', 'actions_run_id', 'status'} or
                not re.fullmatch('[0-9a-f]{40}', item['cipher_oid']) or not re.fullmatch('[0-9a-f]{64}', item['cipher_sha256']) or
                type(item['cipher_bytes']) is not int or
                not 32 <= item['cipher_bytes'] <= 12_000_000 or not re.fullmatch('[0-9]+', item['actions_run_id']) or
                item['status'] not in ('SUCCESS', 'DEGRADED')):
            raise ValueError('READBACK_UNPINNED_RECORD')
    native = expected['native_identity']
    if (not isinstance(native, dict) or set(native) != {'source_sha256', 'requirements_lock_sha256', 'calendar_sha256', 'runtime'} or
            any(not re.fullmatch('[0-9a-f]{64}', native[name]) for name in native if name != 'runtime') or
            native['runtime'].get('python') != '3.12.14' or set(native['runtime']) != {'python', 'numpy', 'pandas'}):
        raise ValueError('READBACK_UNPINNED_NATIVE_IDENTITY')
    public = expected['public_report']
    if (set(public) != {'path', 'commit', 'git_blob_oid', 'sha256', 'bytes'} or
            public['path'] != 'reports/a92-inherited-v1/2026-10-08.md' or
            any(not re.fullmatch('[0-9a-f]{40}', public[name]) for name in ('commit', 'git_blob_oid')) or
            not re.fullmatch('[0-9a-f]{64}', public['sha256']) or type(public['bytes']) is not int or not 0 < public['bytes'] <= 500_000):
        raise ValueError('READBACK_UNPINNED_PUBLIC_REPORT')
    seed = expected['seed_import']
    if (not isinstance(seed, dict) or set(seed) != {'manifest_sha256', 'plaintext_sha256', 'seed_bundle_sha256',
            'seed_artifact_sha256', 'seed_snapshot_manifest_sha256', 'seed_state_sha256'} or
            any(not re.fullmatch('[0-9a-f]{64}', str(value)) for value in seed.values())):
        raise ValueError('READBACK_UNPINNED_SEED')
    from seed_contract import require_evidence_provenance
    require_evidence_provenance(expected['input_evidence'])


def members_of(bundle):
    members, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        for item in archive:
            path = PurePosixPath(item.name); total += item.size
            if (not item.isfile() or path.is_absolute() or '..' in path.parts or item.name in members or
                    total > store.MAX_EXPANDED or len(members) >= 5000):
                raise ValueError('READBACK_UNSAFE_BUNDLE')
            raw = archive.extractfile(item).read(item.size + 1)
            if len(raw) != item.size:
                raise ValueError('READBACK_MEMBER_LENGTH')
            members[item.name] = raw
    return members


def validate_record(value, raw, pin, expected):
    profile.require_record(value)
    day = pin['date']; report = value['report']; state = value['risk_state']
    if (strict_json(raw) != value or value['run_id'] != pin['actions_run_id'] or value['status'] != pin['status'] or
            report['target_date'] != day or state['scan_date'] != day or state['schema_version'] != 2 or
            report['workflow_sha'] != expected['workflow_sha']):
        raise ValueError('READBACK_RECORD_IDENTITY')
    seed = day == '2026-09-30'
    if (report['continuation'] != {'mode': 'native_full_checkpoint_seed' if seed else 'native_full_checkpoint_resume',
                                  'previous_close': '2026-09-30'} or value['previous_date'] != (None if seed else '2026-09-30') or
            (seed and value.get('seed_import') != expected['seed_import']) or
            (not seed and report.get('input_evidence') != expected['input_evidence'])):
        raise ValueError('READBACK_CONTINUATION_BINDING')
    if not re.fullmatch('[0-9a-f]{64}', str(value['sha256'])):
        raise ValueError('READBACK_BUNDLE_IDENTITY')
    bundle = store.decode_bundle(value['bundle'], value['sha256'])
    if type(value['bytes']) is not int or value['bytes'] != len(bundle):
        raise ValueError('READBACK_BUNDLE_IDENTITY')
    publisher.verified_report(value, day, bundle)
    files = members_of(bundle)
    if strict_json(files['output/risk_state.json']) != state or strict_json(files['output/daily_report.json']) != report:
        raise ValueError('READBACK_STATE_REPORT_BINDING')
    if value['markdown'] != reporting.markdown(report) or files['output/daily_report.md'] != value['markdown'].encode():
        raise ValueError('READBACK_READING_REPORT_BINDING')
    artifact_bytes = files[f'output/signals_{day}.json']; native = strict_json(artifact_bytes)
    pointer = strict_json(files['output/latest_success.json']); checkpoint = state['checkpoint']
    if (set(checkpoint) != {'identity', 'prefix_hashes', 'state', 'artifact_sha256', 'payload_sha256'} or
            digest(canonical({key: item for key, item in checkpoint.items() if key != 'payload_sha256'})) != checkpoint['payload_sha256'] or
            set(checkpoint['state']) != STATE_FIELDS or checkpoint['state']['schema_version'] != 1 or
            checkpoint['state']['last_completed_close'] != day or native.get('status') != 'ok' or native.get('mode') != 'simulation' or
            native.get('risk_state_saved') is not True or native['scan_date'] != day or native['run_id'] != state['run_id'] or
            pointer.get('file') != f'signals_{day}.json' or pointer.get('scan_date') != day or pointer.get('run_id') != state['run_id'] or
            pointer.get('artifact_sha256') != digest(artifact_bytes) or pointer.get('state_sha256') != digest(files['output/risk_state.json']) or
            checkpoint['artifact_sha256'] != digest(canonical({**native, 'risk_state_saved': False}))):
        raise ValueError('READBACK_COMPLETE_PUBLICATION')
    identity = checkpoint['identity']; request = report['profile']['config_fingerprint']
    if ({key: identity[key] for key in expected['native_identity']} != expected['native_identity'] or
            identity['request'] != request or digest(request.encode()) != expected['config_fingerprint_sha256'] or
            report['profile']['capital'] != checkpoint['state']['initial_capital'] or
            report['profile']['universe'] != [[code, name] for code, name in native['symbols'].items()] or
            identity['symbols'] != native['symbols'] or len(identity['symbols']) != 17):
        raise ValueError('READBACK_PROFILE_NATIVE_BINDING')
    symbol_hash = digest('|'.join(['trade', '17', ','.join(sorted(identity['symbols'])), request]).encode())[:16]
    if state.get('symbols_hash') != symbol_hash or state.get('total_symbols') != 17:
        raise ValueError('READBACK_RISK_SYMBOL_IDENTITY')
    prefix = f'output/snapshots/{day}/'; manifest_raw = files[prefix + 'manifest.json']; manifest = strict_json(manifest_raw)
    manifest_sha = digest(manifest_raw)
    symbols = manifest.get('symbols')
    if (not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols) or
            any(not isinstance(code, str) or not re.fullmatch('[0-9]{6}', code) for code in symbols)):
        raise ValueError('READBACK_SNAPSHOT_SYMBOLS')
    required = {f'market_data/{code}.csv' for code in symbols} | {'regime_data/000300.csv', 'regime_data/000682.csv'}
    if (manifest['end_date'] != day or manifest.get('deployment_policy') != 'production_daily_replay' or
            files[prefix + 'manifest.sha256'].decode().strip() != manifest_sha or
            native['deployment']['snapshot_manifest_sha256'] != manifest_sha):
        raise ValueError('READBACK_SNAPSHOT_BINDING')
    declared = set()
    for entry in manifest['evidence']:
        path = PurePosixPath(entry['path'])
        if path.is_absolute() or '..' in path.parts or entry['path'] in declared or entry['path'] not in required:
            raise ValueError('READBACK_SNAPSHOT_PATH')
        declared.add(entry['path']); content = files[prefix + entry['path']]
        if len(content) != entry['bytes'] or digest(content) != entry['sha256']:
            raise ValueError('READBACK_SNAPSHOT_FILE')
    actual = {name[len(prefix):] for name in files if name.startswith(prefix)}
    if declared != required or actual != declared | {'manifest.json', 'manifest.sha256'}:
        raise ValueError('READBACK_SNAPSHOT_FILE_SET')
    if seed and any(digest(files[path]) != expected['seed_import'][field] for path, field in (
            ('output/risk_state.json', 'seed_state_sha256'),
            (f'output/signals_{day}.json', 'seed_artifact_sha256'),
            (prefix + 'manifest.json', 'seed_snapshot_manifest_sha256'))):
        raise ValueError('READBACK_REVIEWED_SEED_BINDING')
    return {'date': day, 'status': value['status'], 'actions_run_id': value['run_id'],
        'raw_record_bytes': len(raw), 'raw_record_sha256': digest(raw), 'bundle_bytes': len(bundle), 'bundle_sha256': digest(bundle),
        'risk_state_schema_version': 2, 'native_publication_complete': True, 'risk_state_sha256': digest(files['output/risk_state.json']),
        'native_artifact_sha256': digest(artifact_bytes), 'snapshot_manifest_sha256': manifest_sha,
        'checkpoint_payload_sha256': checkpoint['payload_sha256'], 'checkpoint_state_canonical_sha256': digest(canonical(checkpoint['state'])),
        'checkpoint_state_fields': sorted(checkpoint['state']), 'portfolio_risk_field_types': {key: type(item).__name__ for key, item in checkpoint['state']['portfolio_risk'].items()},
        'profile_matches_expected': True, 'state_origin': report['state_origin'], 'native_identity': expected['native_identity'],
        'workflow_sha': report['workflow_sha'], 'source_sha': value['strategy_sha']}


def seal_transport(raw, oid, path, public_key):
    aad = ('trade-checkpoint-evidence-v1|' + oid + '|' + path).encode()
    key, nonce = os.urandom(32), os.urandom(12)
    wrapped = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=aad))
    sealed = b'TCP1' + struct.pack('>H', len(wrapped)) + wrapped + nonce + AESGCM(key).encrypt(nonce, raw, aad)
    encoded = base64.b64encode(sealed).decode()
    return sealed, [encoded[i:i + 12000] for i in range(0, len(encoded), 12000)]


def read_evidence():
    directory = Path(__file__).parent
    expected = strict_json((directory / 'expected.json').read_bytes()); validate_expected(expected)
    os.environ['TRADE_SIMULATION_IDENTITY'] = profile.INHERITED
    public_bytes = (directory / 'evidence_recipient.pub').read_bytes()
    public_key = serialization.load_pem_public_key(public_bytes)
    if digest(public_bytes) != RECIPIENT_SHA or not isinstance(public_key, RSAPublicKey) or public_key.key_size < 2048:
        raise ValueError('READBACK_RECIPIENT_IDENTITY')
    original_api, store_api = publisher.api, store.api
    def readonly(method, path, *args, **kwargs):
        if method != 'GET': raise ValueError('NON_READ_OPERATION_FORBIDDEN')
        return original_api(method, path, *args, **kwargs)
    publisher.api = store.api = readonly
    try:
        legacy_before = readonly('GET', '/git/ref/heads/runtime-state')['object']['sha']
        before, _, files = store._snapshot()
        if legacy_before != LEGACY or before != expected['state_commit']:
            raise ValueError('READBACK_BRANCH_BEFORE')
        values, metadata, transports = [], [], []
        for pin in expected['records']:
            path = f"runs/{pin['date']}.json.enc"
            if files.get(path) != pin['cipher_oid']: raise ValueError('READBACK_CIPHER_OID')
            item = readonly('GET', '/git/blobs/' + pin['cipher_oid'], limit=18_000_000)
            if item['encoding'] != 'base64': raise ValueError('READBACK_BLOB_ENCODING')
            ciphertext = base64.b64decode(''.join(item['content'].split()), validate=True)
            if (len(ciphertext) != item['size'] or len(ciphertext) != pin['cipher_bytes'] or
                    publisher.blob_id(ciphertext) != pin['cipher_oid'] or digest(ciphertext) != pin['cipher_sha256']):
                raise ValueError('READBACK_CIPHER_INTEGRITY')
            value = store._open(ciphertext, path)
            raw = AESGCM(store._key()).decrypt(ciphertext[3:15], ciphertext[15:], store._aad(path))
            metadata.append({**validate_record(value, raw, pin, expected), 'cipher_oid': pin['cipher_oid'], 'cipher_sha256': pin['cipher_sha256']})
            values.append(value); sealed, chunks = seal_transport(raw, pin['cipher_oid'], path, public_key)
            transports.append((pin['date'], chunks))
            metadata[-1]['transport'] = {'format': 'TCP1 RSA-OAEP-SHA256 + AES-256-GCM',
                'sealed_bytes': len(sealed), 'sealed_sha256': digest(sealed), 'chunks': len(chunks),
                'recipient_public_key_sha256': RECIPIENT_SHA}
        if values[0]['report']['profile'] != values[1]['report']['profile']:
            raise ValueError('READBACK_CROSS_DAY_PROFILE')
        public = expected['public_report']; rendered = public_report.markdown(values[1]['report']).encode()
        main = readonly('GET', '/git/ref/heads/main')['object']['sha']
        if (len(rendered) != public['bytes'] or digest(rendered) != public['sha256'] or publisher.blob_id(rendered) != public['git_blob_oid'] or
                publisher.read_file(public['path'], public['commit']) != rendered or publisher.read_file(public['path'], main) != rendered):
            raise ValueError('READBACK_PUBLIC_REPORT')
        after = readonly('GET', '/git/ref/heads/runtime-state-a92-inherited-v1')['object']['sha']
        legacy_after = readonly('GET', '/git/ref/heads/runtime-state')['object']['sha']
        if after != before or after != expected['state_commit'] or legacy_after != LEGACY or legacy_after != legacy_before:
            raise ValueError('READBACK_BRANCH_AFTER')
        proof = {'scope': 'two authenticated pinned records; GET only; no replay or business writes',
            'identity': profile.INHERITED, 'legacy_before': legacy_before, 'legacy_after': legacy_after,
            'state_before': before, 'state_after': after, 'records': metadata, 'public_report': {**public, 'verified_main': main},
            'cli_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), 'economic_values_printed': False, 'business_writes': False}
        print('INHERITED_EVIDENCE_METADATA ' + json.dumps(proof, sort_keys=True, ensure_ascii=False))
        for day, chunks in transports:
            for number, chunk in enumerate(chunks, 1): print(f'INHERITED_EVIDENCE_CHUNK {day} {number}/{len(chunks)} {chunk}')
    finally:
        publisher.api, store.api = original_api, store_api


if __name__ == '__main__':
    try:
        read_evidence()
    except Exception as exc:
        text = str(exc)
        reason = text if (re.fullmatch('[A-Z][A-Z_0-9:]{0,99}', text) and text.startswith(
            ('READBACK_', 'NON_READ_', 'STATE_', 'SAVED_', 'PUBLIC_', 'IMPORT_', 'INDEPENDENT_'))) else type(exc).__name__
        print('INHERITED_EVIDENCE_READ_FAILED reason=' + reason + '; no business state was changed')
        raise SystemExit(1) from None
