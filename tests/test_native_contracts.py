"""Frozen production contracts with synthetic signals and bars; no account replay."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import public_report
import runner
import runtime_profile
from test_publication import fixture

SOURCE = os.environ.get('TRADE_CONTRACT_SOURCE', '')


@unittest.skipUnless(SOURCE, 'Pinned production source is checked after bootstrap')
class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=SOURCE, text=True).strip(),
                         runtime_profile.SOURCE_SHA)
        self.source = patch.object(sys, 'path', [SOURCE, *sys.path])
        self.source.start(); self.addCleanup(self.source.stop)
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('NO_MARKET_NETWORK'))
        self.network.start(); self.addCleanup(self.network.stop)

    def test_real_signal_serializer_public_intent_preserves_only_safe_fields(self):
        from quantfusion.domain.models import Signal, SignalPurpose
        from quantfusion.application.daily_signals import serialize_pending_signals
        signal = Signal(symbol='688361', strategy_name='fixture_strategy', direction='buy', target_shares=200,
                        price=123.45, signal_date='2026-10-12', purpose=SignalPurpose.ENTRY,
                        reason='PRIVATE_ACCOUNT_MUST_NOT_APPEAR', position_target=999999)
        pending, _ = serialize_pending_signals([signal], False)
        _, blocked = serialize_pending_signals([signal], True, blocked_reason='current_route_mismatch')
        frozen = copy.deepcopy((pending, blocked))
        text = public_report.intents(pending) + public_report.intents(blocked)
        for value in ('方向：buy', '信号日期：2026-10-12', '意图用途：entry', '策略名称：fixture_strategy',
                      '已受阻：是', '受阻原因：current_route_mismatch', '原生执行队列标记：否'):
            self.assertIn(value, text)
        for value in ('PRIVATE_ACCOUNT_MUST_NOT_APPEAR', '123.45', '999999', 'target_shares', 'position_target'):
            self.assertNotIn(value, text)
        value = fixture()
        value['observation']['symbols'][0]['pending_signals'] = pending
        value['comparison'] = {'status': '有变化', 'changes': [
            {'field': '000000.pending_signals', 'yesterday': pending, 'today': blocked}]}
        document = public_report.markdown(value)
        self.assertIn('受阻原因：current_route_mismatch', document)
        self.assertIn('不保证真实市场可成交', document)
        self.assertEqual((pending, blocked), frozen)

    def test_certified_suspension_risk_and_final_coverage_share_native_eligibility(self):
        import pandas as pd
        from quantfusion.config.overlay import RISK_BASKET
        from quantfusion.data.sessions import require_frame_coverage, resolve_scan_dates
        dates = resolve_scan_dates('2026-07-01')
        def frame(day):
            return pd.DataFrame({'open': [10.], 'high': [11.], 'low': [9.], 'close': [10.], 'volume': [100.]},
                                index=pd.to_datetime([day]))
        frames = {code: frame('2026-06-26' if code == '688072' else '2026-07-01') for code in RISK_BASKET}
        frames['688072'] = frame('2026-06-26')
        frozen = {code: value.copy() for code, value in frames.items()}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'latest_success.json').write_text(json.dumps({'scan_date': '2026-07-01', 'run_id': 'synthetic'}))
            ctx = SimpleNamespace(request=SimpleNamespace(start_date='2026-07-01', end_date='2026-07-01',
                    cache_dir=output / 'cache', output_dir=output),
                    snapshot_frames=frames, actual_evidence_dates={}, scan_dates=dates,
                    regime_dir=output, run_id='synthetic')
            evidence = runner.prepare_risk_inputs(ctx)
            native = {'status': 'ok', 'mode': 'simulation', 'scan_date': '2026-07-01', 'run_id': 'synthetic',
                      'actual_evidence_dates': {**evidence, '688072': '2026-06-26'},
                      'warmup_health': {'warmup_status': 'READY'}}
            indices = {'000300': {'evidence_date': '2026-07-01'}}
            with patch('quantfusion.data.sessions.index_coverage', return_value=indices):
                runner.validate_native(native, ctx, {'688072': 'synthetic'}, None)
                native['actual_evidence_dates']['688072'] = '2026-06-25'
                with self.assertRaisesRegex(ValueError, 'STOCK_DATE_COVERAGE'):
                    runner.validate_native(native, ctx, {'688072': 'synthetic'}, None)
            with self.assertRaisesRegex(ValueError, 'TRADING_DAY_COVERAGE'):
                require_frame_coverage(frame('2026-06-26'), dates, '688361', allow_certified_suspension=True)
            for code in frozen:
                pd.testing.assert_frame_equal(ctx.snapshot_frames[code], frozen[code])


if __name__ == '__main__':
    unittest.main()
