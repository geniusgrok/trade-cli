"""Encrypted daily state and audit on the repository's independent state branch."""
from __future__ import annotations

import base64
from datetime import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from publish_report import api, blob_id

BRANCH = 'runtime-state'
MAX_BUNDLE = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key() -> bytes:
    # The existing Actions read secret remains outside the public repository.
    secret = os.environ['STATE_SEAL_KEY']
    if len(secret) < 32:
        raise ValueError('STATE_KEY_MISSING_OR_SHORT')
    return hashlib.sha256(b'trade-cli runtime-state v1\0' + secret.encode()).digest()


def _seal(value: dict, path: str) -> bytes:
    nonce = os.urandom(12)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    return b'TD1' + nonce + AESGCM(_key()).encrypt(nonce, raw, path.encode())


def _open(data: bytes, path: str) -> dict:
    if not data.startswith(b'TD1') or len(data) < 32:
        raise ValueError('STATE_ENVELOPE_INVALID')
    try:
        value = json.loads(AESGCM(_key()).decrypt(data[3:15], data[15:], path.encode()))
    except (ValueError, KeyError, InvalidTag) as exc:
        raise ValueError('STATE_DECRYPT_FAILED') from exc
    if not isinstance(value, dict):
        raise ValueError('STATE_DOCUMENT_INVALID')
    return value


def _snapshot() -> tuple[str, str, dict[str, str]]:
    ref = api('GET', f'/git/ref/heads/{BRANCH}')
    if ref is None:
        raise ValueError('STATE_BRANCH_MISSING')
    head = ref['object']['sha']
    commit = api('GET', '/git/commits/' + head)
    tree = commit['tree']['sha']
    listing = api('GET', '/git/trees/' + tree + '?recursive=1')
    if listing.get('truncated'):
        raise ValueError('STATE_TREE_TRUNCATED')
    files = {x['path']: x['sha'] for x in listing['tree'] if x['type'] == 'blob'}
    return head, tree, files


def _read(path: str, files: dict[str, str]) -> dict | None:
    sha = files.get(path)
    if sha is None:
        return None
    item = api('GET', '/git/blobs/' + sha, limit=18_000_000)
    if item['encoding'] != 'base64':
        raise ValueError('STATE_BLOB_ENCODING')
    data = base64.b64decode(''.join(item['content'].split()), validate=True)
    if blob_id(data) != sha or len(data) != item['size']:
        raise ValueError('STATE_BLOB_INTEGRITY')
    return _open(data, path)


def _commit(path: str, value: dict, head: str, tree: str, files: dict[str, str]) -> None:
    data = _seal(value, path)
    if len(data) > 12_000_000:
        raise ValueError('STATE_FILE_TOO_LARGE')
    created = api('POST', '/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'}, limit=1_000_000)
    if created['sha'] != blob_id(data):
        raise ValueError('STATE_BLOB_RECEIPT')
    updated = api('POST', '/git/trees', {'base_tree': tree, 'tree': [
        {'path': path, 'mode': '100644', 'type': 'blob', 'sha': created['sha']}]})
    commit = api('POST', '/git/commits', {'message': '保存加密运行状态', 'tree': updated['sha'], 'parents': [head]})
    try:
        api('PATCH', f'/git/refs/heads/{BRANCH}', {'sha': commit['sha'], 'force': False})
    except RuntimeError:
        # A rejected or ambiguous ref update must be resolved by the caller's readback.
        raise RuntimeError('STATE_REF_UPDATE_UNCERTAIN') from None
    current = _snapshot()
    if current[0] != commit['sha'] or current[2].get(path) != created['sha'] or _read(path, current[2]) != value:
        raise ValueError('STATE_READBACK_MISMATCH')


def _path(day: str) -> str:
    if not DATE.fullmatch(day) or datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
        raise ValueError('INVALID_STATE_DATE')
    return f'runs/{day}.json.enc'


