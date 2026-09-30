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


class SourceCheckoutTests(unittest.TestCase):
    def test_checkout_uses_transferred_repository_and_preserves_main_and_key(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'trade-source'
            source.mkdir()
            (source / 'requirements-lock.txt').write_text('', encoding='utf-8')
            env = {'RUNNER_TEMP': root, 'TRADE_READ_TOKEN': 'test-read-secret',
                   'STATE_SEAL_KEY': 'existing-state-key'}
            with patch.dict(os.environ, env, clear=True), \
                    patch('bootstrap.sys.version_info', (3, 12, 0)), \
                    patch('bootstrap.subprocess.run') as run, \
                    patch('bootstrap.store.request') as save, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(), 0)
                self.assertEqual(os.environ['STATE_SEAL_KEY'], 'existing-state-key')
            clone = run.call_args_list[0]
            self.assertEqual(clone.args[0], [
                'git', 'clone', '--depth=1', '--filter=blob:none', '--no-checkout',
                '--branch=main', 'https://github.com/geniusgrok/trade.git', str(source)])
            self.assertTrue(clone.kwargs['check'])
            self.assertEqual(clone.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(run.call_args_list[2].args[0], ['git', 'checkout', 'main'])
            install = run.call_args_list[-1]
            self.assertEqual(install.args[0][-2:], ['-r', str(source / 'requirements-lock.txt')])
            self.assertNotIn('TRADE_READ_TOKEN', install.kwargs['env'])
            save.assert_not_called()

    def test_checkout_failure_stops_before_dependency_installation(self):
        import subprocess

        with tempfile.TemporaryDirectory() as root:
            env = {'RUNNER_TEMP': root, 'TRADE_READ_TOKEN': 'test-read-secret'}
            out = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch('bootstrap.sys.version_info', (3, 12, 0)), \
                    patch('bootstrap.subprocess.run', side_effect=subprocess.CalledProcessError(128, 'git')) as run, \
                    patch('bootstrap.store.request', return_value={'saved': True}) as save, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(bootstrap.main(), 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(save.call_args.args[1]['status'], 'PREPARATION_FAILED')
            self.assertIn('BLOCKED: SOURCE_OR_ENVIRONMENT_PREPARATION_FAILED', out.getvalue())
            self.assertNotIn('test-read-secret', out.getvalue())


if __name__ == '__main__':
    unittest.main()
