"""仅发布已保存结果的中文公开视图，不调用策略、不改写同日结果。"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import time as clock
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from public_report import checked_date, markdown, safe_document

REPOSITORY = 'geniusgrok/trade-cli'
API = f'https://api.github.com/repos/{REPOSITORY}'


def blob_id(data: bytes) -> str:
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


def api(method: str, path: str, body: dict | None = None, *, limit: int = 2_000_000) -> dict | None:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(API + path, data=data, method=method, headers={
        'Authorization': 'Bearer ' + os.environ['REPORT_PUBLISH_TOKEN'],
        'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    for attempt in range(3 if method == 'GET' else 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(limit + 1)
            if len(raw) > limit:
                raise ValueError('PUBLIC_RESPONSE_TOO_LARGE')
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if method == 'GET' and exc.code == 404:
                return None
            if method == 'GET' and exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                clock.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f'PUBLIC_HTTP_{exc.code}') from None
        except (urllib.error.URLError, TimeoutError):
            if method == 'GET' and attempt < 2:
                clock.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError('PUBLIC_TRANSPORT_FAILED') from None


def read_file(path: str, ref: str) -> bytes | None:
    result = api('GET', '/contents/' + path + '?ref=' + urllib.parse.quote(ref, safe=''))
    if result is None:
        return None
    if result.get('type') != 'file' or result.get('encoding') != 'base64':
        raise ValueError('INVALID_PUBLIC_FILE')
    data = base64.b64decode(''.join(result['content'].split()), validate=True)
    if len(data) != result['size'] or blob_id(data) != result['sha']:
        raise ValueError('PUBLIC_FILE_INTEGRITY')
    return data


def verify_file(path: str, expected: bytes, commit: str | None = None) -> dict:
    if commit is not None and read_file(path, commit) != expected:
        raise ValueError('PUBLIC_COMMIT_READBACK_MISMATCH')
    ref = api('GET', '/git/ref/heads/main')
    head = ref['object']['sha']
    if not re.fullmatch('[0-9a-f]{40}', head) or read_file(path, head) != expected:
        raise ValueError('PUBLIC_MAIN_READBACK_MISMATCH')
    return {'path': path, 'commit': commit or head, 'verified_main': head,
            'bytes': len(expected), 'sha256': hashlib.sha256(expected).hexdigest(),
            'git_blob': blob_id(expected)}


def publish(target: str, text: str) -> dict:
    checked_date(target)
    safe_document(text)
    data = text.encode('utf-8')
    if len(data) > 500_000:
        raise ValueError('PUBLIC_REPORT_TOO_LARGE')
    path = f'reports/{target}.md'
    existing = read_file(path, 'main')
    if existing is not None:
        if existing != data:
            raise ValueError('EXISTING_PUBLIC_REPORT_DIFFERS')
        return verify_file(path, data)
    payload = {'message': f'保存 {target} 盘后日报', 'branch': 'main',
               'content': base64.b64encode(data).decode()}
    # No sha parameter: only create. A concurrent or uncertain write is read first.
    try:
        receipt = api('PUT', '/contents/' + path, payload)
    except RuntimeError:
        if read_file(path, 'main') != data:
            raise
        return verify_file(path, data)
    if receipt['content']['sha'] != blob_id(data):
        raise ValueError('PUBLIC_RECEIPT_MISMATCH')
    return verify_file(path, data, receipt['commit']['sha'])


def verified_report(saved: dict, target: str, bundle: bytes) -> dict:
    report = saved['report']
    if (report['target_date'] != target or report['status'] != saved['status'] or
            str(report['actions_run_id']) != saved['run_id'] or len(bundle) != saved['bytes'] or
            hashlib.sha256(bundle).hexdigest() != saved['sha256']):
        raise ValueError('SAVED_REPORT_IDENTITY')
    if saved['status'] in {'SUCCESS', 'DEGRADED'}:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
            members = archive.getmembers()
            if sum(m.size for m in members) > 64 * 1024 * 1024:
                raise ValueError('SAVED_REPORT_BUNDLE_LIMIT')
            matches = [m for m in members if m.name == 'output/daily_report.json']
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 1_000_000:
                raise ValueError('SAVED_REPORT_MEMBER')
            content = archive.extractfile(matches[0])
            if content is None or json.loads(content.read()) != report:
                raise ValueError('SAVED_REPORT_READBACK')
    return report


def main() -> None:
    import private_store as store
    source = Path(os.environ['RUNNER_TEMP']) / 'trade-source'
    sys.path.insert(0, str(source))
    from quantfusion.data.sessions import load_calendar
    from runner import resolve_target

    target, _, gate = resolve_target(load_calendar(), datetime.now(ZoneInfo('Asia/Shanghai')),
        os.environ.get('TARGET_DATE', ''), os.environ['GITHUB_EVENT_NAME'])
    if gate != 'READY':
        print('休市或尚未收盘，不发布新的盘后日报')
        return
    saved = store.request('result', {'date': target})
    if not saved or saved.get('status') == 'RUNNING':
        raise ValueError('RESULT_NOT_FINISHED')
    bundle = store.decode_bundle(saved['bundle'], saved['sha256'])
    report = verified_report(saved, target, bundle)
    proof = publish(target, markdown(report))
    # The verified public Git commit is the publication receipt, not a new store event.
    print(json.dumps(proof, ensure_ascii=False, sort_keys=True))
    print('中文日报已保存并完成内容回读核验：' + proof['path'])
    if saved['status'] == 'FAILED':
        raise ValueError('SAVED_RESULT_FAILED')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not leak private responses, values, paths or diagnostics in public logs.
        print('日报发布或核验失败；不重新计算策略，不覆盖已有日报')
        raise SystemExit(1) from None
