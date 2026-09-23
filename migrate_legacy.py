"""One-time import of existing completed runs before switching away from legacy storage."""
from __future__ import annotations
import base64
from datetime import date, datetime, timedelta
import io
import json
import os
import tarfile
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import private_store as store

ENDPOINT = 'https://ykhdfyjbfdvayqmvaxgb.supabase.co/functions/v1/trade-daily'
FIRST_DAY = date(2026, 9, 22)


def legacy_result(day: str) -> dict | None:
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    url += ('&' if '?' in url else '?') + urllib.parse.urlencode({'audience': ENDPOINT})
    token_request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urllib.request.urlopen(token_request, timeout=60) as response:
        token = json.load(response)['value']
    request = urllib.request.Request(ENDPOINT + '/result', data=json.dumps({'date': day}).encode(), headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def risk_from_bundle(bundle: bytes) -> dict | None:
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        matches = [m for m in archive if m.name == 'output/risk_state.json']
        if not matches:
            return None
        if len(matches) != 1 or matches[0].size > 1_000_000 or not matches[0].isfile():
            raise ValueError('LEGACY_STATE_INVALID')
        return json.load(archive.extractfile(matches[0]))


def main() -> None:
    marker = 'migration/legacy-complete.json.enc'
    _, _, files = store._snapshot()
    if marker in files:
        if store._read(marker, files) != {'first_day': FIRST_DAY.isoformat(), 'completed': True}:
            raise ValueError('LEGACY_MARKER_INVALID')
        print('LEGACY_IMPORT_ALREADY_VERIFIED')
        return
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    if (today - FIRST_DAY).days >= 32:
        raise ValueError('LEGACY_IMPORT_WINDOW_EXPIRED')
    imported = 0
    found = 0
    for offset in range((today - FIRST_DAY).days + 1):
        day = (FIRST_DAY + timedelta(days=offset)).isoformat()
        saved = legacy_result(day)
        if not saved:
            continue
        found += 1
        if saved['status'] == 'RUNNING':
            raise ValueError('LEGACY_RUN_UNRESOLVED')
        bundle = store.decode_bundle(saved['bundle'], saved['sha256'])
        report = saved['report']
        if (saved['status'] not in ('SUCCESS', 'DEGRADED', 'FAILED') or
                report['target_date'] != day or str(report['actions_run_id']) != saved['run_id'] or
                len(bundle) != saved['bytes']):
            raise ValueError('LEGACY_RESULT_INVALID')
        risk = risk_from_bundle(bundle)
        if risk and risk.get('scan_date') != day:
            risk = None
        if saved['status'] == 'SUCCESS' and (not risk or risk.get('scan_date') != day):
            raise ValueError('LEGACY_RISK_STATE_MISSING')
        path = store._path(day)
        head, tree, files = store._snapshot()
        if path in files:
            existing = store._read(path, files)
            if existing['sha256'] != saved['sha256'] or existing['report'] != report:
                raise ValueError('LEGACY_IMPORT_CONFLICT')
            continue
        record = {'status': saved['status'], 'run_id': saved['run_id'],
                  'strategy_sha': report['strategy_sha'], 'previous_date': report.get('previous_trading_date'),
                  'report': report, 'markdown': '', 'risk_state': risk, 'bundle': saved['bundle'],
                  'sha256': saved['sha256'], 'bytes': saved['bytes']}
        store._commit(path, record, head, tree, files)
        imported += 1
    if not found:
        raise ValueError('NO_LEGACY_RESULT_TO_IMPORT')
    head, tree, files = store._snapshot()
    store._commit(marker, {'first_day': FIRST_DAY.isoformat(), 'completed': True}, head, tree, files)
    print('LEGACY_RESULT_IMPORT_VERIFIED:', imported)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('BLOCKED: LEGACY_IMPORT_FAILED')
        raise SystemExit(1) from None
