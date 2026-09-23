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


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('BLOCKED: STATE_BRANCH_OR_KEY_UNAVAILABLE')
        raise SystemExit(1) from None
