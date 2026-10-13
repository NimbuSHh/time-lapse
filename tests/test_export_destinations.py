"""Exercise destination routing with disposable media and real package writes."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import os

from timelapse.demo import create
from timelapse.project import Project
from timelapse.server import App
from timelapse import backups


class ExportDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app_root = self.root / 'app'
        self.app_root.mkdir()
        self.app = App(self.app_root)
        self.addCleanup(self.app.pool.shutdown)
        paths = create(self.root / '素材 中文', 30)
        self.project = Project(**paths)
        self.project.calculate({'selected': [], 'start': .2, 'end': .2})
        self.app.project = self.project
        self.before = {p: p.read_bytes() for folder in (self.project.raw_dir, self.project.preview_dir)
                       for p in folder.iterdir() if p.is_file()}
        self.data = {'id': self.project.id, 'revision': self.project.result['revision']}

    def export(self, **extra):
        key = self.app.dispatch('/api/export', {**self.data, **extra})['job']
        deadline = time.monotonic() + 10
        while self.app.jobs[key]['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        task = self.app.jobs[key]
        self.assertEqual(task['state'], 'done', task)
        return task['result']

    def assert_unchanged(self):
        for path, data in self.before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_default_creates_distinct_source_subdirectories_with_exact_backups(self):
        for result in (self.export(), self.export(destination='source_subdir')):
            path = Path(result['path'])
            self.assertEqual(path.parent.parent, self.project.raw_dir)
            self.assertEqual(len(list((path / 'corrected').glob('*.xmp'))), 30)
            for frame in self.project.frames:
                self.assertEqual((path / 'originals' / frame.xmp.name).read_bytes(), frame.source.data)
                self.assertEqual((path / 'corrected' / frame.xmp.name).read_bytes(), frame.source.updated(self.project.result['final'][self.project.frames.index(frame)]))
        self.assertEqual(len(list(self.project.raw_dir.glob('timelapse-*'))), 2)
        self.assert_unchanged()
        self.assertFalse((self.app_root / 'outputs').exists())

    def test_custom_directory_contains_corrected_xmps_directly(self):
        custom = self.root / '自选 path'; custom.mkdir()
        sentinel = custom / 'existing.txt'; sentinel.write_text('keep')
        result = self.export(destination='custom', output_dir=str(custom))
        self.assertEqual(Path(result['path']), custom)
        self.assertEqual(result['layout'], 'direct')
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertEqual(len(list(custom.iterdir())), 31)
        self.assertTrue(all(p.is_file() for p in custom.iterdir()))
        for frame, value in zip(self.project.frames, self.project.result['final']):
            self.assertEqual((custom / frame.xmp.name).read_bytes(), frame.source.updated(value))
        self.assert_unchanged()

    def test_conflict_blocks_entire_direct_export_including_source_directory(self):
        custom = self.root / 'custom'; custom.mkdir()
        conflict = custom / self.project.frames[-1].xmp.name.upper()
        conflict.write_bytes(b'existing edit')
        for folder in (custom, self.project.raw_dir):
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, '同名'):
                self.project.export_direct(folder, self.data['revision'])
        self.assertEqual(list(custom.iterdir()), [conflict])
        self.assertEqual(conflict.read_bytes(), b'existing edit')
        self.assert_unchanged()

    def test_file_arriving_during_publish_is_preserved_and_partial_export_removed(self):
        custom = self.root / 'custom'; custom.mkdir()
        link = os.link
        last = custom / self.project.frames[-1].xmp.name
        def concurrent_file(source, target):
            if target == last:
                target.write_bytes(b'concurrent edit')
            return link(source, target)
        with patch('timelapse.project.os.link', side_effect=concurrent_file):
            with self.assertRaises(FileExistsError):
                self.project.export_direct(custom, self.data['revision'])
        self.assertEqual(list(custom.iterdir()), [last])
        self.assertEqual(last.read_bytes(), b'concurrent edit')
        self.assert_unchanged()

    def test_invalid_and_project_destinations_fail_before_writing(self):
        alias = self.root / 'alias'; alias.symlink_to(self.app_root, target_is_directory=True)
        child = self.app_root / 'child'; child.mkdir()
        for path in ('', 'relative', str(self.root / 'missing'), str(self.app_root), str(child), str(alias / 'child')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.app.dispatch('/api/export', {**self.data, 'destination': 'custom', 'output_dir': path})
        with self.assertRaisesRegex(ValueError, '未知'):
            self.app.dispatch('/api/export', {**self.data, 'destination': 'invalid'})
        self.assert_unchanged()
        self.assertEqual(list(child.iterdir()), [])

    def test_backup_requires_acr_confirmation_then_writes_and_restores(self):
        with self.assertRaisesRegex(ValueError, '关闭 ACR'):
            self.app.dispatch('/api/export', {**self.data, 'destination': 'source_backup'})
        self.assert_unchanged()
        result = self.export(destination='source_backup', acr_closed=True)
        self.assertTrue(self.project.written)
        for frame, value in zip(self.project.frames, self.project.result['final']):
            self.assertEqual(frame.xmp.read_bytes(), frame.source.updated(value))
            self.assertEqual((Path(result['backup_path']) / 'originals' / frame.xmp.name).read_bytes(), self.before[frame.xmp])
            self.assertEqual(frame.raw.read_bytes(), self.before[frame.raw])
        backups.restore(result['raw_dir'], result['backup_id'])
        self.assert_unchanged()
