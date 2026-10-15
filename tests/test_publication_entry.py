"""Publication inherits the runner receipt; no second clock or native replay."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import private_store as store
import public_report as report
import publish_report as publisher
import runtime_profile as profile
from runner import resolve_target
from test_publication import fixture


class PublicationEntryTests(unittest.TestCase):
    def invoke(self, saved, *, gate='READY', receipt_change=None, result=None):
        with tempfile.TemporaryDirectory() as directory:
            receipt = {'target_date': '2026-09-22', 'previous_trading_date': '2026-09-21',
                       'gate': gate, 'run_id': '90000', 'attempt': 1,
                       'workflow_sha': 'f' * 40, 'simulation_identity': profile.INHERITED}
            receipt.update(receipt_change or {})
            (Path(directory) / 'trade-daily-receipt.json').write_text(json.dumps(receipt))
            # A changed target environment or a missing native package cannot change the receipt date.
            with patch.dict('os.environ', {'RUNNER_TEMP': directory, 'GITHUB_RUN_ID': '90000',
                    'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'f' * 40, 'TARGET_DATE': '2026-09-23',
                    'TRADE_SIMULATION_IDENTITY': profile.INHERITED}), \
                 patch.dict(sys.modules, {'quantfusion': None, 'runner': None}), \
                 patch.object(store, 'request', return_value=saved) as request, \
                 patch.object(store, 'decode_bundle', return_value=b'evidence'), \
                 patch.object(publisher, 'verified_report', return_value=result or fixture()), \
                 patch.object(publisher, 'publish', return_value={'path': 'reports/2026-09-22.md'}) as publish, \
                 patch('builtins.print'):
                if isinstance(saved, Exception):
                    request.side_effect = saved
                try:
                    publisher.main()
                finally:
                    self.request, self.publish = request, publish

    def test_archived_entry_cannot_read_or_publish_a_new_report(self):
        with patch.dict('os.environ', {'TRADE_SIMULATION_IDENTITY': ''}), \
             patch.object(publisher, 'publish') as publish, \
             patch.object(profile, 'source_path') as source:
            with self.assertRaisesRegex(ValueError, 'ARCHIVED_SERIES_READ_ONLY'):
                publisher.main()
        source.assert_not_called()
        publish.assert_not_called()

    def test_daily_production_requires_the_repository_main_branch(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/trade-daily.yml').read_text()
        self.assertIn("  daily:\n    if: github.repository == 'geniusgrok/trade-cli' && github.ref == 'refs/heads/main'\n", workflow)

    def test_ready_day_without_finished_result_fails_instead_of_reporting_success(self):
        with self.assertRaisesRegex(ValueError, 'RESULT_NOT_FINISHED'):
            self.invoke({'status': 'RUNNING'})
        self.publish.assert_not_called()

    def test_failed_report_is_published_but_workflow_remains_failed(self):
        value = {**fixture(), 'status': 'FAILED'}
        with self.assertRaisesRegex(ValueError, 'SAVED_RESULT_FAILED'):
            self.invoke({'status': 'FAILED', 'bundle': 'unused', 'sha256': 'digest'}, result=value)
        self.publish.assert_called_once_with('2026-09-22', report.markdown(value))

    def test_cross_midnight_publishes_the_runner_day_without_rechecking_clock(self):
        calendar = SimpleNamespace(sessions=('2026-09-21', '2026-09-22', '2026-09-23'),
            coverage_start='2026-01-01', coverage_end='2026-12-31')
        before = datetime(2026, 9, 22, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
        after = datetime(2026, 9, 23, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(resolve_target(calendar, before, ''),
                         ('2026-09-22', '2026-09-21', 'READY'))
        self.assertEqual(resolve_target(calendar, after, '')[2], 'BEFORE_CLOSE')
        self.invoke({'status': 'DEGRADED', 'bundle': 'unused', 'sha256': 'digest'})
        self.request.assert_called_once_with('result', {'date': '2026-09-22'})
        self.publish.assert_called_once_with('2026-09-22', report.markdown(fixture()))

    def test_holiday_receipt_remains_skipped_on_the_following_trading_day(self):
        calendar = SimpleNamespace(sessions=('2026-09-24', '2026-09-28'),
            coverage_start='2026-01-01', coverage_end='2026-12-31')
        sunday = datetime(2026, 9, 27, 17, tzinfo=ZoneInfo('Asia/Shanghai'))
        monday = datetime(2026, 9, 28, 17, tzinfo=ZoneInfo('Asia/Shanghai'))
        target, previous, gate = resolve_target(calendar, sunday, '')
        self.assertEqual(gate, 'HOLIDAY')
        self.assertEqual(resolve_target(calendar, monday, '')[2], 'READY')
        self.invoke({}, gate=gate, receipt_change={'target_date': target, 'previous_trading_date': previous})
        self.request.assert_not_called()
        self.publish.assert_not_called()

    def test_other_run_attempt_workflow_or_identity_receipt_is_rejected(self):
        for change in ({'run_id': 'other'}, {'attempt': 2}, {'workflow_sha': 'e' * 40},
                       {'simulation_identity': ''}, {'gate': 'UNKNOWN'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'RUN_RECEIPT_IDENTITY'):
                self.invoke({}, receipt_change=change)
            self.request.assert_not_called()
            self.publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
