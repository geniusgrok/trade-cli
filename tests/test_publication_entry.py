"""公开发布使用仓库回读凭据，不扩展原件服务接口。"""
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

import public_report as report
import publish_report as publisher
from test_publication import fixture


class PublicationEntryTests(unittest.TestCase):
    def test_completed_publication_does_not_add_unsupported_store_event(self):
        value = fixture()
        saved = {'status': 'DEGRADED', 'bundle': 'unused', 'sha256': 'digest'}
        store = types.ModuleType('private_store')
        store.request = Mock(return_value=saved)
        store.decode_bundle = Mock(return_value=b'evidence')
        sessions = types.ModuleType('quantfusion.data.sessions')
        sessions.load_calendar = Mock(return_value=object())
        runner = types.ModuleType('runner')
        runner.resolve_target = Mock(return_value=('2026-09-22', '2026-09-21', 'READY'))
        modules = {'private_store': store, 'runner': runner,
                   'quantfusion': types.ModuleType('quantfusion'),
                   'quantfusion.data': types.ModuleType('quantfusion.data'),
                   'quantfusion.data.sessions': sessions}
        proof = {'path': 'reports/2026-09-22.md', 'sha256': 'a' * 64}
        with patch.dict(sys.modules, modules), patch.dict('os.environ', {
                'RUNNER_TEMP': '/tmp/test-publication', 'GITHUB_EVENT_NAME': 'push'}), \
             patch.object(sys, 'path', sys.path.copy()), \
             patch.object(publisher, 'verified_report', return_value=value), \
             patch.object(publisher, 'publish', return_value=proof) as publish, \
             patch('builtins.print') as output:
            publisher.main()
        store.request.assert_called_once_with('result', {'date': '2026-09-22'})
        publish.assert_called_once_with('2026-09-22', report.markdown(value))
        self.assertEqual(json.loads(output.call_args_list[0].args[0]), proof)


if __name__ == '__main__':
    unittest.main()
