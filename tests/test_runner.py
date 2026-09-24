import base64
from datetime import datetime
import io
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import private_store as store
import report
from runner import existing_result_outcome, prepare_native_with_retry, resolve_target


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = SimpleNamespace(sessions=('2026-09-18','2026-09-21','2026-09-22','2026-09-23','2026-09-24','2026-09-28'), coverage_start='2026-01-01',coverage_end='2026-12-31')
    def clock(self, hour):
        return datetime(2026,9,23,hour,1,tzinfo=ZoneInfo('Asia/Shanghai'))
    def test_preclose_never_becomes_today_signal(self):
        self.assertEqual(resolve_target(self.calendar,self.clock(10),'','schedule'),('2026-09-23','2026-09-22','BEFORE_CLOSE'))
    def test_deployment_acceptance_is_previous_completed_session(self):
        self.assertEqual(resolve_target(self.calendar,self.clock(10),'','push'),('2026-09-22','2026-09-21','READY'))
    def test_schedule_targets_today_after_close(self):
        self.assertEqual(resolve_target(self.calendar,self.clock(17),'','schedule'),('2026-09-23','2026-09-22','READY'))
    def test_future_rejected(self):
        with self.assertRaises(ValueError): resolve_target(self.calendar,self.clock(17),'2026-09-24','workflow_dispatch')
    def test_holiday_returns_actual_latest_session(self):
        now=datetime(2026,9,25,17,1,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(resolve_target(self.calendar,now,'','schedule'),('2026-09-25','2026-09-24','HOLIDAY'))


class NativePreparationRetryTests(unittest.TestCase):
    def test_late_index_and_risk_bars_retry_without_reserving_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            contexts, waits, stages = [], [], []
            def build():
                ctx = object()
                contexts.append(ctx)
                return ctx
            def before(ctx):
                stages.append(('index', ctx))
                if len(contexts) == 1:
                    raise ValueError('INDEX_EVIDENCE_UNAVAILABLE: TRADING_DAY_COVERAGE')
            def after(ctx):
                stages.append(('risk', ctx))
                if len(contexts) == 2:
                    raise ValueError('TRADING_DAY_COVERAGE:risk')
            with (Path(directory) / 'native.log').open('w', encoding='utf-8') as log:
                result = prepare_native_with_retry(build, None, log,
                    lambda ctx, load: True, lambda ctx: True, waits.append,
                    before=before, after=after)
            self.assertIs(result, contexts[2])
            self.assertEqual(waits, [600, 600])
            self.assertEqual(stages, [('index', contexts[0]), ('index', contexts[1]),
                ('risk', contexts[1]), ('index', contexts[2]), ('risk', contexts[2])])

    def test_delayed_bar_rebuilds_context_before_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            contexts, waits = [], []
            def build():
                ctx = object()
                contexts.append(ctx)
                return ctx
            def probe(ctx):
                if len(contexts) == 1:
                    print('TRADING_DAY_COVERAGE:300308', file=log)
                    return False
                return True
            with (Path(directory) / 'native.log').open('w', encoding='utf-8') as log:
                result = prepare_native_with_retry(build, None, log,
                    lambda ctx, load: True, probe, waits.append)
            self.assertIs(result, contexts[1])
            self.assertEqual(len(contexts), 2)
            self.assertEqual(waits, [600])

    def test_other_failure_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            contexts, waits = [], []
            def probe(ctx):
                print('risk state validation failed', file=log)
                return False
            with (Path(directory) / 'native.log').open('w', encoding='utf-8') as log:
                with self.assertRaisesRegex(ValueError, 'NATIVE_PREPARATION_FAILED'):
                    prepare_native_with_retry(lambda: contexts.append(object()), None,
                        log, lambda ctx, load: True, probe, waits.append)
            self.assertEqual(len(contexts), 1)
            self.assertEqual(waits, [])


class ExistingResultTests(unittest.TestCase):
    def test_completed_result_is_safe_to_republish_without_replay(self):
        for status in ('SUCCESS', 'DEGRADED'):
            self.assertEqual(existing_result_outcome({'status': status}),
                             'EXISTING_RESULT_' + status)

    def test_unfinished_or_failed_result_must_not_be_green(self):
        for status in ('RUNNING', 'FAILED', 'UNKNOWN'):
            with self.assertRaisesRegex(ValueError, 'EXISTING_RESULT_UNUSABLE'):
                existing_result_outcome({'status': status})


class EvidenceTests(unittest.TestCase):
    def test_public_branch_state_is_authenticated_and_path_bound(self):
        with patch.dict('os.environ', {'STATE_SEAL_KEY': 's' * 40}):
            value = {'status': 'RUNNING', 'run_id': '123'}
            sealed = store._seal(value, 'runs/2026-09-22.json.enc')
            self.assertEqual(store._open(sealed, 'runs/2026-09-22.json.enc'), value)
            self.assertNotIn(b'RUNNING', sealed)
            with self.assertRaises(ValueError):
                store._open(sealed, 'runs/2026-09-23.json.enc')
            tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
            with self.assertRaises(ValueError):
                store._open(tampered, 'runs/2026-09-22.json.enc')

    def test_exact_bundle_roundtrip_and_restore_allowlist(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as e:
            root=Path(d)
            for name, data in {'cache/x.csv':b'a,b\n1,2\n','output/risk_state.json':b'{"x":1}', 'output/signals_old.json':b'old','source/private.py':b'not allowed'}.items():
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            blob=store.pack(root)
            self.assertEqual(blob,store.pack(root))
            remote=store.decode_bundle(base64.b64encode(blob).decode(),store.digest(blob))
            self.assertEqual(remote,blob)
            store.restore(remote,Path(e))
            self.assertEqual((Path(e)/'cache/x.csv').read_bytes(),b'a,b\n1,2\n')
            self.assertFalse((Path(e)/'output/signals_old.json').exists())
            with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as archive:
                self.assertNotIn('source/private.py',archive.getnames())
    def test_corruption_rejected(self):
        with self.assertRaises(ValueError): store.decode_bundle(base64.b64encode(b'bad').decode(),'0'*64)
    def test_archive_traversal_rejected(self):
        out=io.BytesIO()
        with tarfile.open(fileobj=out,mode='w:gz') as ar:
            item=tarfile.TarInfo('../escape');item.size=1;ar.addfile(item,io.BytesIO(b'x'))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): store.restore(out.getvalue(),Path(d))
    def test_uncertain_upload_is_read_before_any_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'output').mkdir();(root/'output/a').write_bytes(b'x')
            blob=store.pack(root);r={'actions_run_id':'123'}
            saved={'run_id':'123','status':'SUCCESS','report':r,'bundle':base64.b64encode(blob).decode(),'sha256':store.digest(blob)}
            with patch('private_store.request',side_effect=[RuntimeError('unknown'),saved,saved]) as req:
                store.finish('2026-09-22','SUCCESS',r,'report',{},root)
                self.assertEqual([c.args[0] for c in req.call_args_list],['finish','result','result'])

    def test_transient_state_outage_retries_same_finished_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'output').mkdir();(root/'output/a').write_bytes(b'x')
            blob=store.pack(root);r={'actions_run_id':'123'}
            saved={'run_id':'123','status':'SUCCESS','report':r,
                   'bundle':base64.b64encode(blob).decode(),'sha256':store.digest(blob)}
            responses=[RuntimeError('write uncertain'), RuntimeError('read unavailable'),
                       {'saved':True}, saved]
            with patch('private_store.request',side_effect=responses) as req, \
                 patch('private_store.clock.sleep') as pause:
                proof=store.finish('2026-09-22','SUCCESS',r,'report',{},root)
            self.assertEqual([c.args[0] for c in req.call_args_list],
                             ['finish','result','finish','result'])
            pause.assert_called_once_with(5)
            self.assertEqual(proof['sha256'],store.digest(blob))


