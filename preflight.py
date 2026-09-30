"""Fail before production if state cannot be read or authenticated."""
import private_store as store


def main() -> None:
    store._key()
    head, _, files = store._snapshot()
    if not head or not isinstance(files, dict):
        raise ValueError('STATE_BRANCH_UNAVAILABLE')
    for path in sorted(p for p in files if p.startswith('runs/') and p.endswith('.json.enc'))[-2:]:
        store._read(path, files)
    print('STATE_BRANCH_AND_ENCRYPTION_VERIFIED')


def check() -> int:
    try:
        main()
    except Exception as exc:
        # Only fixed categories may enter public Actions logs.
        reason = str(exc)
        if reason not in {
            'STATE_KEY_MISSING_OR_SHORT', 'STATE_BRANCH_MISSING',
            'STATE_BRANCH_UNAVAILABLE', 'STATE_DECRYPT_FAILED',
            'STATE_ENVELOPE_INVALID', 'STATE_DOCUMENT_INVALID',
            'STATE_BLOB_INTEGRITY', 'STATE_BLOB_ENCODING',
            'STATE_TREE_TRUNCATED', 'PUBLIC_TRANSPORT_FAILED',
            'PUBLIC_HTTP_401', 'PUBLIC_HTTP_403', 'PUBLIC_HTTP_429',
        }:
            reason = 'STATE_BRANCH_OR_KEY_UNAVAILABLE'
        print('BLOCKED: ' + reason)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(check())
