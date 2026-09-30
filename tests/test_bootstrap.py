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


    def test_source_failures_expose_only_safe_diagnostic_categories(self):
        import subprocess

        cases = [
            ('Repository not found', 'SOURCE_REPOSITORY_NOT_ACCESSIBLE'),
            ('Authentication failed', 'SOURCE_AUTHENTICATION_FAILED'),
            ('Could not resolve host', 'SOURCE_DNS_FAILED'),
            ('Write access to repository not granted', 'SOURCE_ACCESS_FORBIDDEN'),
            ('The requested URL returned error: 403', 'SOURCE_ACCESS_FORBIDDEN'),
            ('The requested URL returned error: 401', 'SOURCE_AUTHENTICATION_FAILED'),
            ('could not read Username: terminal prompts disabled', 'SOURCE_AUTHENTICATION_FAILED'),
            ('The requested URL returned error: 404', 'SOURCE_REPOSITORY_NOT_ACCESSIBLE'),
            ('Remote branch main not found', 'SOURCE_BRANCH_MISSING'),
            ('destination path already exists', 'SOURCE_DIRECTORY_NOT_EMPTY'),
        ]
        for message, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                out = io.StringIO()
                env = {'RUNNER_TEMP': root, 'TRADE_READ_TOKEN': 'test-read-secret'}

                def fail_clone(command, **kwargs):
                    kwargs['stdout'].write((message + ' private-log test-read-secret').encode())
                    raise subprocess.CalledProcessError(128, command)

                with patch.dict(os.environ, env, clear=True), \
                        patch('bootstrap.sys.version_info', (3, 12, 0)), \
                        patch('bootstrap.subprocess.run', side_effect=fail_clone) as run, \
                        patch('bootstrap.store.request') as save, \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(bootstrap.main(), 1)
                self.assertEqual(run.call_count, 1)
                self.assertIn('PREPARATION_PHASE: SOURCE_CLONE', out.getvalue())
                self.assertIn('PREPARATION_REASON: ' + reason, out.getvalue())
                self.assertNotIn('private-log', out.getvalue())
                self.assertNotIn('test-read-secret', out.getvalue())
                details = save.call_args.args[1]['details']
                self.assertEqual(details['reason'], reason)
                self.assertNotIn('test-read-secret', details['log_tail'])

    def test_dependency_failure_is_distinguished_from_source_access(self):
        from pathlib import Path
        import subprocess

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'trade-source'
            source.mkdir()
            (source / 'requirements-lock.txt').write_text('', encoding='utf-8')
            env = {'RUNNER_TEMP': root, 'TRADE_READ_TOKEN': 'test-read-secret'}
            out = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch('bootstrap.sys.version_info', (3, 12, 0)), \
                    patch('bootstrap.subprocess.run', side_effect=[None, None, None,
                          subprocess.CalledProcessError(1, 'pip')]), \
                    patch('bootstrap.store.request'), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(bootstrap.main(), 1)
            self.assertIn('PREPARATION_PHASE: DEPENDENCY_INSTALLATION', out.getvalue())
            self.assertIn('PREPARATION_REASON: OPERATION_FAILED', out.getvalue())


if __name__ == '__main__':
    unittest.main()
