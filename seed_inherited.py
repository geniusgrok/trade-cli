"""Import only the reviewed reconstruction into its independent paper branch."""
import base64
from datetime import datetime
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
from zoneinfo import ZoneInfo

from checkpoint_transfer import open_import
import private_store as store
import publish_report as publisher
import report as reporting
import runtime_profile as profile
import seed_contract

BASELINE = '2026-09-30'


def extract_snapshot(data: bytes, destination: Path, day: str, provenance: dict) -> Path:
    seed_contract.require_evidence_provenance(provenance)
    prefix = f'output/snapshots/{day}/'
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        names, total = set(), 0
        for item in archive.getmembers():
            path = PurePosixPath(item.name)
            total += item.size
            if (not item.isfile() or path.is_absolute() or '..' in path.parts or
                    item.name in names or total > store.MAX_EXPANDED):
                raise ValueError('UNSAFE_IMPORT_BUNDLE')
            names.add(item.name)
        if provenance['kind'] == 'provider_full_history_recovery':
            try:
                receipt = archive.extractfile('evidence_origin.json').read()
            except (KeyError, AttributeError) as exc:
                raise ValueError('IMPORT_RECOVERY_RECEIPT_REQUIRED') from exc
            if store.digest(receipt) != provenance['recovery_receipt_sha256']:
                raise ValueError('IMPORT_RECOVERY_RECEIPT_IDENTITY')
            facts = seed_contract.strict_json(receipt)
            if (facts.get('nature') != 'posttask_provider_full_history_recovery_not_historical_receipt' or
                    facts.get('producer_source') != profile.SOURCE_SHA or facts.get('old_observed_close') != BASELINE or
                    facts.get('target_close') != day or facts.get('original_current_bundle_sha256') != provenance['original_bundle_sha256'] or
                    facts.get('same_market_no_code_mapping_no_history_splice') is not True or
                    facts.get('strict_old_native_prefixes_exact') is not True or
                    facts.get('new_native_snapshot_manifest_sha256') != store.digest(archive.extractfile(prefix + 'manifest.json').read())):
                raise ValueError('IMPORT_RECOVERY_RECEIPT_IDENTITY')
        for item in archive.getmembers():
            if not item.name.startswith(prefix):
                continue
            target = destination / item.name[len(prefix):]
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('UNSAFE_IMPORT_BUNDLE')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(item) as source:
                target.write_bytes(source.read())
    return destination


def validate_native_seed(root: Path, evidence: Path, payload: dict, manifest: dict) -> tuple[dict, dict]:
    source = profile.source_path()
    profile.require_source(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip())
    sys.path.insert(0, str(source))
    from quantfusion.application.daily_context import ScanContext, ScanRequest
    from quantfusion.application.daily_scan import build_argument_parser
    from quantfusion.config import universe
    from quantfusion.data.sessions import resolve_scan_dates
    from quantfusion.data.snapshot import verify_frozen_snapshot, sha256_file
    from quantfusion.io.state_store import validate_risk_state, validate_checkpoint_identity, require_completed_publication, checkpoint_prefix_hashes

    output = root / 'output'
    state = seed_contract.strict_json((output / 'risk_state.json').read_bytes())
    native = seed_contract.strict_json((output / f'signals_{BASELINE}.json').read_bytes())
    request = payload['seed_profile']
    symbols = dict(universe.SYMBOL_NAMES)
    if request['universe'] != [[code, name] for code, name in symbols.items()] or native.get('symbols') != symbols:
        raise ValueError('IMPORT_CORE17_IDENTITY')
    args = build_argument_parser().parse_args(['--resume', '--start-date', request['start_date'], '--end-date', BASELINE,
        '--capital', str(request['capital']), '--cache-dir', str(root / 'cache'), '--regime-data-dir', str(root / 'regime'),
        '--output-dir', str(output)])
    ctx = ScanContext(ScanRequest.from_args(args, request['start_date'], BASELINE, args.capital), symbols, resolve_scan_dates(BASELINE))
    ctx.tradable = symbols
    ctx.snapshot_dir = output / 'snapshots' / BASELINE
    if (validate_risk_state(state) is not None or state.get('schema_version') != 2 or
            state['checkpoint']['state']['initial_capital'] != request['capital'] or
            ctx.config_fingerprint != request['config_fingerprint'] or native.get('status') != 'ok' or native.get('mode') != 'simulation'):
        raise ValueError('IMPORT_NATIVE_STATE')
    verify_frozen_snapshot(ctx.snapshot_dir)
    verify_frozen_snapshot(evidence)
    require_completed_publication(str(output), state, manifest['evidence_date'])
    validate_checkpoint_identity(ctx, state['checkpoint'])
    declared = state['checkpoint']['identity']
    identity = {name: declared[name] for name in manifest['native_identity']}
    if identity != manifest['native_identity']:
        raise ValueError('IMPORT_NATIVE_IDENTITY')
    paths = {'seed_state_sha256': output / 'risk_state.json', 'seed_artifact_sha256': output / f'signals_{BASELINE}.json',
             'seed_snapshot_manifest_sha256': ctx.snapshot_dir / 'manifest.json',
             'evidence_snapshot_manifest_sha256': evidence / 'manifest.json'}
    if any(sha256_file(path) != manifest[name] for name, path in paths.items()):
        raise ValueError('IMPORT_NATIVE_FILE_IDENTITY')
    if checkpoint_prefix_hashes(evidence, BASELINE) != state['checkpoint']['prefix_hashes']:
        raise ValueError('IMPORT_OBSERVED_PREFIX_CHANGED')
    return state, native


