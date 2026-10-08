import hashlib
import unittest

from cryptography.exceptions import InvalidTag
from checkpoint_transfer import import_public_key, open_import, seal_import


class CheckpointTransferTests(unittest.TestCase):
    def setUp(self):
        self.key = hashlib.sha256(b'synthetic transfer fixture only').digest()
        self.payload = b'{"scope":"synthetic; no real account or keys"}'
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.sealed = seal_import(self.payload, import_public_key(self.key))

    def test_exact_authenticated_roundtrip(self):
        self.assertEqual(open_import(self.sealed, self.key, self.digest), self.payload)

    def test_ciphertext_tampering_fails(self):
        data = self.sealed[:-1] + bytes([self.sealed[-1] ^ 1])
        with self.assertRaises(InvalidTag):
            open_import(data, self.key, self.digest)

    def test_wrong_approved_payload_hash_fails(self):
        with self.assertRaises(InvalidTag):
            open_import(self.sealed, self.key, '0' * 64)

    def test_different_receiver_fails(self):
        with self.assertRaises(InvalidTag):
            open_import(self.sealed, b'\x01' * 32, self.digest)

    def test_invalid_protocol_fails(self):
        with self.assertRaises(ValueError):
            open_import(b'BAD1' + self.sealed[4:], self.key, self.digest)

    def test_public_key_encryption_is_not_origin_authentication(self):
        other = b'{"scope":"unapproved synthetic payload"}'
        sealed = seal_import(other, import_public_key(self.key))
        with self.assertRaises(InvalidTag):
            open_import(sealed, self.key, self.digest)


if __name__ == '__main__':
    unittest.main()
