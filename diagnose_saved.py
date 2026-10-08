"""Read authenticated saved evidence; never decide, fetch quotes, or write state."""
from __future__ import annotations
import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

import private_store as store


def fingerprint_fields(text):
    fields = dict(part.split('=', 1) for part in text.split('|'))
    if len(text) > 500 or set(fields) - {'start', 'indicator', 'capital', 'warmup', 'deployment', 'admission', 'legacy_research_policy'}:
        raise ValueError('INVALID_SAVED_EVIDENCE')
    if fields.get('indicator') != 'warm' or fields.get('deployment') not in {'auto', 'trend', 'weak'}:
        raise ValueError('INVALID_SAVED_EVIDENCE')
    date.fromisoformat(fields['start'])
    if not 120 <= int(fields['warmup']) <= 10000 or not 0 < float(fields['capital']) < 1e100:
        raise ValueError('INVALID_SAVED_EVIDENCE')
    if fields.get('admission') not in (None, 'systemic') or fields.get('legacy_research_policy') not in (None, 'ordinary_systemic_admission'):
        raise ValueError('INVALID_SAVED_EVIDENCE')
    return fields


def inspect_saved(saved, day, references):
    bundle = store.decode_bundle(saved['bundle'], saved['sha256'])
    if (len(bundle) != saved['bytes'] or saved['report']['target_date'] != day or saved['status'] not in ('SUCCESS', 'DEGRADED')
            or saved['report']['status'] != saved['status'] or str(saved['report']['actions_run_id']) != saved['run_id']):
        raise ValueError('INVALID_SAVED_EVIDENCE')
    report = saved['report']
    fields = fingerprint_fields(report['profile']['config_fingerprint'])
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        members, total = {}, 0
        for member in archive:
            total += member.size
            if not member.isfile() or member.name in members or total > store.MAX_EXPANDED or len(members) >= 5000:
                raise ValueError('INVALID_SAVED_EVIDENCE')
            members[member.name] = member
        def read(name):
            member = members[name]
            if member.size > 2_000_000:
                raise ValueError('INVALID_SAVED_EVIDENCE')
            return archive.extractfile(member).read()
        native = json.loads(read('output/signals_' + day + '.json'))
        if native['scan_date'] != day or json.loads(read('output/daily_report.json')) != report:
            raise ValueError('INVALID_SAVED_EVIDENCE')
        root = 'output/snapshots/' + day + '/'
        manifest_raw = read(root + 'manifest.json')
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        if read(root + 'manifest.sha256').decode().strip() != manifest_sha:
            raise ValueError('INVALID_SAVED_EVIDENCE')
        manifest = json.loads(manifest_raw)
        if manifest['end_date'] != day or native['deployment']['snapshot_manifest_sha256'] != manifest_sha:
            raise ValueError('INVALID_SAVED_EVIDENCE')
        evidence = {item['path']: item for item in manifest['evidence']}
        coverage = []
        for code in references:
            relative = 'market_data/' + code + '.csv'
            if relative not in evidence and root + relative not in members:
                coverage.append({'symbol': code, 'present': False, 'first_date': None, 'last_date': None,
                    'pre_start_bars': None, 'required_days': native['warmup_health']['required_days'], 'warmup_short': None, 'csv_sha256': None})
                continue
            raw = read(root + relative)
            if hashlib.sha256(raw).hexdigest() != evidence[relative]['sha256'] or len(raw) != evidence[relative]['bytes']:
                raise ValueError('INVALID_SAVED_EVIDENCE')
            rows = list(csv.DictReader(io.StringIO(raw.decode())))
            days = [row['date'][:10] for row in rows]
            if days != sorted(set(days)) or any(date.fromisoformat(d).isoformat() != d or d > day for d in days):
                raise ValueError('INVALID_SAVED_EVIDENCE')
            before = [d for d in days if d < fields['start']]
            coverage.append({'symbol': code, 'present': True, 'first_date': min(days) if days else None,
                'last_date': max(days) if days else None, 'pre_start_bars': len(before),
                'required_days': native['warmup_health']['required_days'],
                'warmup_short': len(before) < native['warmup_health']['required_days'], 'csv_sha256': evidence[relative]['sha256']})
        state = json.loads(read('output/risk_state.json'))
    symbols = sorted(native['symbols'])
    if (any(not re.fullmatch(r'\d{6}', code) for code in symbols) or len(symbols) != 17
            or not re.fullmatch('[0-9a-f]{40}', report['strategy_sha'])
            or not re.fullmatch('[0-9a-f]{16}', state['symbols_hash'])):
        raise ValueError('INVALID_SAVED_EVIDENCE')
    metadata = {key: saved[key] for key in ('status', 'report', 'risk_state', 'sha256', 'bytes')}
    return {'report': report, 'fields': fields, 'symbols': symbols, 'state': state,
        'coverage': coverage, 'manifest_sha256': manifest_sha,
        'metadata_sha256': hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()}


