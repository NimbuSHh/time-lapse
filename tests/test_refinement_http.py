"""Isolated HTTP contract tests; refinement and Adobe execution are mocked."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from timelapse.server import App, handler


class RefinementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name))
        self.backend = types.ModuleType('timelapse.refinement')
        self.backend.run = Mock(return_value={'state': 'review-needed', 'verified': False})
        self.backend.load_latest = Mock(return_value=None)
        self.modules = patch.dict(sys.modules, {'timelapse.refinement': self.backend})
        self.modules.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.app.refine_cancel.set()
        self.server.shutdown()
        self.server.server_close()
        self.app.pool.shutdown()
        self.modules.stop()
        self.tmp.cleanup()

    def call(self, path, data=None, authorized=True, extra_headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'X-Timelapse-Token': self.app.token} if authorized else {}
        headers.update(extra_headers or {})
        if data is not None:
            headers['Content-Type'] = 'application/json'
        conn.request('GET' if data is None else 'POST', path,
                     None if data is None else json.dumps(data), headers)
        response = conn.getresponse()
        result = response.status, response.read(), response.headers
        conn.close()
        return result

    def wait_job(self, key):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            code, body, _ = self.call('/api/job?id=' + key)
            self.assertEqual(code, 200)
            job = json.loads(body)
            if job['state'] != 'running':
                return job
            time.sleep(.01)
        self.fail('Mock refinement did not finish')

    def test_independent_page_assets_do_not_require_or_embed_token(self):
        for path, mime in [('/refine', 'text/html'), ('/refine/', 'text/html'),
                           ('/refine.js', 'text/javascript'), ('/refine.css', 'text/css')]:
            code, body, headers = self.call(path, authorized=False)
            self.assertEqual(code, 200)
            self.assertTrue(headers['Content-Type'].startswith(mime))
            self.assertNotIn(self.app.token.encode(), body)
            self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertIn('四段小样验证，通过后自动继续810帧'.encode(), self.call('/refine')[1])
        self.assertIn(b'/app.js', self.call('/')[1])

    def test_status_start_cancel_keep_existing_authentication(self):
        for route, data in [('/api/refine/status', None), ('/api/refine/start', {}),
                            ('/api/refine/cancel', {})]:
            self.assertEqual(self.call(route, data, authorized=False)[0], 403)
        self.assertEqual(self.call('/api/refine/start', {}, extra_headers={'Origin': 'https://example.com'})[0], 403)
        self.backend.run.assert_not_called()

    def test_start_uses_current_workspace_and_preserves_candidate_state(self):
        self.backend.run.return_value = {'state': 'candidate', 'verified': False, 'message': '尚未通过验收'}
        code, body, _ = self.call('/api/refine/start', {'output': '/ignored/path'})
        self.assertEqual(code, 200)
        key = json.loads(body)['job']
        self.assertEqual(self.wait_job(key)['state'], 'done')
        args, kwargs = self.backend.run.call_args
        self.assertEqual(args, (self.app.work_output(),))
        self.assertTrue(callable(kwargs['progress']))
        self.assertFalse(kwargs['cancelled']())
        code, body, _ = self.call('/api/refine/status')
        status = json.loads(body)
        self.assertIsNone(status['job'])
        self.assertEqual(status['task']['state'], 'candidate')
        self.assertFalse(status['task']['result']['verified'])

    def test_progress_cancel_and_shared_job_exclusion(self):
        entered = threading.Event()
        def run(output, progress, cancelled):
            progress(3, 16, '四段小样显影')
            entered.set()
            deadline = time.monotonic() + 3
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(.01)
            return {'state': 'paused', 'verified': False}
        self.backend.run.side_effect = run
        code, body, _ = self.call('/api/refine/start', {})
        key = json.loads(body)['job']
        self.assertTrue(entered.wait(2))
        status = json.loads(self.call('/api/refine/status')[1])
        self.assertEqual(status['job'], key)
        self.assertEqual((status['task']['done'], status['task']['total']), (3, 16))
        self.assertEqual(status['task']['phase'], '四段小样显影')
        self.assertEqual(self.call('/api/refine/start', {})[0], 400)
        with self.assertRaisesRegex(ValueError, '上一项任务'):
            self.app.submit(lambda progress: None)
        self.assertEqual(self.call('/api/refine/cancel', {})[0], 200)
        self.assertEqual(self.wait_job(key)['result']['state'], 'paused')
        self.assertEqual(self.backend.run.call_count, 1)

    def test_existing_job_blocks_refinement_without_starting_it(self):
        self.app.jobs = {'other': {'state': 'running'}}
        self.assertEqual(self.call('/api/refine/start', {})[0], 400)
        self.backend.run.assert_not_called()

    def test_status_restores_saved_task_and_reports_worker_errors(self):
        self.backend.load_latest.return_value = {'state': 'review-needed', 'phase': '小样未通过',
                                                'result': {'verified': False}}
        status = json.loads(self.call('/api/refine/status')[1])
        self.assertEqual(status['task']['state'], 'review-needed')
        self.backend.run.side_effect = ValueError('来源校验失败')
        key = json.loads(self.call('/api/refine/start', {})[1])['job']
        self.assertEqual(self.wait_job(key)['state'], 'error')
        task = json.loads(self.call('/api/refine/status')[1])['task']
        self.assertEqual(task['state'], 'error')
        self.assertEqual(task['error'], '来源校验失败')


if __name__ == '__main__':
    unittest.main()
