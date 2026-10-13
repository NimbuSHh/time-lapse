"""Pipeline persistence and failure tests with independent synthetic rendering."""
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from timelapse import automatic, workspace
from timelapse.photoshop import Cancelled
from tests.test_joint_feedback import render, sidecar


class Renderer:
    identity = 'test-independent-renderer-v1'
    calls = []
    def __init__(self, folder, cancelled):
        self.folder, self.cancelled = folder, cancelled
    def preflight(self):
        pass
    def render(self, raw, jpg, edge):
        self.calls.append((str(raw), edge))
        jpg.write_bytes(render(int(raw.stem[1:]), raw.with_suffix('.xmp').read_bytes()))


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'; self.source.mkdir()
        for i in range(12):
            (self.source/f'F{i:03}.ARW').write_bytes(f'raw-{i}'.encode())
            (self.source/f'F{i:03}.xmp').write_bytes(sidecar(i))
        self.output = workspace.output_root(self.source)
        self.before = automatic.inputs(self.source)[1]
        Renderer.calls = []
    def make(self, **kwargs):
        return automatic.Run(self.source, self.output, renderer_factory=Renderer, **kwargs)
    def test_full_pipeline_outputs_and_completed_resume(self):
        job = self.make()
        result = job.execute()
        self.assertEqual(result['count'], 12)
        self.assertEqual(len(list(Path(result['xmps']).glob('*.xmp'))), 12)
        self.assertEqual(len(list(Path(result['jpgs']).glob('*.jpg'))), 12)
        self.assertEqual(sum(edge == 0 for _, edge in Renderer.calls), 12)
        self.assertTrue(all(job.folder in Path(raw).parents for raw, _ in Renderer.calls))
        self.assertEqual(automatic.inputs(self.source)[1], self.before)
        calls = len(Renderer.calls)
        self.assertEqual(self.make().execute(), result)
        self.assertEqual(len(Renderer.calls), calls)
        next(Path(result['jpgs']).glob('*.jpg')).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '结果发生改变'):
            self.make().execute()
    def test_cancel_retains_receipts_and_resume_skips_completed_frames(self):
        job = self.make(cancelled=lambda: len(Renderer.calls) >= 2)
        with self.assertRaises(Cancelled): job.execute()
        self.assertEqual(job.state['state'], 'stopped')
        first = [raw for raw, _ in Renderer.calls]
        resumed = self.make()
        self.assertEqual(resumed.folder, job.folder)
        resumed.render_group('initial')
        self.assertEqual(len(Renderer.calls), 12)
        self.assertTrue(all(sum(raw == done for raw, _ in Renderer.calls) == 1 for done in first))
    def test_corrupt_render_and_changed_source_are_not_reused(self):
        job = self.make(); jpgs = job.render_group('initial')
        next(jpgs.glob('*.jpg')).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '已显影文件发生变化'): job.render_group('initial')
        (self.source/'F000.xmp').write_bytes(sidecar(0, (.1,-40,0)))
        with self.assertRaisesRegex(ValueError, '原素材或 XMP'): job.unchanged()
        self.assertNotEqual(self.make().folder, job.folder)
    def test_wrong_adobe_recipe_is_rejected_before_receipt(self):
        job = self.make()
        def wrong(raw, jpg, edge): jpg.write_bytes(render(int(raw.stem[1:]), sidecar(int(raw.stem[1:]), (.5,-40,0))))
        with patch.object(job.renderer, 'render', side_effect=wrong):
            with self.assertRaises(ValueError): job.render_group('initial')
        self.assertFalse(list((job.folder/'initial/receipts').glob('*.json')))
    def test_renderer_upgrade_starts_separate_run(self):
        first = self.make()
        with patch.object(Renderer, 'identity', 'test-independent-renderer-v2'):
            second = self.make()
        self.assertNotEqual(first.folder, second.folder)
        self.assertTrue((first.folder/'run.json').exists())
    def test_workspace_lock_prevents_simultaneous_runs(self):
        self.output.mkdir()
        with (self.output/'.automatic.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, '仍在运行'):
                automatic.run(self.source, self.output, renderer_factory=Renderer)
    def test_project_output_is_rejected_before_write(self):
        with self.assertRaisesRegex(ValueError, '程序项目目录'):
            automatic.run(self.source, workspace.PROJECT_ROOT/'outputs', renderer_factory=Renderer)
        self.assertEqual(workspace.output_root(self.output/'nested'), self.output)
        self.assertTrue(workspace.safe_child_output(self.output/'run', self.source))
        self.assertFalse(workspace.safe_child_output(self.source, self.source))

    def test_full_size_metadata_does_not_inherit_preview_size_limits(self):
        from timelapse.joint_feedback import match
        data = bytearray(render(0, sidecar(0)))
        sof = data.index(b'\xff\xc0')
        data[sof+5:sof+9] = (7000).to_bytes(2, 'big') + (10000).to_bytes(2, 'big')
        data = bytes(data) + b'\0' * (26*1024*1024)
        with self.assertRaises(ValueError): match(sidecar(0), data, 'preview')
        with patch('PIL.JpegImagePlugin.JpegImageFile.load', side_effect=AssertionError('Must not decode pixels')):
            match(sidecar(0), data, 'full-size', full_size=True)

    def test_solver_update_recomputes_failed_phase_without_losing_render_receipts(self):
        job=self.make(); rendered=job.render_group('initial'); calls=len(Renderer.calls)
        def result(name,ready):
            folder=job.folder/name; folder.mkdir(); (folder/'manifest.json').write_text('{}')
            return {'path':str(folder),'ready':ready}
        old=job.phase('calibration',lambda:result('old',False),version='old-solver')
        new=job.phase('calibration',lambda:result('new',True),version='new-solver')
        self.assertNotEqual(new['path'],old['path']); self.assertTrue(Path(old['path']).is_dir())
        self.assertEqual(job.state['superseded_phases'][0]['result'],old)
        job.render_group('initial'); self.assertEqual(len(Renderer.calls),calls)
        with self.assertRaisesRegex(ValueError,'完整候选不能静默替换'):
            job.phase('calibration',lambda:result('unsafe',True),version='future-solver')
        self.assertFalse((job.folder/'unsafe').exists())
