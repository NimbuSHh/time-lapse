"""App lifecycle and frozen video-tool selection, with no real media or Adobe."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from timelapse import desktop, preview_video, workspace


class DesktopLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for module in (desktop, workspace):
            mocked = patch.object(module, 'PROJECT_ROOT', Path(temporary.name).resolve())
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_finder_working_directory_is_not_an_output_destination(self):
        service = desktop.DesktopService()
        try:
            self.assertFalse(service.app.workspace_explicit)
            self.assertIsNone(service.app.work_dir)
            self.assertEqual(service.app.root, workspace.PROJECT_ROOT)
            client = http.client.HTTPConnection('127.0.0.1', service.server.server_port, timeout=3)
            client.request('GET', '/api/config', headers={'X-Timelapse-Token': service.app.token})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertFalse(json.loads(response.read())['workspace_explicit'])
            client.close()
            for route in ('/api/auto/status', '/api/trajectory/status'):
                client = http.client.HTTPConnection('127.0.0.1', service.server.server_port, timeout=3)
                client.request('GET', route, headers={'X-Timelapse-Token': service.app.token})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {'task': None, 'job': None})
                client.close()
            with self.assertRaisesRegex(ValueError, '请先选择素材'):
                service.app.work_output()
        finally:
            service.close()

    def test_quit_cancels_only_owned_tasks_and_waits_for_progress_save(self):
        service = desktop.DesktopService()
        separate_task = threading.Event()
        saved = threading.Event()
        def worker():
            if service.app.trajectory_cancel.wait(3):
                saved.set()
        future = service.app.pool.submit(worker)
        service.close()
        self.assertTrue(saved.is_set())
        self.assertTrue(future.done())
        self.assertTrue(service.app.auto_cancel.is_set())
        self.assertTrue(service.app.refine_cancel.is_set())
        self.assertFalse(separate_task.is_set())
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(service.server.fileno(), -1)


class BundledToolTests(unittest.TestCase):
    def test_frozen_app_never_uses_external_tools_when_bundle_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp)/'timelapse/preview_video.py'
            with patch.object(preview_video, '__file__', str(module)), \
                 patch.object(preview_video.sys, 'frozen', True, create=True), \
                 patch.object(preview_video.shutil, 'which') as which:
                with self.assertRaisesRegex(ValueError, 'App 内置视频工具不完整'):
                    preview_video._tools()
                which.assert_not_called()

    def test_frozen_app_prefers_its_complete_executable_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            module = Path(temp)/'timelapse/preview_video.py'
            folder = module.parent/'bin'; folder.mkdir(parents=True)
            for name in ('ffmpeg', 'ffprobe'):
                path = folder/name; path.write_text('synthetic'); path.chmod(0o755)
            with patch.object(preview_video, '__file__', str(module)), \
                 patch.object(preview_video.sys, 'frozen', True, create=True), \
                 patch.object(preview_video.shutil, 'which') as which:
                self.assertEqual(preview_video._tools(), tuple(folder.resolve()/name for name in ('ffmpeg', 'ffprobe')))
                which.assert_not_called()
