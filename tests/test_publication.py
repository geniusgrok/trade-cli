"""公开日报边界、完整性和只补发不重算的定向验证。"""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import public_report as report
import publish_report as publisher


def fixture():
    symbols = [{'code': f'{i:06}', 'name': f'测试标的{i}',
                'native': {'signal': '观望', 'held_shares': 888888},
                'quote': {'date': '2026-09-22', 'close': 2.0},
                'pending_signals': [], 'blocked_signals': []} for i in range(17)]
    return {'target_date': '2026-09-22', 'previous_trading_date': '2026-09-21',
            'data_date': '2026-09-22', 'status': 'DEGRADED', 'strategy_sha': 'a' * 40,
            'actions_run_id': '12345', 'observation': {'market': {'summary': {'buys_suppressed': True},
                'portfolio': {'final_assets': 999999, 'terminal_risk_lock': False}}, 'symbols': symbols},
            'comparison': {'status': '不可比较', 'changes': []}}


def envelope(data):
    return {'type': 'file', 'encoding': 'base64', 'size': len(data),
            'sha': publisher.blob_id(data), 'content': base64.b64encode(data).decode()}


class PresentationTests(unittest.TestCase):
    def test_all_members_order_and_missing_fields(self):
        value = fixture()
        text = report.markdown(value)
        self.assertIn('降级（DEGRADED）', text)
        self.assertIn('不可比较', text)
        self.assertIn('未提供', text)
        self.assertEqual([text.index(f"| {i:06} | 测试标的{i}") for i in range(17)],
                         sorted(text.index(f"| {i:06} | 测试标的{i}") for i in range(17)))
        self.assertIn('688498 测试标的16', text)
        self.assertNotIn('888888', text)
        self.assertNotIn('999999', text)

    def test_private_fields_and_nested_intent_details_are_excluded(self):
        value = fixture()
        marker = 'TRADE_READ_TOKEN'
        value['error'] = marker
        value['observation']['market']['arbitrary'] = {'key': marker}
        value['observation']['symbols'][0]['pending_signals'] = [{'action': 'BUY', 'quantity': 999999,
                                                                 'account_id': marker, 'target_weight': .1}]
        text = report.markdown(value)
        self.assertNotIn(marker, text)
        self.assertNotIn('999999', text)
        self.assertIn('BUY', text)
        self.assertIn('目标仓位：0.1', text)

    def test_sensitive_allowed_values_rejected_without_echo(self):
        for marker in ['TRADE_READ_TOKEN', 'hello@example.com', 'https://private.example/report',
                       'Bearer abcde', '-----BEGIN PRIVATE KEY-----']:
            value = fixture()
            value['observation']['symbols'][0]['name'] = marker
            with self.subTest(marker=marker), self.assertRaises(ValueError) as caught:
                report.markdown(value)
            self.assertNotIn(marker, str(caught.exception))

    def test_comparison_never_leaks_private_fields(self):
        value = fixture()
        value['comparison'] = {'status': '有变化', 'changes': [
            {'field': 'market.portfolio.final_assets', 'yesterday': 'SECRET', 'today': 'PRIVATE'},
            {'field': '000000.native.signal', 'yesterday': '观望', 'today': 'BUY'}]}
        text = report.markdown(value)
        self.assertIn('观望 → BUY', text)
        self.assertNotIn('PRIVATE', text)
        self.assertIn('000000 测试标的0', text)
        self.assertNotIn('SECRET', text)

    def test_invalid_or_missing_universe_refused(self):
        value = fixture()
        value['observation']['symbols'].pop()
        with self.assertRaises(ValueError):
            report.markdown(value)

    def test_failed_result_has_no_fabricated_signals_or_error_details(self):
        value = fixture()
        value.update(status='FAILED', error='PRIVATE')
        value.pop('observation')
        text = report.markdown(value)
        self.assertIn('失败（FAILED）', text)
        self.assertIn('未取得完整', text)
        self.assertNotIn('PRIVATE', text)

    def test_nested_structure_not_silently_dumped(self):
        value = fixture()
        value['observation']['symbols'][0]['native']['eligibility'] = {'account_id': 'private'}
        with self.assertRaises(ValueError):
            report.markdown(value)

    def test_docs_include_hidden_documentation_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.github').mkdir()
            (root / '.github' / 'notes.md').write_text('TRADE_READ_TOKEN')
            with self.assertRaises(ValueError):
                report.check_docs(root)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.text = report.markdown(fixture())
        self.data = self.text.encode()
        self.head = 'b' * 40

    def test_create_then_immutable_and_main_readback(self):
        responses = [None, {'content': {'sha': publisher.blob_id(self.data)}, 'commit': {'sha': self.head}},
                     envelope(self.data), {'object': {'sha': self.head}}, envelope(self.data)]
        with patch.object(publisher, 'api', side_effect=responses) as api:
            proof = publisher.publish('2026-09-22', self.text)
        self.assertEqual(proof['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual([c.args[0] for c in api.call_args_list], ['GET', 'PUT', 'GET', 'GET', 'GET'])
        self.assertNotIn('sha', api.call_args_list[1].args[2])
        self.assertEqual(proof['bytes'], len(self.data))

    def test_existing_equal_does_not_write(self):
        with patch.object(publisher, 'api', side_effect=[envelope(self.data), {'object': {'sha': self.head}}, envelope(self.data)]) as api:
            publisher.publish('2026-09-22', self.text)
        self.assertTrue(all(c.args[0] == 'GET' for c in api.call_args_list))

    def test_existing_different_is_never_overwritten(self):
        with patch.object(publisher, 'api', return_value=envelope(b'different')) as api:
            with self.assertRaisesRegex(ValueError, 'EXISTING_PUBLIC_REPORT_DIFFERS'):
                publisher.publish('2026-09-22', self.text)
        self.assertEqual(api.call_count, 1)

    def test_uncertain_write_reads_before_any_retry(self):
        responses = [None, RuntimeError('timeout'), envelope(self.data), {'object': {'sha': self.head}}, envelope(self.data)]
        with patch.object(publisher, 'api', side_effect=responses) as api:
            publisher.publish('2026-09-22', self.text)
        self.assertEqual(sum(c.args[0] == 'PUT' for c in api.call_args_list), 1)

    def test_tampered_remote_bytes_are_rejected(self):
        bad = envelope(self.data)
        bad['sha'] = '0' * 40
        with patch.object(publisher, 'api', return_value=bad), self.assertRaises(ValueError):
            publisher.publish('2026-09-22', self.text)

    def test_saved_report_bound_to_original_bundle(self):
        value = fixture()
        out = io.BytesIO()
        raw = json.dumps(value).encode()
        with tarfile.open(fileobj=out, mode='w:gz') as archive:
            info = tarfile.TarInfo('output/daily_report.json')
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
        data = out.getvalue()
        saved = {'report': value, 'status': value['status'], 'run_id': value['actions_run_id'],
                 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        self.assertEqual(publisher.verified_report(saved, value['target_date'], data), value)
        changed = copy.deepcopy(saved)
        changed['report']['status'] = changed['status'] = 'SUCCESS'
        with self.assertRaises(ValueError):
            publisher.verified_report(changed, value['target_date'], data)

    def test_target_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            publisher.publish('../secret', self.text)


if __name__ == '__main__':
    unittest.main()
