"""Authenticate one immutable old paper record; emit recipient-sealed evidence."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import struct
import subprocess
import tarfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import private_store as store

COMMIT = '9cbb3d2a7648715422ff41739de621e9d4c12b5f'
OID = '6861c312e010b755f244ecab804bffaa3ce31a7f'
CIPHER_SHA = 'd012b6ed01564f74145c0ced2129aad4e4efac980e5483be3c95518dcff0eb99'
CIPHER_BYTES = 620006
RECIPIENT_SHA = '668be6f304597fb7cf8e9ab3ca79da3b340db5273d6e10e8caa91f9c0c8ae284'
RECORD = 'runs/2026-09-30.json.enc'
SOURCE = 'd2fee61a7f91679f6fdabaf97ba68cd030ad290a'
BUNDLE_SHA = 'e9aa0cd6da700515f19e4e71d9e19ef645b8447ab692fe30ea6fc9115d0e8b0f'
AAD = ('trade-checkpoint-evidence-v1|' + OID + '|' + RECORD).encode()


def read_evidence():
    original_api = store.api
    def readonly(method, path, *args, **kwargs):
        if method != 'GET':
            raise ValueError('NON_READ_OPERATION_FORBIDDEN')
        return original_api(method, path, *args, **kwargs)
    store.api = readonly
    before = store._snapshot()[0]
    commit = readonly('GET', '/git/commits/' + COMMIT)
    tree = readonly('GET', '/git/trees/' + commit['tree']['sha'] + '?recursive=1')
    if tree.get('truncated'):
        raise ValueError('TRUNCATED_TREE')
    files = {v['path']: v['sha'] for v in tree['tree'] if v['type'] == 'blob'}
    if files.get(RECORD) != OID:
        raise ValueError('OLD_RECORD_IDENTITY')
    item = readonly('GET', '/git/blobs/' + OID, limit=18_000_000)
    ciphertext = base64.b64decode(''.join(item['content'].split()), validate=True)
    if (len(ciphertext) != item['size'] or len(ciphertext) != CIPHER_BYTES or store.blob_id(ciphertext) != OID
            or hashlib.sha256(ciphertext).hexdigest() != CIPHER_SHA):
        raise ValueError('CIPHERTEXT_INTEGRITY')
    value = store._open(ciphertext, RECORD)
    # Preserve the exact authenticated old serialized record, not a reserialization.
    raw = AESGCM(store._key()).decrypt(ciphertext[3:15], ciphertext[15:], RECORD.encode())
    if json.loads(raw) != value or value['run_id'] != '36724655406':
        raise ValueError('OLD_RUN_IDENTITY')
    if value['report']['strategy_sha'] != SOURCE or value['report']['target_date'] != '2026-09-30':
        raise ValueError('OLD_PRODUCER_IDENTITY')
    bundle = store.decode_bundle(value['bundle'], BUNDLE_SHA)
    if value['sha256'] != BUNDLE_SHA or value['bytes'] != len(bundle):
        raise ValueError('ORIGINAL_BUNDLE_INTEGRITY')
    members, inventory, total = {}, [], 0
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        for m in archive:
            name = PurePosixPath(m.name)
            total += m.size
            if (not m.isfile() or name.is_absolute() or '..' in name.parts or m.name in members
                    or total > store.MAX_EXPANDED or len(members) >= 5000):
                raise ValueError('UNSAFE_OLD_BUNDLE')
            data = archive.extractfile(m).read(m.size + 1)
            if len(data) != m.size:
                raise ValueError('OLD_MEMBER_LENGTH')
            members[m.name] = data
            public_path = (m.name in {'output/risk_state.json','output/latest_success.json','output/daily_report.json',
                'output/daily_report.md','output/signals_2026-09-30.json','logs/production.log'}
                or re.fullmatch(r'(cache|regime)/[0-9]{6}\.csv', m.name)
                or re.fullmatch(r'output/snapshots/2026-09-30/(manifest\.(json|sha256)|(market_data|regime_data)/[0-9]{6}\.csv)', m.name))
            inventory.append({('path' if public_path else 'private_path_sha256'):
                m.name if public_path else hashlib.sha256(m.name.encode()).hexdigest(),
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    risk = json.loads(members['output/risk_state.json'])
    if risk != value['risk_state'] or json.loads(members['output/daily_report.json']) != value['report']:
        raise ValueError('OLD_REPORT_STATE_BINDING')
    manifest_path = 'output/snapshots/2026-09-30/manifest.json'
    manifest_raw = members[manifest_path]
    manifest = json.loads(manifest_raw)
    prefix = 'output/snapshots/2026-09-30/'
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    if (manifest['end_date'] != '2026-09-30'
            or members[prefix + 'manifest.sha256'].decode().strip() != manifest_sha):
        raise ValueError('OLD_MANIFEST_IDENTITY')
    symbols = manifest['symbols']
    if (not symbols or len(symbols) != len(set(symbols)) or any(not re.fullmatch('[0-9]{6}', x) for x in symbols)):
        raise ValueError('OLD_MANIFEST_SYMBOLS')
    required = {f'market_data/{x}.csv' for x in symbols} | {'regime_data/000300.csv','regime_data/000682.csv'}
    declared = set()
    for entry in manifest['evidence']:
        if entry['path'] not in required or entry['path'] in declared:
            raise ValueError('OLD_MANIFEST_PATH')
        declared.add(entry['path'])
        data = members[prefix + entry['path']]
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('OLD_INPUT_INTEGRITY')
    actual = {name[len(prefix):] for name in members if name.startswith(prefix)}
    if declared != required or actual != declared | {'manifest.json','manifest.sha256'}:
        raise ValueError('OLD_SNAPSHOT_FILE_SET')
    native = json.loads(members['output/signals_2026-09-30.json'])
    if (manifest.get('deployment_policy') != 'production_daily_replay'
            or native['scan_date'] != '2026-09-30' or risk['scan_date'] != native['scan_date']
            or risk['run_id'] != native['run_id'] or not native['risk_state_saved']
            or native['deployment']['snapshot_manifest_sha256'] != manifest_sha):
        raise ValueError('OLD_NATIVE_RECORD_BINDING')
    after = store._snapshot()[0]
    metadata = {'evidence_scope': 'authenticated original old record; no replay or state writes',
        'state_before': before, 'state_after': after, 'pinned_state_commit': COMMIT,
        'record_path': RECORD, 'ciphertext_oid': OID, 'ciphertext_sha256': hashlib.sha256(ciphertext).hexdigest(),
        'original_plaintext_bytes': len(raw), 'original_plaintext_sha256': hashlib.sha256(raw).hexdigest(),
        'bundle_sha256': BUNDLE_SHA, 'bundle_bytes': len(bundle),
        'risk_state_schema_version': risk.get('schema_version'), 'risk_state_fields': sorted(k for k in risk if k in {
            'schema_version','run_id','scan_date','terminal_risk_lock','sector_guard_active','cycle_lock_count',
            'max_drawdown','total_return','final_assets','symbols_hash','total_symbols','checkpoint'}),
        'risk_state_field_types': {k: type(risk[k]).__name__ for k in risk if k in {
            'schema_version','run_id','scan_date','terminal_risk_lock','sector_guard_active','cycle_lock_count',
            'max_drawdown','total_return','final_assets','symbols_hash','total_symbols','checkpoint'}},
        'risk_state_total_field_count': len(risk),
        'risk_state_checkpoint_present': 'checkpoint' in risk,
        'member_inventory': inventory, 'cli_sha': subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        'economic_values_printed': False, 'business_writes': False}
    public_bytes = Path('evidence_recipient.pub').read_bytes()
    public_key = serialization.load_pem_public_key(public_bytes)
    if hashlib.sha256(public_bytes).hexdigest() != RECIPIENT_SHA or not isinstance(public_key, RSAPublicKey) or public_key.key_size < 2048:
        raise ValueError('RECIPIENT_IDENTITY')
    key, nonce = os.urandom(32), os.urandom(12)
    wrapped = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=AAD))
    sealed = b'TCP1' + struct.pack('>H', len(wrapped)) + wrapped + nonce + AESGCM(key).encrypt(nonce, raw, AAD)
    encoded = base64.b64encode(sealed).decode()
    chunks = [encoded[i:i+12000] for i in range(0, len(encoded), 12000)]
    metadata['transport'] = {'format': 'TCP1 RSA-OAEP-SHA256 + AES-256-GCM', 'sealed_bytes': len(sealed),
        'sealed_sha256': hashlib.sha256(sealed).hexdigest(), 'chunks': len(chunks),
        'recipient_public_key_sha256': hashlib.sha256(Path('evidence_recipient.pub').read_bytes()).hexdigest()}
    print('CHECKPOINT_EVIDENCE_METADATA ' + json.dumps(metadata, sort_keys=True, ensure_ascii=False))
    for number, chunk in enumerate(chunks, 1):
        print(f'CHECKPOINT_EVIDENCE_CHUNK {number}/{len(chunks)} {chunk}')


if __name__ == '__main__':
    try:
        read_evidence()
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_]+', str(exc)) else type(exc).__name__
        print('CHECKPOINT_EVIDENCE_READ_FAILED reason=' + reason + '; no business state was changed')
        raise SystemExit(1) from None