def verify_existing(record: dict, expected: dict) -> None:
    if not isinstance(record, dict) or record.get('status') not in ('SUCCESS', 'DEGRADED'):
        raise ValueError('EXISTING_INDEPENDENT_SEED_UNUSABLE')
    profile.require_record(record)
    for key in ('seed_import', 'state_origin', 'risk_state', 'strategy_sha', 'simulation_identity', 'status'):
        if record.get(key) != expected.get(key):
            raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')
    for key in ('profile', 'observation', 'continuation', 'state_origin'):
        if record['report'].get(key) != expected['report'].get(key):
            raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')
    bundle = store.decode_bundle(record['bundle'], record['sha256'])
    publisher.verified_report(record, BASELINE, bundle)
    if record.get('markdown') != reporting.markdown(record['report']):
        raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store.restore(bundle, root, complete=True)
        if seed_contract.strict_json((root / 'output/risk_state.json').read_bytes()) != expected['risk_state']:
            raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')
        if store.digest((root / 'output/risk_state.json').read_bytes()) != expected['seed_import']['seed_state_sha256']:
            raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')
        if store.digest((root / f'output/signals_{BASELINE}.json').read_bytes()) != expected['seed_import']['seed_artifact_sha256']:
            raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')
        if store.digest((root / 'output/snapshots' / BASELINE / 'manifest.json').read_bytes()) != expected['seed_import']['seed_snapshot_manifest_sha256']:
            raise ValueError('EXISTING_INDEPENDENT_SEED_DIFFERS')


