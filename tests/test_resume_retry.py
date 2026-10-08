"""Finite wait classification never promotes other failures to late delivery."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from runner import current_bar_pending, failure_reason, prepare_native_with_retry


class ResumeRetryTests(unittest.TestCase):
    target, previous = '2026-10-09', '2026-10-08'
    bar = 'TRADING_DAY_COVERAGE:300308: expected=2026-10-09; observed=2026-10-08; missing bar or conflicting/unverified suspension'
    eligible = {'300308': {'status': 'SESSION_REQUIRED', 'required_quote_date': target}}

    def test_native_single_current_bar_absence_is_the_only_stock_wait(self):
        self.assertTrue(current_bar_pending(self.bar, self.target, self.previous, self.eligible))
        for message in (self.bar.replace('observed=2026-10-08', 'observed=2026-09-30'),
                self.bar.replace('expected=2026-10-09', 'expected=2026-10-08'),
                'invalid_response ' + self.bar, 'RISK_INPUT_UNAVAILABLE:300308',
                'INDEX_EVIDENCE_UNAVAILABLE:000300: missing file', 'Unable to refresh index',
                'checkpoint observed input prefix changed (qfq vintage UNKNOWN)'):
            with self.subTest(message=message):
                self.assertFalse(current_bar_pending(message, self.target, self.previous, self.eligible))
        for eligibility in ({}, {'300308': {'status': 'CERTIFIED_SUSPENSION', 'required_quote_date': self.previous}}):
            self.assertFalse(current_bar_pending(self.bar, self.target, self.previous, eligibility))

    def test_a_late_bar_does_not_mask_another_native_fatal_error(self):
        late = '    300308 光通信: ' + self.bar
        self.assertTrue(current_bar_pending(late, self.target, self.previous, self.eligible))
        for other in ('    688256 算力: invalid_response', '    688256 算力: FUTURE_EVIDENCE:688256',
                '    688256 算力: 必需行情返回空数据', '  [Cache] 688256: history refresh failed (opaque error); using cached data only'):
            with self.subTest(other=other):
                self.assertFalse(current_bar_pending(late + '\n' + other, self.target, self.previous, self.eligible))

    def test_wait_is_finite_and_a_historical_request_has_no_wait(self):
        for pending, expected_waits, builds_expected, reason in (
                ((self.target, self.previous), [600, 600], 3, 'CURRENT_BAR_UNAVAILABLE'),
                (None, [], 1, 'NATIVE_PREPARATION_FAILED')):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as name:
                contexts, waits = [], []
                def build():
                    ctx = SimpleNamespace(scan_dates={'symbol_eligibility': self.eligible})
                    contexts.append(ctx); return ctx
                with (Path(name) / 'native.log').open('w') as log:
                    def probe(ctx):
                        print(self.bar, file=log); return False
                    with self.assertRaisesRegex(ValueError, reason):
                        prepare_native_with_retry(build, None, log, lambda ctx, load: True,
                            probe, waits.append, pending_session=pending)
                self.assertEqual(waits, expected_waits)
                self.assertEqual(len(contexts), builds_expected)
                self.assertEqual(len({id(ctx) for ctx in contexts}), builds_expected)

    def test_public_failure_category_contains_no_private_exception_values(self):
        self.assertEqual(failure_reason(ValueError('checkpoint observed input prefix changed (qfq vintage UNKNOWN)')), 'CHECKPOINT_HISTORY_CHANGED')
        self.assertEqual(failure_reason(ValueError('checkpoint source/runtime/config/calendar/pool identity changed')), 'CHECKPOINT_IDENTITY_CHANGED')
        for message in ('assets=987654.25', '123456.00', 'INVALID_RESPONSE private body', 'EXISTING_RESULT_UNUSABLE:private-status'):
            result = failure_reason(ValueError(message))
            self.assertNotIn('987654', result); self.assertNotIn('private', result)
            self.assertIn(result, ('PRODUCTION_VALIDATION_FAILED', 'EXISTING_RESULT_UNUSABLE'))


if __name__ == '__main__': unittest.main()
