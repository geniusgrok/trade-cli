"""Check committed file bytes against the local delivery manifest."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).parent
manifest = json.loads((root / 'integrity.json').read_text())
for name, expected in manifest.items():
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('INVALID_MANIFEST_PATH')
    data = path.read_bytes()
    actual = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              'git_blob': hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()}
    if actual != expected:
        raise ValueError('PUBLIC_CHECKOUT_INTEGRITY_FAILED: ' + name)
print(f'PUBLIC_CHECKOUT_BYTE_AND_HASH_VERIFIED: {len(manifest)} files')