def request(route: str, payload: dict) -> dict:
    day = payload['date']
    path = _path(day)
    head, tree, files = _snapshot()
    current = _read(path, files)
    if route == 'result':
        return current or {}
    if route == 'context':
        earlier = sorted(p for p in files if re.fullmatch(r'runs/\d{4}-\d{2}-\d{2}\.json\.enc', p) and p < path)
        previous = None
        for candidate in reversed(earlier):
            record = _read(candidate, files)
            if record['status'] in ('SUCCESS', 'DEGRADED') and record.get('risk_state') is not None:
                previous = {'date': candidate[5:15], 'strategy_sha': record['report']['strategy_sha'],
                            'risk_state': record['risk_state'], 'bundle': record['bundle'],
                            'bundle_sha256': record['sha256'], 'profile': record['report'].get('profile', {})}
                break
        compare = _read(_path(payload['compare_date']), files) if payload.get('compare_date') else None
        return {'existing': current and {k: current[k] for k in ('status', 'run_id', 'strategy_sha')},
                'previous': previous, 'comparison': compare['report'] if compare and compare['status'] in ('SUCCESS', 'DEGRADED') else None}
    if route == 'start':
        if current:
            return {'started': False, 'status': current['status'], 'run_id': current['run_id']}
        runs = [p for p in files if re.fullmatch(r'runs/\d{4}-\d{2}-\d{2}\.json\.enc', p)]
        if any(p > path for p in runs):
            raise ValueError('OUT_OF_ORDER_DATE')
        if runs:
            last = _read(max(runs), files)
            if last['status'] in ('RUNNING', 'FAILED'):
                raise ValueError('PREVIOUS_RUN_UNRESOLVED')
        prior = request('context', {'date': day, 'compare_date': None})['previous']
        if (prior or {}).get('date') != payload.get('previous_date'):
            raise ValueError('STATE_CHANGED')
        record = {'status': 'RUNNING', 'run_id': os.environ['GITHUB_RUN_ID'],
                  'strategy_sha': payload['strategy_sha'], 'previous_date': payload.get('previous_date')}
        try:
            _commit(path, record, head, tree, files)
        except (RuntimeError, ValueError):
            if _read(path, _snapshot()[2]) != record:
                raise
        return {'started': True}
    if route == 'finish':
        report = payload['report']
        status = payload['status']
        if not current or current['run_id'] != str(report['actions_run_id']):
            raise ValueError('RESERVATION_REQUIRED')
        if status not in ('SUCCESS', 'DEGRADED', 'FAILED') or report['target_date'] != day or report['status'] != status or report['strategy_sha'] != current['strategy_sha']:
            raise ValueError('REPORT_IDENTITY')
        if status == 'SUCCESS' and payload['risk_state'] is None:
            raise ValueError('STATE_REQUIRED')
        if payload['risk_state'] is not None and payload['risk_state'].get('scan_date') != day:
            raise ValueError('STATE_IDENTITY')
        if status == 'FAILED' and payload['risk_state'] is not None:
            raise ValueError('FAILED_STATE_NOT_PUBLISHABLE')
        decode_bundle(payload['bundle'], payload['sha256'])
        record = {**current, 'status': status, 'report': report, 'markdown': payload['markdown'],
                  'risk_state': payload['risk_state'], 'bundle': payload['bundle'],
                  'sha256': payload['sha256'], 'bytes': len(base64.b64decode(payload['bundle']))}
        if current['status'] != 'RUNNING':
            if current != record:
                raise ValueError('IMMUTABLE_RESULT')
            return {'saved': True, 'sha256': record['sha256']}
        try:
            _commit(path, record, head, tree, files)
        except (RuntimeError, ValueError):
            if _read(path, _snapshot()[2]) != record:
                raise
        return {'saved': True, 'sha256': record['sha256']}
    if route == 'event':
        status = payload['status']
        if status not in {'HOLIDAY', 'BEFORE_CLOSE', 'PREPARATION_FAILED', 'DELIVERY_FAILED', 'VERIFIED'}:
            raise ValueError('INVALID_EVENT')
        audit = {'date': day, 'status': status, 'details': payload['details'],
                 'run_id': os.environ['GITHUB_RUN_ID'], 'attempt': int(os.environ['GITHUB_RUN_ATTEMPT'])}
        event_path = f"events/{day}/{audit['run_id']}-{audit['attempt']}-{status}.json.enc"
        prior = _read(event_path, files)
        if prior is not None:
            if prior != audit:
                raise ValueError('IMMUTABLE_EVENT')
            return {'saved': True}
        try:
            _commit(event_path, audit, head, tree, files)
        except (RuntimeError, ValueError):
            if _read(event_path, _snapshot()[2]) != audit:
                raise
        return {'saved': True}
    raise ValueError('UNKNOWN_STORE_ROUTE')