def initialize(record: dict) -> None:
    if not profile.independent():
        raise ValueError('EXPLICIT_INDEPENDENT_IDENTITY_REQUIRED')
    profile.require_record(record)
    branch = profile.state_branch()
    path = store._path(BASELINE)
    if store.api('GET', f'/git/ref/heads/{branch}') is not None:
        files = store._snapshot()[2]
        verify_existing(store._read(path, files), record)
        for saved_path in files:
            if re.fullmatch(r'runs/\d{4}-\d{2}-\d{2}\.json\.enc', saved_path) and saved_path != path:
                later = store._read(saved_path, files)
                profile.require_record(later)
                if later.get('status') not in ('SUCCESS', 'DEGRADED', 'RUNNING', 'FAILED'):
                    raise ValueError('EXISTING_INDEPENDENT_LINEAGE_UNUSABLE')
        return
    sealed = store._seal(record, path)
    if len(sealed) > 12_000_000:
        raise ValueError('STATE_FILE_TOO_LARGE')
    blob = store.api('POST', '/git/blobs', {'content': base64.b64encode(sealed).decode(), 'encoding': 'base64'})
    if blob['sha'] != publisher.blob_id(sealed):
        raise ValueError('STATE_BLOB_RECEIPT')
    tree = store.api('POST', '/git/trees', {'tree': [{'path': path, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
    commit = store.api('POST', '/git/commits', {'message': '导入已核验的独立完整收盘种子', 'tree': tree['sha'], 'parents': []})
    try:
        store.api('POST', '/git/refs', {'ref': f'refs/heads/{branch}', 'sha': commit['sha']})
    except RuntimeError:
        # A concurrent or uncertain create is authenticated before any retry.
        pass
    _, _, files = store._snapshot()
    verify_existing(store._read(path, files), record)


def main() -> None:
    if not profile.independent():
        raise ValueError('EXPLICIT_INDEPENDENT_IDENTITY_REQUIRED')
    checkout = Path(__file__).parent
    approved = (checkout / 'approved_seed.json').read_bytes()
    cipher = (checkout / 'approved_seed.enc').read_bytes()
    integrity = seed_contract.strict_json((checkout / 'integrity.json').read_bytes())
    for name, raw in (('approved_seed.json', approved), ('approved_seed.enc', cipher)):
        expected = {'bytes': len(raw), 'sha256': store.digest(raw), 'git_blob': publisher.blob_id(raw)}
        if integrity.get(name) != expected:
            raise ValueError('IMPORT_REVIEWED_CHECKOUT_REQUIRED')
    manifest = seed_contract.strict_json(approved)
    seed_contract.validate_manifest(manifest, cipher)
    plaintext = open_import(cipher, store._key(), manifest['plaintext_sha256'])
    payload = seed_contract.validate(manifest, plaintext, cipher)
    if os.environ['TARGET_DATE'] != payload['evidence_date']:
        raise ValueError('IMPORT_EVIDENCE_DATE')
    root = Path(os.environ['RUNNER_TEMP']) / 'trade-inherited-seed'
    store.restore(store.decode_bundle(payload['seed_bundle'], payload['seed_bundle_sha256']), root, complete=True)
    evidence = Path(os.environ['RUNNER_TEMP']) / 'trade-inherited-evidence'
    extract_snapshot(store.decode_bundle(payload['evidence_bundle'], payload['evidence_bundle_sha256']), evidence, payload['evidence_date'], payload['evidence_provenance'])
    state, native = validate_native_seed(root, evidence, payload, manifest)
    (evidence / 'provenance.json').write_text(json.dumps(payload['evidence_provenance'], sort_keys=True))
    from runner import identity
    report = {**identity(profile.source_path()), 'target_date': BASELINE, 'data_date': BASELINE,
              'state_origin': profile.STATE_ORIGIN.copy(), 'profile': payload['seed_profile'],
              'continuation': {'mode': 'native_full_checkpoint_seed', 'previous_close': BASELINE},
              'comparison': {'status': '不可比较', 'reason': '事后重建基线；没有前一日独立序列结果', 'changes': []},
              'observation': reporting.observation(native, dict(payload['seed_profile']['universe']), {}),
              'phase': 'INDEPENDENT_SEED_IMPORTED', 'finished_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
    health = native.get('warmup_health', {}).get('warmup_status')
    if health not in ('READY', 'DEGRADED'):
        raise ValueError('IMPORT_WARMUP_INVALID')
    flags = native.get('summary', {})
    report['status'] = 'DEGRADED' if health == 'DEGRADED' or flags.get('buys_suppressed') else 'SUCCESS'
    markdown = reporting.markdown(report)
    (root / 'output/daily_report.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False))
    (root / 'output/daily_report.md').write_text(markdown)
    bundle = store.pack(root)
    record = {'status': report['status'], 'run_id': str(report['actions_run_id']), 'strategy_sha': profile.SOURCE_SHA,
              'simulation_identity': profile.INHERITED, 'state_origin': profile.STATE_ORIGIN.copy(), 'previous_date': None,
              'report': report, 'markdown': markdown, 'risk_state': state, 'bundle': base64.b64encode(bundle).decode(),
              'sha256': store.digest(bundle), 'bytes': len(bundle),
              'seed_import': {'manifest_sha256': store.digest(approved), 'plaintext_sha256': manifest['plaintext_sha256'],
                              'seed_bundle_sha256': manifest['seed_bundle_sha256'], 'seed_artifact_sha256': manifest['seed_artifact_sha256']}}
    record['seed_import']['seed_snapshot_manifest_sha256'] = manifest['seed_snapshot_manifest_sha256']
    record['seed_import']['seed_state_sha256'] = manifest['seed_state_sha256']
    initialize(record)
    print('INDEPENDENT_SEED_AND_SAVED_EVIDENCE_VERIFIED')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('BLOCKED: INDEPENDENT_SEED_IMPORT_FAILED')
        reason = str(exc)
        if not re.fullmatch(r'(?:IMPORT|INDEPENDENT|STATE|PUBLIC|EXPLICIT|EXISTING|UNSAFE|UNKNOWN)_[A-Z_0-9:]{1,80}', reason):
            reason = type(exc).__name__
        print('REASON_CODE: ' + reason)
        raise SystemExit(1) from None
