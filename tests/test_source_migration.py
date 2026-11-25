"""The approved one-way source transition and public version boundary."""
import os
import sys
import types
import unittest
from unittest.mock import patch

import private_store as store
import public_report
import runtime_profile as profile
import source_migration as migration
from test_publication import fixture


class MigrationBoundaryTests(unittest.TestCase):
    def test_historical_producer_readable_but_never_a_new_daily_writer(self):
        def record(sha):
            return {'simulation_identity': profile.INHERITED, 'strategy_sha': sha,
                    'state_origin': profile.STATE_ORIGIN.copy(),
                    'report': {'simulation_identity': profile.INHERITED,
                               'strategy_sha': sha, 'state_origin': profile.STATE_ORIGIN.copy()}}
        with patch.dict(os.environ, {'TRADE_SIMULATION_IDENTITY': profile.INHERITED}, clear=True), \
             patch('private_store._snapshot') as snapshot:
            old, new = profile.SEED_SOURCE_SHA, profile.SOURCE_SHA
            self.assertEqual(profile.STATE_ORIGIN['cutover_source'], old)
            profile.require_record(record(old))
            profile.require_record(record(new))
            with self.assertRaisesRegex(ValueError, 'INDEPENDENT_REPORT_IDENTITY'):
                profile.require_record({**record(old), 'report': record(new)['report']})
            with self.assertRaisesRegex(ValueError, 'INDEPENDENT_RECORD_IDENTITY'):
                profile.require_record(record('f' * 40))
            with self.assertRaisesRegex(ValueError, 'INDEPENDENT_SOURCE_IDENTITY'):
                store.request('start', {'date': '2026-10-09', 'strategy_sha': old})
            snapshot.assert_not_called()
            self.assertEqual(profile.bootstrap_source(), new)
            with patch.dict(os.environ, {'TRADE_SOURCE_PURPOSE': 'seed'}):
                self.assertEqual(profile.bootstrap_source(), old)
                with self.assertRaisesRegex(ValueError, 'INDEPENDENT_SOURCE_IDENTITY'):
                    profile.require_source(old)
                profile.require_seed_source(old)

    def test_proved_partial_star_parent_retained_without_fabricating_an_order(self):
        signal = dict(symbol='688361', strategy_name='dual_ma', direction='sell',
                      target_shares=100, signal_date='2026-10-08', reason='risk',
                      purpose='risk_reduction', risk_reason='guard', risk_scope='', fusion_votes=1)
        sleeve = {'name': 'fast', 'positions': [{'symbol': '688361', 'strategy_name': 'dual_ma', 'shares': 300}],
                  'trades': [
                      {'symbol': '688361', 'strategy_name': 'dual_ma', 'direction': 'buy', 'shares': 500, 'date': '2026-09-30'},
                      {**{k: v for k, v in signal.items() if k != 'target_shares'}, 'date': '2026-10-09', 'shares': 200},
                  ]}
        row = {**signal, 'strategy_name': 'fast:dual_ma', 'target_shares': 300}
        state = {'last_completed_close': '2026-10-09',
                 'controller': {'values': {'journal': [{'date': '2026-10-08', 'pending_signals': [row]}]}}}
        # Test the STAR quantity boundary independently of importing the
        # production package on the lightweight pre-bootstrap checks.
        rules = types.ModuleType('quantfusion.domain.rules')
        rules.sell_order_quantity = lambda code, qty, balance: balance if qty >= balance else qty if qty >= 200 else 0
        with patch.dict(sys.modules, {'quantfusion.domain.rules': rules}):
            self.assertEqual(migration._star_declaration(signal, sleeve, state), (300, 500))
            state['controller']['values']['journal'].clear()
            with self.assertRaisesRegex(ValueError, 'CHECKPOINT_MIGRATION_SELL_PROOF'):
                migration._star_declaration(signal, sleeve, state)
            # An unchanged a92 declaration newly made at the boundary is
            # revalidated by the current native open rules, not widened.
            self.assertEqual(migration._star_declaration(
                {**signal, 'signal_date': '2026-10-09'}, sleeve, state), (None, None))

    def test_public_v3_comparison_allows_many_valid_owned_intents(self):
        value = fixture()
        value['public_report_version'] = 3
        code = value['observation']['symbols'][0]['code']
        pending = [{'direction': 'sell', 'signal_date': '2026-09-21', 'purpose': 'risk_reduction',
                    'strategy_name': f'sleeve_{i}:turtle_breakout', 'blocked': False,
                    'executable': True, 'target_shares': 987654321} for i in range(9)]
        self.assertGreater(len(public_report.intents(pending, version=3)), 500)
        value['comparison'] = {'status': '有变化', 'changes': [
            {'field': f'{code}.pending_signals', 'yesterday': pending, 'today': []}]}
        document = public_report.markdown(value)
        self.assertIn('sleeve_8:turtle_breakout', document)
        self.assertNotIn('987654321', document)

    def test_public_v3_only_adds_allowed_market_and_intent_fields(self):
        value = fixture()
        original = public_report.markdown(value)
        market = value['observation']['market']
        market['major_damage_tactical'] = {'status': 'APPLIED', 'damage_depth': .40,
            'repair_fraction': .25, 'exposure_ceiling': .35, 'market_phase': True,
            'native_restrictions': [], 'private_notional': 'PRIVATE_SENTINEL'}
        market['held_structure_risk'] = {'status': 'CONFIRMED', 'weak_symbols': ['PRIVATE_SYMBOL'],
            'weak_equity_share': .7654321, 'covered_plan_shares_before_native_risk': {'PRIVATE_SYMBOL': 987654321}}
        market['sleeve_capital_reweight'] = {'cash_before': ['PRIVATE_CASH'], 'position_values': [987654321]}
        self.assertEqual(public_report.markdown(value), original)
        value['public_report_version'] = 3
        value['observation']['symbols'][0]['pending_signals'] = [
            {'direction': 'sell', 'risk_reason': 'held_structure_trim', 'target_shares': 987654321,
             'sell_parent_shares': 987654321, 'sell_position_shares': 987654321, 'reason': 'PRIVATE_REASON'},
            {'direction': 'sell', 'risk_reason': 'PRIVATE_UNKNOWN_REASON'}]
        document = public_report.markdown(value)
        for visible in ('深回撤修复战术状态', 'APPLIED', '纸面组合持仓结构风险状态', 'CONFIRMED', 'held_structure_trim'):
            self.assertIn(visible, document)
        for hidden in ('PRIVATE_', '987654321', '0.7654321', 'sell_parent_shares', 'sell_position_shares', 'cash_before'):
            self.assertNotIn(hidden, document)


if __name__ == '__main__':
    unittest.main()
