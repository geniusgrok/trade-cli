"""Reject anonymous/forged callers and verify the real workflow identity."""
from datetime import datetime
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo
import private_store as store


def main() -> None:
    for token in (None, 'not-a-valid.signed-token.signature'):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with urllib.request.urlopen(urllib.request.Request(store.ENDPOINT + '/context', data=b'{}', headers=headers), timeout=60):
                raise RuntimeError('UNAUTHORIZED_REQUEST_ACCEPTED')
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise RuntimeError('AUTH_DENIAL_NOT_VERIFIED') from None
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    response = store.request('context', {'date': today, 'compare_date': None})
    if not isinstance(response, dict) or set(response) != {'existing', 'previous', 'comparison'}:
        raise RuntimeError('PRIVATE_STORE_NOT_READY')
    print('ANONYMOUS_AND_FORGED_TOKEN_DENIED; WORKFLOW_OIDC_VERIFIED')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('BLOCKED: PRIVATE_STORE_AUTH_OR_AVAILABILITY')
        raise SystemExit(1)
