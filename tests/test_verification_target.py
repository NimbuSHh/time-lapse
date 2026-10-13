import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from timelapse.demo import create
from timelapse.project import Project, digest, verify_manifest
from timelapse.server import App


class VerificationTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.paths = create(self.root / 'demo', count=30)
        self.app = App(self.root)
        self.app.project = Project(**self.paths)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.pool.shutdown)

    def dispatch(self, route, data, app=None):
        app = app or self.app
        with patch.object(app, 'submit', side_effect=lambda task: task(None)):
            return app.dispatch(route, data)

    def calculate(self, **settings):
        return self.dispatch('/api/calculate', {'id': self.app.project.id,
                                               'settings': {'selected': [15], **settings}})

    def verify(self, result, app=None, **overrides):
        return self.dispatch('/api/verify', {'id': self.app.project.id,
            'revision': result['revision'], 'preview_dir': self.paths['preview_dir'], **overrides}, app)

    def test_analysis_without_export_survives_restart_and_keeps_sources_unchanged(self):
        before = {p: digest(p.read_bytes()) for p in (self.root / 'demo').rglob('*') if p.is_file()}
        result = self.calculate()
        self.assertEqual(self.app.project.exports, {})
        first = self.verify(result)
        fresh = App(self.root)
        self.addCleanup(fresh.pool.shutdown)
        self.assertIsNotNone(fresh.project)
        state = fresh.verification_state()
        self.assertTrue(state['editable'])
        self.assertEqual(state['target']['result'], result)
        self.assertEqual(state['target']['report'], self.app.project.report)
        self.assertEqual(first, self.verify(result, fresh))
        self.assertEqual(len(fresh.verification_images), 30)
        self.assertEqual(list((self.root / 'Time-lapse工作').iterdir()), [self.app.target_path])
        self.assertEqual(before, {p: digest(p.read_bytes()) for p in before})

    def test_restored_project_can_adjust_but_cannot_export_changed_sources(self):
        self.calculate()
        fresh = App(self.root)
        self.addCleanup(fresh.pool.shutdown)
        fresh.project.unchanged()
        result = self.dispatch('/api/calculate', {'id': fresh.project.id,
            'settings': {'selected': [15], 'strength': .8}}, fresh)
        self.assertEqual(result['settings']['strength'], .8)
        frame = fresh.project.frames[0]
        frame.xmp.write_bytes(frame.source.data + b'\n')
        with self.assertRaisesRegex(ValueError, '源文件已改变'):
            fresh.project.export(self.root / 'packages', result['revision'])

    def test_calibration_plan_survives_restart_and_target_adjustment(self):
        result = self.calculate()
        plan = self.dispatch('/api/exposure/prepare', {'id': self.app.project.id, 'revision': result['revision']})
        fresh = App(self.root)
        self.addCleanup(fresh.pool.shutdown)
        self.assertEqual(fresh.verification_state()['exposure_calibration']['calibration_id'], plan['calibration_id'])
        self.assertEqual(fresh.project.frames[0].measure['dimensions'], self.app.project.frames[0].measure['dimensions'])
        updated = self.dispatch('/api/calculate', {'id': fresh.project.id,
            'settings': {'selected': [15], 'strength': .8}}, fresh)
        self.assertEqual(fresh.verification_state()['exposure_calibration']['revision'], updated['revision'])
        self.assertIn(plan['calibration_id'], fresh.project.exposure_calibrations)

    def test_interrupted_apply_is_consumed_before_source_writes_and_after_restart(self):
        result = self.calculate()
        def interrupted(*args):
            saved = json.loads(self.app.target_path.read_text())
            self.assertTrue(saved['target']['checkpoint']['written'])
            raise OSError('interrupted write')
        with patch('timelapse.server.backups.apply', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted write'):
                self.dispatch('/api/apply', {'id': self.app.project.id, 'revision': result['revision'], 'acr_closed': True})
        fresh = App(self.root)
        self.addCleanup(fresh.pool.shutdown)
        self.assertTrue(fresh.verification_state()['written'])
        self.assertFalse(fresh.verification_state()['editable'])
        with self.assertRaisesRegex(ValueError, '写回'):
            self.dispatch('/api/calculate', {'id': fresh.project.id, 'settings': {}}, fresh)

    def test_old_goal_without_checkpoint_remains_reviewable(self):
        result = self.calculate()
        saved = json.loads(self.app.target_path.read_text())
        saved['target'].pop('checkpoint')
        saved['sha256'] = digest(json.dumps(saved['target'], sort_keys=True, allow_nan=False).encode())
        self.app.target_path.write_text(json.dumps(saved))
        fresh = App(self.root)
        self.addCleanup(fresh.pool.shutdown)
        self.assertIsNone(fresh.project)
        self.assertFalse(fresh.verification_state()['editable'])
        self.assertEqual(len(self.verify(result, fresh)['actual']), 30)

    def test_failed_prewrite_checkpoint_does_not_consume_live_snapshot(self):
        result = self.calculate()
        with patch('timelapse.server.os.replace', side_effect=OSError('disk full')), patch('timelapse.server.backups.apply') as apply:
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.dispatch('/api/apply', {'id': self.app.project.id, 'revision': result['revision'], 'acr_closed': True})
            apply.assert_not_called()
        self.assertFalse(self.app.project.written)
        self.assertTrue(self.app.verification_state()['editable'])
        self.calculate(strength=.7)

    def test_page_review_reports_uncertainty_without_marking_it_comparable(self):
        from PIL import Image
        result = self.calculate()
        frozen_bytes = self.app.target_path.read_bytes()
        frame = self.app.project.frames[0]
        with Image.open(frame.jpeg) as im:
            size = im.size
        Image.new('RGB', size, (0, 0, 0)).save(frame.jpeg, format='JPEG')
        checked = self.verify(result)
        self.assertEqual(checked['measurement_status'], 'needs-review')
        self.assertEqual(checked['comparable_frames'], 29)
        self.assertEqual(checked['uncertain_frames'][0]['frame'], 1)
        self.assertFalse(checked['measurement_checks'][0]['comparable'])
        self.assertEqual(len(self.app.verification_images), 30)
        self.assertEqual(frozen_bytes, self.app.target_path.read_bytes())
        with self.assertRaisesRegex(ValueError, '不可比'):
            verify_manifest(self.app.verification_target, self.paths['preview_dir'])

    def test_stale_page_cannot_verify_a_different_target(self):
        first = self.calculate()
        second = self.calculate(strength=.9)
        with self.assertRaisesRegex(ValueError, '目标已改变'):
            self.verify(first)
        self.verify(second)
        with self.assertRaisesRegex(ValueError, '目标已改变'):
            self.verify(second, id='other-sequence')

    def test_missing_analysis_and_directory_have_specific_errors(self):
        with self.assertRaisesRegex(ValueError, '尚无分析目标'):
            self.dispatch('/api/verify', {})
        result = self.calculate()
        with self.assertRaisesRegex(ValueError, '请选择 ACR'):
            self.verify(result, preview_dir='  ')
        with self.assertRaisesRegex(ValueError, '一一对应'):
            self.verify(result, preview_dir=str(self.root))

    def test_export_constraints_do_not_block_measurement(self):
        # A sequence with no camera response evidence still has a brightness target.
        self.app.project.report['events'] = []
        result = self.calculate(selected=[], start=.5, end=.5)
        self.assertTrue(result['calibration_required'])
        self.assertEqual(len(self.verify(result)['actual']), 30)
        with self.assertRaisesRegex(ValueError, '响应依据'):
            self.app.project.validate_write(result['revision'])
        # Recreate the normal camera-prior analysis with an exposure limit violation.
        self.app.project = Project(**self.paths)
        self.app.project.exposures = [4.9] * 30
        result = self.calculate(start=1, end=1)
        self.assertTrue(result['blocked'])
        self.assertEqual(len(self.verify(result)['actual']), 30)
        with self.assertRaisesRegex(ValueError, '安全范围'):
            self.app.project.validate_write(result['revision'])

    def test_failed_save_preserves_previous_durable_target(self):
        result = self.calculate()
        before = self.app.target_path.read_bytes()
        with patch('timelapse.server.os.replace', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.calculate(strength=.9)
        self.assertEqual(self.app.target_path.read_bytes(), before)
        self.assertEqual(self.app.verification_target['result'], result)
        self.assertFalse(self.app.verification_state()['editable'])
        self.assertFalse(list(self.app.target_path.parent.glob('.verification-*')))

    def test_corrupt_saved_target_reports_reason_without_breaking_startup(self):
        self.calculate()
        saved = json.loads(self.app.target_path.read_text())
        saved['target']['result']['target'][0] += 1
        self.app.target_path.write_text(json.dumps(saved))
        fresh = App(self.root)
        self.addCleanup(fresh.pool.shutdown)
        self.assertIsNone(fresh.verification_state()['target'])
        self.assertIn('校验失败', fresh.verification_state()['reason'])

    def test_direct_measurement_matches_exported_manifest(self):
        result = self.calculate()
        direct = self.verify(result)
        exported = self.app.project.export(self.root / 'packages', result['revision'])
        manifest = json.loads((Path(exported['path']) / 'manifest.json').read_text())
        expected = verify_manifest(manifest, self.paths['preview_dir'])
        expected.pop('_images')
        self.assertEqual(direct, expected)


if __name__ == '__main__':
    unittest.main()