def pack(root: Path) -> bytes:
    """Retain exact runtime files, not private source, environment or Git data."""
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode='wb', mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w') as archive:
            for path in sorted(root.rglob('*')):
                relative = path.relative_to(root)
                if relative.parts[0] not in {'cache', 'regime', 'output', 'logs'}:
                    continue
                if path.is_symlink():
                    raise ValueError('RUNTIME_SYMLINK')
                if not path.is_file():
                    continue
                info = tarfile.TarInfo(relative.as_posix())
                info.size = path.stat().st_size
                info.mode = 0o600
                with path.open('rb') as source:
                    archive.addfile(info, source)
    data = out.getvalue()
    if len(data) > MAX_BUNDLE:
        raise ValueError('BUNDLE_LIMIT')
    return data


def decode_bundle(encoded: str, expected: str) -> bytes:
    data = base64.b64decode(''.join(encoded.split()), validate=True)
    if len(data) > MAX_BUNDLE or digest(data) != expected:
        raise ValueError('BUNDLE_INTEGRITY')
    return data


def restore(data: bytes, root: Path) -> None:
    """Restore caches and native state only; old reports never become today's."""
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        seen = set()
        for info in archive:
            path = PurePosixPath(info.name)
            total += info.size
            if (not info.isfile() or path.is_absolute() or '..' in path.parts or
                    info.name in seen or total > MAX_EXPANDED):
                raise ValueError('UNSAFE_BUNDLE')
            seen.add(info.name)
            if not (path.parts[0] in {'cache', 'regime'} or info.name == 'output/risk_state.json'):
                continue
            target = root.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root.resolve()):
                raise ValueError('UNSAFE_BUNDLE')
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(info)
            if source is None:
                raise ValueError('MISSING_BUNDLE_MEMBER')
            value = source.read(info.size + 1)
            if len(value) != info.size:
                raise ValueError('BUNDLE_LENGTH')
            target.write_bytes(value)


def finish(date: str, status: str, report: dict, markdown: str,
           risk_state: dict | None, root: Path) -> dict:
    bundle = pack(root)
    expected = digest(bundle)
    payload = {'date': date, 'status': status, 'report': report, 'markdown': markdown,
               'risk_state': risk_state, 'bundle': base64.b64encode(bundle).decode(), 'sha256': expected}
    try:
        receipt = request('finish', payload)
    except RuntimeError:
        saved = request('result', {'date': date})
        if saved.get('sha256') != expected or saved.get('report') != report:
            raise
        receipt = {'saved': True}
    saved = request('result', {'date': date})
    if not receipt.get('saved') or saved.get('run_id') != str(report['actions_run_id']):
        raise ValueError('PERSISTENCE_NOT_VERIFIED')
    remote = decode_bundle(saved['bundle'], saved['sha256'])
    if remote != bundle or saved.get('report') != report or saved.get('status') != status:
        raise ValueError('READBACK_MISMATCH')
    return {'sha256': expected, 'bytes': len(bundle)}
