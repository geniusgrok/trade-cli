"""Full-frame basis conversion preserves exact history and rejects revisions."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

try:
    import pandas as pd
except ImportError:
    pd = None

import continuous_inputs as inputs


@unittest.skipIf(pd is None, 'Production pandas is installed by bootstrap')
class PriceBasisTests(unittest.TestCase):
    def setUp(self):
        self.close = '2026-10-09'
        self.old = pd.DataFrame({'open': [100.12, 100.5], 'close': [99.34, 101.42],
            'high': [101.56, 102.3], 'low': [98.42, 100.1], 'volume': [100.0, 200.0]},
            index=pd.to_datetime(['2026-10-08', self.close]))
        self.fresh = pd.concat([self.old, self.old.iloc[-1:].set_axis(pd.to_datetime(['2026-10-12']))])
        self.fresh[['open', 'close', 'high', 'low']] /= 1.002
        self.expected = inputs.prefix_hash(self.old, self.close)
        module = types.ModuleType('quantfusion.data.providers')
        module._normalize_columns = lambda frame: frame
        self.patch = patch.dict(sys.modules, {'quantfusion.data.providers': module})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def convert(self, fresh=None, expected=None):
        return inputs.match_price_basis(self.fresh if fresh is None else fresh, self.old,
            self.close, self.expected if expected is None else expected)

    def test_complete_history_reproduces_committed_hash_and_converts_the_new_bar(self):
        original, old = self.fresh.copy(), self.old.copy()
        adjusted, proof = self.convert()
        self.assertEqual(inputs.prefix_hash(adjusted, self.close), self.expected)
        self.assertEqual(adjusted.loc['2026-10-12', 'close'], 101.42)
        self.assertTrue(proof['exact_prefix_verified'])
        pd.testing.assert_frame_equal(self.fresh, original)
        pd.testing.assert_frame_equal(self.old, old)
        pd.testing.assert_series_equal(adjusted['volume'], original['volume'])

    def test_volume_revision_missing_history_and_isolated_price_correction_fail(self):
        volume, price = self.fresh.copy(), self.fresh.copy()
        volume.loc['2026-10-08', 'volume'] += 1
        price.loc['2026-10-08', 'close'] += .02
        for frame in (volume, price, self.fresh.iloc[1:]):
            with self.subTest(frame=frame), self.assertRaisesRegex(ValueError, 'CHECKPOINT_HISTORY_CHANGED'):
                self.convert(frame)

    def test_exact_hash_is_required_even_when_a_scale_fits(self):
        with self.assertRaisesRegex(ValueError, 'CHECKPOINT_HISTORY_CHANGED'):
            self.convert(expected='0' * 64)

    def test_datetime_storage_units_do_not_change_daily_evidence_identity(self):
        self.old.index = self.old.index.as_unit('us')
        self.fresh.index = self.fresh.index.as_unit('ns')
        adjusted, proof = self.convert()
        self.assertEqual(inputs.prefix_hash(adjusted, self.close), self.expected)
        self.assertTrue(proof['exact_prefix_verified'])

    def test_corrupt_and_nonpositive_prices_do_not_enter_the_converter(self):
        for value in (float('inf'), 0, -1):
            frame = self.fresh.copy()
            frame.loc['2026-10-08', 'open'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'CHECKPOINT_HISTORY_CHANGED'):
                self.convert(frame)

    def test_native_raw_bars_and_factors_recover_precision_before_cent_rounding(self):
        raw = self.fresh.copy()
        raw[['open', 'close', 'high', 'low']] = self.old.iloc[-1][['open', 'close', 'high', 'low']].to_numpy()
        factors = pd.DataFrame({'date': ['2020-01-01', '2026-10-12'], 'qfq_factor': [1.503, 1.0]})
        def normalize(frame):
            value = frame.copy()
            if 'date' in value:
                value = value.set_index(pd.to_datetime(value.pop('date')))
            value.index.name = 'date'
            return value[['open', 'close', 'high', 'low', 'volume']]
        providers = types.SimpleNamespace(ak=types.SimpleNamespace(stock_zh_a_daily=Mock(
            side_effect=[raw.reset_index(names='date'), factors])),
            _exchange_symbol=lambda code: 'sz' + code, _normalize_columns=normalize,
            _normalize_provider_volume=lambda frame, provider, symbol: frame)
        precise = inputs.fetch_precise_sina(providers, '300308', '2026-10-08', '2026-10-12')
        self.assertAlmostEqual(precise.loc['2026-10-08', 'close'], raw.iloc[0]['close'] / 1.503)
        self.assertIn('qfq_factor', precise.attrs['basis_factors_csv'])
        old = normalize(raw.loc[:self.close].copy())
        old[['open', 'close', 'high', 'low']] = (old[['open', 'close', 'high', 'low']] / 1.5).round(2)
        restored, proof = inputs.match_price_basis(precise, old, self.close, inputs.prefix_hash(old, self.close))
        self.assertEqual(inputs.prefix_hash(restored, self.close), inputs.prefix_hash(old, self.close))
        self.assertTrue(proof['exact_prefix_verified'])


if __name__ == '__main__':
    unittest.main()
