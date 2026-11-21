"""Input preparation preserves native fail-closed checks before computation."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from runner import prepare_regime_inputs

try:
    import pandas as pd
except ImportError:
    pd = None


class IndexPreparationTests(unittest.TestCase):
    def setUp(self):
        self.refresh = Mock(return_value={'indices': {'000300': {'provider': 'Tencent'}}})
        self.coverage = Mock(return_value={'000300': {'evidence_date': '2026-09-22'}})
        contracts = types.ModuleType('quantfusion.data.contracts')
        contracts.refresh_regime_indices = self.refresh
        sessions = types.ModuleType('quantfusion.data.sessions')
        sessions.index_coverage = self.coverage
        self.modules = patch.dict(sys.modules, {
            'quantfusion': types.ModuleType('quantfusion'),
            'quantfusion.data': types.ModuleType('quantfusion.data'),
            'quantfusion.data.contracts': contracts,
            'quantfusion.data.sessions': sessions,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.dates = {'required_evidence_date': '2026-09-22'}

    def test_reuses_native_providers_and_retains_provenance(self):
        result = prepare_regime_inputs('/runtime/regime', '2026-09-22', self.dates)
        self.refresh.assert_called_once_with(
            '/runtime/regime', end_date='2026-09-22', strict=True, allow_provider_fallback=True
        )
        self.coverage.assert_called_once_with('/runtime/regime', self.dates)
        self.assertEqual(result, {'refresh': self.refresh.return_value, 'coverage': self.coverage.return_value})

    def test_unavailable_providers_do_not_continue(self):
        self.refresh.side_effect = RuntimeError('all providers failed')
        with self.assertRaisesRegex(RuntimeError, 'all providers failed'):
            prepare_regime_inputs('/runtime/regime', '2026-09-22', self.dates)
        self.coverage.assert_not_called()

    def test_preserved_stale_file_is_not_accepted(self):
        self.refresh.return_value = {'indices': {'000300': {'status': 'preserved_last_good'}}}
        self.coverage.side_effect = ValueError('INDEX_EVIDENCE_STALE')
        with self.assertRaisesRegex(ValueError, 'INDEX_EVIDENCE_STALE'):
            prepare_regime_inputs('/runtime/regime', '2026-09-22', self.dates)

    def test_missing_index_is_not_accepted(self):
        self.coverage.side_effect = FileNotFoundError('000682.csv')
        with self.assertRaises(FileNotFoundError):
            prepare_regime_inputs('/runtime/regime', '2026-09-22', self.dates)


@unittest.skipIf(pd is None, 'Production pandas is installed by bootstrap')
class HistoricalIndexRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = '2026-11-02'
        self.dates = {'requested_as_of': self.target, 'required_evidence_date': self.target}
        self.anchor = pd.DataFrame({'date': ['2026-10-30'], 'close': [1.0]})
        self.new = pd.DataFrame({'date': ['2026-10-29', '2026-10-30', self.target, '2026-11-03'],
                                 'close': [2.5, 1.5, 3., 4.]})
        contracts = self.contracts = types.ModuleType('quantfusion.data.contracts')
        contracts.INDEX_SYMBOLS = {'000300': 'csi000300', '000682': 'csi000682'}
        contracts.is_frozen_data_directory = Mock(return_value=False)
        contracts.refresh_regime_indices = Mock(side_effect=AssertionError('ordinary historical path stays offline'))
        contracts._fetch_index_frame = Mock(return_value=self.new)
        contracts._fetch_fallback_index_frame = Mock(return_value=(self.anchor, 'Tencent'))
        contracts.ak = types.SimpleNamespace(stock_zh_index_daily=Mock(return_value=self.new))
        contracts._normalize_index_frame = Mock(side_effect=lambda frame, *, end_date:
                                                frame.loc[frame['date'] <= end_date])
        contracts._atomic_csv = Mock(side_effect=lambda frame, path: frame.to_csv(path, index=False))
        sessions = types.ModuleType('quantfusion.data.sessions')

        def coverage(root, dates):
            found = {}
            for code in contracts.INDEX_SYMBOLS:
                frame = pd.read_csv(Path(root) / f'{code}.csv')
                if dates['required_evidence_date'] not in set(frame['date']):
                    raise ValueError(f'INDEX_EVIDENCE_UNAVAILABLE:{code}')
                found[code] = {'evidence_date': self.target}
            return found

        sessions.index_coverage = Mock(side_effect=coverage)
        modules = patch.dict(sys.modules, {'quantfusion': types.ModuleType('quantfusion'),
            'quantfusion.data': types.ModuleType('quantfusion.data'),
            'quantfusion.data.contracts': contracts, 'quantfusion.data.sessions': sessions})
        modules.start()
        self.addCleanup(modules.stop)
        for code in contracts.INDEX_SYMBOLS:
            self.anchor.to_csv(self.root / f'{code}.csv', index=False)

    def prepare(self):
        return prepare_regime_inputs(self.root, self.target, self.dates, historical_recovery=True)

    def test_stale_archive_gets_full_histories_cut_at_target_and_tries_sina_after_stale_tencent(self):
        self.contracts._fetch_index_frame.side_effect = [self.new, self.anchor]
        result = self.prepare()
        self.assertEqual(result['refresh']['indices']['000682']['provider'], 'Sina')
        self.contracts._fetch_fallback_index_frame.assert_called_once_with('000682', end_timestamp=pd.Timestamp(self.target))
        self.contracts.ak.stock_zh_index_daily.assert_called_once_with(symbol='sh000682')
        for code in self.contracts.INDEX_SYMBOLS:
            self.assertEqual(result['coverage'][code]['evidence_date'], self.target)
            self.assertEqual(pd.read_csv(self.root / f'{code}.csv')['date'].tolist(),
                             ['2026-10-29', '2026-10-30', self.target])
        self.assertEqual(self.prepare()['refresh']['mode'], 'historical_verified_existing')
        self.assertEqual(self.contracts._fetch_index_frame.call_count, 2)

    def test_missing_target_on_either_index_keeps_both_existing_files(self):
        self.contracts._fetch_index_frame.side_effect = [self.new, self.anchor]
        self.contracts.ak.stock_zh_index_daily.return_value = self.anchor
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_INDEX_UNAVAILABLE:000682'):
            self.prepare()
        self.contracts._atomic_csv.assert_not_called()
        self.contracts.is_frozen_data_directory.return_value = True
        with self.assertRaisesRegex(ValueError, 'HISTORICAL_INDEX_DIRECTORY_FROZEN'):
            self.prepare()
        self.assertEqual(self.contracts._fetch_index_frame.call_count, 2)


if __name__ == '__main__':
    unittest.main()
