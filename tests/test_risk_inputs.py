"""Risk evidence extends the snapshot, never the production trading universe."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

from runner import prepare_risk_inputs


def frame(**kwargs):
    value = Mock(empty=False, attrs={})
    value.configure_mock(**kwargs)
    value.copy.return_value = value
    return value


class RiskInputTests(unittest.TestCase):
    def setUp(self):
        self.fetch = Mock(return_value=frame())
        self.coverage = Mock(return_value='2026-09-22')
        overlay = types.ModuleType('quantfusion.config.overlay')
        overlay.RISK_BASKET = ('A', 'B')
        providers = types.ModuleType('quantfusion.data.providers')
        providers.DataFetcher = types.SimpleNamespace(load_stock_data=self.fetch)
        sessions = types.ModuleType('quantfusion.data.sessions')
        sessions.require_frame_coverage = self.coverage
        modules = patch.dict(sys.modules, {
            'quantfusion': types.ModuleType('quantfusion'),
            'quantfusion.config': types.ModuleType('quantfusion.config'),
            'quantfusion.config.overlay': overlay,
            'quantfusion.data': types.ModuleType('quantfusion.data'),
            'quantfusion.data.providers': providers,
            'quantfusion.data.sessions': sessions,
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.ctx = types.SimpleNamespace(
            request=types.SimpleNamespace(start_date='2026-07-01', end_date='2026-09-22', cache_dir='/runtime/cache'),
            snapshot_frames={'A': frame()}, actual_evidence_dates={'A': '2026-09-22'},
            scan_dates={'required_evidence_date': '2026-09-22'},
            symbols={'A': 'tradable'}, tradable={'A': 'tradable'},
        )

    def test_fetch_missing_evidence_without_expanding_trading_pool(self):
        dates = prepare_risk_inputs(self.ctx)
        self.fetch.assert_called_once_with('B', '2025-05-27', '2026-09-22', data_dir=None, cache_dir='/runtime/cache')
        self.assertEqual(dates, {'A': '2026-09-22', 'B': '2026-09-22'})
        self.assertEqual(set(self.ctx.snapshot_frames), {'A', 'B'})
        self.assertEqual(self.ctx.symbols, {'A': 'tradable'})
        self.assertEqual(self.ctx.tradable, self.ctx.symbols)
        self.assertEqual(self.coverage.call_count, 2)

    def test_existing_evidence_is_reused(self):
        self.ctx.snapshot_frames['B'] = frame()
        prepare_risk_inputs(self.ctx)
        self.fetch.assert_not_called()

    def test_unavailable_or_stale_input_does_not_publish_partial_snapshot(self):
        for value in (None, frame(empty=True), frame(attrs={'_stale': True})):
            with self.subTest(value=value):
                self.fetch.return_value = value
                with self.assertRaisesRegex(ValueError, 'RISK_INPUT_UNAVAILABLE:B'):
                    prepare_risk_inputs(self.ctx)
                self.assertEqual(set(self.ctx.snapshot_frames), {'A'})
                self.assertEqual(self.ctx.actual_evidence_dates, {'A': '2026-09-22'})

    def test_wrong_date_does_not_publish_partial_snapshot(self):
        self.coverage.side_effect = ['2026-09-22', ValueError('RISK_DATE_MISMATCH')]
        with self.assertRaisesRegex(ValueError, 'RISK_DATE_MISMATCH'):
            prepare_risk_inputs(self.ctx)
        self.assertEqual(set(self.ctx.snapshot_frames), {'A'})
        self.assertEqual(self.ctx.actual_evidence_dates, {'A': '2026-09-22'})


if __name__ == '__main__':
    unittest.main()
