import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class CredentialReadinessTests(unittest.TestCase):
    def check_missing(self, variable_present, expected):
        with tempfile.TemporaryDirectory() as root:
            env = {'RUNNER_TEMP': root, 'TRADE_READ_TOKEN': '',
                   'SOURCE_TOKEN_VARIABLE_PRESENT': variable_present}
            out = io.StringIO()
            with patch.dict(os.environ, env, clear=True), patch('bootstrap.subprocess.run') as run, patch('bootstrap.store.request', return_value={'saved': True}) as save, contextlib.redirect_stdout(out):
                self.assertEqual(bootstrap.main(), 1)
            run.assert_not_called()
            self.assertEqual(save.call_args.args[0], 'event')
            payload = save.call_args.args[1]
            self.assertEqual(payload['status'], 'PREPARATION_FAILED')
            self.assertFalse(payload['details']['strategy_calculation_started'])
            self.assertEqual(payload['details']['error'], expected)
            self.assertIn('BLOCKED: ' + expected, out.getvalue())

    def test_unavailable_secret_records_private_failure_without_clone(self):
        self.check_missing('false', 'SOURCE_READ_SECRET_MISSING')

    def test_variable_presence_never_substitutes_for_a_secret(self):
        self.check_missing('true', 'SOURCE_TOKEN_IS_VARIABLE_NOT_SECRET')

    def test_storage_failure_does_not_enable_calculation(self):
        with tempfile.TemporaryDirectory() as root:
            out = io.StringIO()
            with patch.dict(os.environ, {'RUNNER_TEMP': root}, clear=True), patch('bootstrap.subprocess.run') as run, patch('bootstrap.store.request', side_effect=RuntimeError('unavailable')), contextlib.redirect_stdout(out):
                self.assertEqual(bootstrap.main(), 1)
            run.assert_not_called()
            self.assertIn('PRIVATE_FAILURE_RECORD_UNAVAILABLE', out.getvalue())


if __name__ == '__main__':
    unittest.main()
