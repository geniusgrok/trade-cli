"""OIDC-authenticated transport. Never print tokens, HTTP bodies or reports."""
from __future__ import annotations
import base64
import gzip
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import tarfile
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = 'https://ykhdfyjbfdvayqmvaxgb.supabase.co/functions/v1/trade-daily'
MAX_BUNDLE = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read(13_000_001)
        if len(raw) > 13_000_000:
            raise RuntimeError('RESPONSE_TOO_LARGE')
        result = json.loads(raw)
        if result is not None and not isinstance(result, dict):
            raise RuntimeError('INVALID_STORE_RESPONSE')
        return result
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(1024))
            code = body.get('error', '')
        except (ValueError, AttributeError):
            code = ''
        safe = code if isinstance(code, str) and re.fullmatch('[A-Z_]{3,60}', code) else 'REJECTED'
        raise RuntimeError(f'PRIVATE_HTTP_{exc.code}_{safe}') from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError('PRIVATE_TRANSPORT_FAILED') from exc


def request(route: str, payload: dict) -> dict:
    token_url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    token_url += ('&' if '?' in token_url else '?') + urllib.parse.urlencode({'audience': ENDPOINT})
    token = _read_json(urllib.request.Request(token_url, headers={
        'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}))['value']
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    return _read_json(urllib.request.Request(ENDPOINT + '/' + route, data=data, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}))


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
    # A transport failure may follow a committed transaction. Inspect before retry.
    try:
        receipt = request('finish', payload)
    except RuntimeError:
        existing = request('result', {'date': date})
        if existing and existing.get('sha256') == expected:
            receipt = {'saved': True, 'sha256': expected}
        elif existing and existing.get('status') == 'RUNNING':
            receipt = request('finish', payload)
        else:
            raise
    saved = request('result', {'date': date})
    if not receipt.get('saved') or not saved or saved.get('run_id') != report['actions_run_id']:
        raise ValueError('PERSISTENCE_NOT_VERIFIED')
    remote = decode_bundle(saved['bundle'], saved['sha256'])
    if remote != bundle or saved.get('report') != report or saved.get('status') != status:
        raise ValueError('READBACK_MISMATCH')
    return {'sha256': expected, 'bytes': len(bundle)}