class ReportTests(unittest.TestCase):
    def test_native_rows_are_reordered_without_inventing_fields(self):
        pool={'2':'B','1':'A'}
        n={'symbols':pool,'signals':[{'code':'1','signal':'wait'},{'code':'2','signal':'buy'}]}
        obs=report.observation(n,pool,{})
        self.assertEqual([s['code'] for s in obs['symbols']],['2','1'])
        self.assertIsNone(obs['symbols'][0]['native'].get('target_weight'))
    def test_missing_or_duplicate_native_member_rejected(self):
        for rows in [[{'code':'1'}],[{'code':'1'},{'code':'1'}]]:
            with self.assertRaises(ValueError): report.observation({'symbols':{'1':'A','2':'B'},'signals':rows},{'1':'A','2':'B'},{})
    def test_no_baseline_is_not_no_change(self):
        self.assertEqual(report.compare({},None,'2026-09-21')['status'],'不可比较')
    def test_actual_action_change_and_zero_quantity_are_preserved(self):
        profile={'universe':['2','1']}
        old={'target_date':'2026-09-21','profile':profile,'strategy_sha':'a','observation':{'market':{},'symbols':[{'code':'1','native':{'signal':'wait'},'pending_signals':[],'blocked_signals':[]}]}}
        new={'profile':profile,'strategy_sha':'a','observation':{'market':{},'symbols':[{'code':'1','native':{'signal':'buy'},'pending_signals':[{'symbol':'1','quantity':0}], 'blocked_signals':[]}]}}
        changes=report.compare(new,old,'2026-09-21')
        self.assertEqual(changes['status'],'有变化')
        self.assertTrue(any(c['field']=='1.native.signal' and c['yesterday']=='wait' and c['today']=='buy' for c in changes['changes']))
        self.assertEqual(new['observation']['symbols'][0]['pending_signals'][0]['quantity'],0)

if __name__=='__main__': unittest.main()
