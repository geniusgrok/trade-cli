"""Input preparation preserves native fail-closed checks before computation."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

from runner import prepare_regime_inputs


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


if __name__ == '__main__':
    unittest.main()