def diagnose(source, day):
    sys.path.insert(0, str(source))
    from quantfusion.application.daily_context import ScanContext, ScanRequest
    from quantfusion.config.daily import SYMBOLS
    from quantfusion.config.overlay import RISK_BASKET
    from quantfusion.io.state_store import compute_identity_hash
    source_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    head, _, files = store._snapshot()
    target_path = store._path(day)
    target = store._read(target_path, files)
    prior_paths = sorted(p for p in files if re.fullmatch(r'runs/\d{4}-\d{2}-\d{2}\.json\.enc', p) and p < target_path)
    previous = next((record for path in reversed(prior_paths)
        if (record := store._read(path, files))['status'] in ('SUCCESS', 'DEGRADED') and record.get('risk_state') is not None), None)
    if previous is None or target is None or target['report']['strategy_sha'] != source_sha:
        raise ValueError('INVALID_SAVED_EVIDENCE')
    old = inspect_saved(previous, previous['report']['target_date'], RISK_BASKET)
    current = inspect_saved(target, day, RISK_BASKET)
    profile = current['report']['profile']
    ctx = ScanContext(ScanRequest(profile['start_date'], day, profile['capital'], '', '', '', '', 'auto', False), dict(SYMBOLS), {})
    if ctx.config_fingerprint != profile['config_fingerprint'] or old['state'] != previous['risk_state']:
        raise ValueError('INVALID_SAVED_EVIDENCE')
    comparison = {key: old['fields'].get(key) == current['fields'].get(key) for key in sorted(set(old['fields']) | set(current['fields']))}
    public_fields = lambda value: {key: item for key, item in value.items() if key != 'capital'}
    locks = lambda state: {key: state.get(key) if isinstance(state.get(key), bool) else None for key in ('terminal_risk_lock', 'sector_guard_active')}
    proof = {'target_date': day, 'source_sha': source_sha, 'state_commit': head,
        'previous_date': old['report']['target_date'], 'previous_source_sha': old['report']['strategy_sha'],
        'target_ciphertext_oid': files[target_path], 'previous_ciphertext_oid': files[store._path(old['report']['target_date'])],
        'target_bundle_sha256': target['sha256'], 'previous_bundle_sha256': previous['sha256'],
        'target_metadata_sha256': current['metadata_sha256'], 'previous_metadata_sha256': old['metadata_sha256'],
        'previous_config': public_fields(old['fields']), 'current_config': public_fields(current['fields']), 'config_fields_equal': comparison,
        'previous_symbols': old['symbols'], 'current_symbols': current['symbols'], 'source_symbols_match': sorted(SYMBOLS) == current['symbols'],
        'previous_symbols_hash': old['state'].get('symbols_hash'), 'previous_hash_matches_saved_config': old['state'].get('symbols_hash') == compute_identity_hash(dict.fromkeys(old['symbols']), old['report']['profile']['config_fingerprint']),
        'current_expected_symbols_hash': compute_identity_hash(dict.fromkeys(current['symbols']), ctx.config_fingerprint),
        'target_keeps_previous_risk_state': current['state'] == old['state'], 'target_saved_new_risk_state': target.get('risk_state') is not None,
        'previous_locks': locks(old['state']), 'target_preserved_locks': locks(current['state']),
        'target_manifest_sha256': current['manifest_sha256'], 'previous_manifest_sha256': old['manifest_sha256'],
        'risk_reference_history': current['coverage'], 'previous_risk_reference_history': old['coverage'], 'business_writes': False}
    proof['state_commit_after_read'] = store._snapshot()[0]
    proof['branch_advanced_during_read'] = proof['state_commit_after_read'] != head
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    try:
        store._path(args.date)
        print(json.dumps(diagnose(args.source, args.date), sort_keys=True, ensure_ascii=False, allow_nan=False))
    except Exception:
        print('SAVED_EVIDENCE_DIAGNOSTIC_FAILED; no business state was changed')
        raise SystemExit(1) from None
