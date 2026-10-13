"""Real JPEG/XMP I/O with independent synthetic renders, never Adobe ACR."""
from io import BytesIO
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from timelapse import analysis
from timelapse.feedback import build, target_hash
from timelapse.project import digest
from timelapse.server import App
from timelapse.xmp import parse
from tests.test_provenance import recipe


def rendered(brightness, xmp):
    # This transfer is known by the test, not calculated from result.prediction.
    linear = 2 ** brightness
    code = 12.92 * linear if linear <= .0031308 else 1.055 * linear ** (1 / 2.4) - .055
    out = BytesIO()
    Image.new('RGB', (96, 64), (round(code * 255),) * 3).save(out, format='JPEG', quality=100,
        xmp=xmp, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    return out.getvalue()


class FeedbackPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source, self.original, self.current, self.actual = [self.root / s for s in ('backup', 'original-jpg', 'current-xmp', 'actual-jpg')]
        for p in (self.source, self.original, self.current, self.actual, self.source / 'originals', self.source / 'corrected'):
            p.mkdir()
        self.n = 12
        self.scene = np.linspace(-5.5, -4.5, self.n)
        self.gain = np.linspace(1.3, 1.7, self.n)
        self.exposures = np.linspace(.4, .8, self.n)
        frames, backup, measures = [], [], []
        for i in range(self.n):
            stem = f'FRAME{i:03}'
            x = recipe(frame=stem)
            corrected = parse(x).updated(self.exposures[i])
            original_jpeg = rendered(self.scene[i], x)
            actual_jpeg = rendered(self.scene[i] + self.gain[i] * self.exposures[i] + .08 * self.exposures[i] ** 2, corrected)
            (self.source / 'originals' / (stem + '.xmp')).write_bytes(x)
            (self.source / 'corrected' / (stem + '.xmp')).write_bytes(corrected)
            (self.current / (stem + '.xmp')).write_bytes(corrected)
            (self.original / (stem + '.jpg')).write_bytes(original_jpeg)
            (self.actual / (stem + '.jpg')).write_bytes(actual_jpeg)
            frames.append({'name': stem + '.ARW', 'preview_sha256': digest(original_jpeg)})
            backup.append({'xmp': stem + '.xmp', 'original': digest(x), 'corrected': digest(corrected)})
            measures.append(analysis.measure(original_jpeg, [0, 0, 1, 1]))
        report = analysis.infer(measures, [0.] * self.n, [0.] * self.n, list(range(self.n)), 'manual')
        target = (np.asarray(report['measured']) + self.exposures).tolist()
        self.frozen = {'schema': 1, 'package_id': 'fixture', 'raw_dir': str(self.current), 'roi': [0, 0, 1, 1],
                       'dimensions': [96, 64], 'report': report, 'frames': frames,
                       'result': {'target': target, 'final': self.exposures.tolist(), 'settings': {'selected': []}}}
        (self.source / 'verification.json').write_text(json.dumps(self.frozen))
        (self.source / 'manifest.json').write_text(json.dumps({'schema': 2, 'state': 'applied', 'package_id': 'fixture', 'frames': backup}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, source=None):
        return build(source or self.source, self.original, self.current, self.actual, self.root / 'outputs')

    def test_two_round_export_preserves_goal_baseline_and_sources(self):
        before = {p: digest(p.read_bytes()) for d in (self.original, self.current, self.actual) for p in d.iterdir()}
        result = self.run_build()
        self.assertTrue(result['ready'])
        self.assertFalse(result['acr_verified'])
        folder = Path(result['path'])
        manifest = json.loads((folder / 'manifest.json').read_text())
        self.assertEqual(manifest['result']['target'], self.frozen['result']['target'])
        self.assertEqual(manifest['target_sha256'], target_hash(self.frozen['result']['target']))
        self.assertEqual(manifest['result']['blocked'], [])
        for p, sha in before.items():
            self.assertEqual(digest(p.read_bytes()), sha)
        # Apply to synthetic working copies only, then independently re-render.
        for i, f in enumerate(manifest['frames']):
            x = (folder / 'corrected' / f['xmp']).read_bytes()
            exposure = parse(x).exposure
            (self.current / f['xmp']).write_bytes(x)
            (self.actual / (Path(f['name']).stem + '.jpg')).write_bytes(rendered(self.scene[i] + self.gain[i] * exposure + .08 * exposure**2, x))
        second = self.run_build(folder)
        self.assertTrue(second['ready'])
        self.assertEqual(second['target_sha256'], result['target_sha256'])
        self.assertLess(second['rmse_before_ev'], .4 * result['rmse_before_ev'])
        m2 = json.loads((Path(second['path']) / 'manifest.json').read_text())
        self.assertEqual(m2['round'], 2)
        self.assertEqual(m2['result']['target'], self.frozen['result']['target'])
        for f in m2['frames']:
            self.assertEqual(f['baseline_exposure'], 0.)

    def test_wrong_jpeg_identity_or_recipe_never_emits_xmp(self):
        for wrong in ('identity', 'recipe'):
            with self.subTest(wrong=wrong):
                path = self.actual / 'FRAME000.jpg'
                prior = path.read_bytes()
                x = recipe(exposure=str(self.exposures[0]), frame='other' if wrong == 'identity' else 'FRAME000',
                           contrast='15' if wrong == 'recipe' else '10')
                path.write_bytes(rendered(-4.7, x))
                result = self.run_build()
                self.assertFalse(result['ready'])
                self.assertEqual(result['audit_counts']['mismatch'], 1)
                self.assertFalse((Path(result['path']) / 'corrected').exists())
                path.write_bytes(prior)

    def test_missing_embedded_recipe_only_exports_diagnostic(self):
        (self.actual / 'FRAME000.jpg').write_bytes(rendered(-4.7, b''))
        result = self.run_build()
        self.assertFalse(result['ready'])
        self.assertEqual(result['audit_counts']['incomplete'], 1)
        self.assertFalse((Path(result['path']) / 'corrected').exists())

    def test_malformed_embedded_recipe_reports_conflict_without_crashing(self):
        (self.actual / 'FRAME000.jpg').write_bytes(rendered(-4.7, b'<not-closed'))
        result = self.run_build()
        self.assertFalse(result['ready'])
        self.assertEqual(result['audit_counts']['mismatch'], 1)
        self.assertFalse((Path(result['path']) / 'corrected').exists())

    def test_changed_snapshot_hash_is_detected(self):
        x = self.source / 'originals' / 'FRAME000.xmp'
        x.write_bytes(x.read_bytes() + b'\n')
        result = self.run_build()
        self.assertFalse(result['ready'])
        audit = json.loads((Path(result['path']) / 'source-audit.json').read_text())
        self.assertTrue(any(c.get('field') == 'sha256' for c in audit['frames'][0]['mismatches']))

    def test_target_corruption_and_path_traversal_are_rejected(self):
        result = self.run_build()
        folder = Path(result['path'])
        path = folder / 'manifest.json'
        data = json.loads(path.read_text())
        data['result']['target'][0] += .1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.run_build(folder)
        data['target_sha256'] = target_hash(data['result']['target'])
        data['frames'][0]['xmp'] = '../../unsafe.xmp'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, '文件名'):
            self.run_build(folder)

    def test_refuses_input_output_overlap_before_writing(self):
        with self.assertRaisesRegex(ValueError, '输出不能'):
            build(self.source, self.original, self.current, self.actual, self.current / 'outputs')
        self.assertFalse((self.current / 'outputs').exists())

    def test_source_changes_during_run_are_not_published(self):
        def mutate(i, n):
            if i == n:
                p = self.current / 'FRAME000.xmp'
                p.write_bytes(p.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            build(self.source, self.original, self.current, self.actual, self.root / 'outputs', progress=mutate)
        self.assertEqual(list((self.root / 'outputs').iterdir()), [])

    def test_api_feedback_is_independent_from_live_project(self):
        app = App(self.root)
        try:
            with patch.object(app, 'submit', side_effect=lambda fn: fn(None)):
                result = app.dispatch('/api/feedback', {'source': str(self.source), 'original_previews': str(self.original),
                    'current_xmps': str(self.current), 'previews': str(self.actual)})
            self.assertTrue(result['ready'])
            self.assertIsNone(app.project)
        finally:
            app.pool.shutdown()

    def saved_target(self, checkpoint=True):
        target = {**self.frozen, 'snapshot_id': 'analysis-fixture',
            'raw_dir': str(self.source / 'originals'), 'preview_dir': str(self.original),
            'public': {'frames': [{'exposure': 0.} for _ in range(self.n)]},
            'result': {**self.frozen['result'], 'revision': 'target-revision'}}
        target['frames'] = [{**f, 'xmp': Path(f['name']).stem + '.xmp'} for f in target['frames']]
        if checkpoint:
            target['checkpoint'] = {'version': 1, 'frames': []}
            for f in target['frames']:
                data = (self.source / 'originals' / f['xmp']).read_bytes()
                f['source_xmp_sha256'] = digest(data)
                target['checkpoint']['frames'].append({'xmp': base64.b64encode(data).decode()})
        path = self.root / 'verification-target.json'
        path.write_text(json.dumps({'target': target,
            'sha256': digest(json.dumps(target, sort_keys=True, allow_nan=False).encode())}))
        return path, target

    def test_analysis_target_needs_no_export_and_keeps_stored_original_xmp(self):
        source, target = self.saved_target()
        # Source sidecars may now contain a different working version; use the
        # recorded bytes, never silently redefine the original baseline.
        (self.source / 'originals' / 'FRAME000.xmp').write_bytes(b'changed')
        result = self.run_build(source)
        self.assertTrue(result['ready'])
        manifest = json.loads((Path(result['path']) / 'manifest.json').read_text())
        self.assertEqual(manifest['result']['target'], target['result']['target'])
        self.assertNotIn('checkpoint', manifest)
        self.assertEqual(parse((Path(result['path']) / 'baseline' / 'FRAME000.xmp').read_bytes()).exposure, 0.)

    def test_old_saved_target_can_use_audited_original_sidecars(self):
        source, _ = self.saved_target(checkpoint=False)
        self.assertTrue(self.run_build(source)['ready'])
        path = self.source / 'originals' / 'FRAME000.xmp'
        path.write_bytes(parse(path.read_bytes()).updated(.2))
        result = self.run_build(source)
        self.assertFalse(result['ready'])
        self.assertTrue(result['issues'])

    def test_analysis_api_binds_target_revision_without_export(self):
        path, target = self.saved_target()
        app = App(self.root)
        try:
            app.verification_target, app.target_path = target, path
            data = {'id': target['snapshot_id'], 'revision': target['result']['revision'],
                'source': '', 'original_previews': str(self.original),
                'current_xmps': str(self.current), 'previews': str(self.actual)}
            with patch.object(app, 'submit', side_effect=lambda fn: fn(None)):
                self.assertTrue(app.dispatch('/api/feedback', data)['ready'])
                with self.assertRaisesRegex(ValueError, '目标已失效'):
                    app.dispatch('/api/feedback', {**data, 'revision': 'stale'})
        finally:
            app.pool.shutdown()

    def test_inconsistent_frame_is_held_byte_exact_while_others_get_candidates(self):
        current = (self.current / 'FRAME000.xmp').read_bytes()
        (self.actual / 'FRAME000.jpg').write_bytes(rendered(self.scene[0], current))
        result = self.run_build()
        self.assertTrue(result['ready'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['blocked_count'], 1)
        self.assertGreater(result['changed_count'], 0)
        self.assertEqual((Path(result['path']) / 'corrected' / 'FRAME000.xmp').read_bytes(), current)
        self.assertEqual(result['issues'][0]['frame'], 1)
        self.assertIn('响应', result['issues'][0]['reason'])
        manifest = json.loads((Path(result['path']) / 'manifest.json').read_text())
        self.assertEqual(len(manifest['frames']), self.n)

    def test_already_within_noise_generates_no_pointless_candidate(self):
        source, target = self.saved_target()
        measurements = [analysis.measure((self.actual / (Path(f['name']).stem + '.jpg')).read_bytes(), [0,0,1,1]) for f in target['frames']]
        from timelapse.measurement import brightness
        target['result']['target'] = [brightness(m, target['report']) for m in measurements]
        source.write_text(json.dumps({'target': target, 'sha256': digest(json.dumps(target, sort_keys=True, allow_nan=False).encode())}))
        result = self.run_build(source)
        self.assertFalse(result['ready'])
        self.assertEqual(result['status'], 'no-changes')
        self.assertFalse((Path(result['path']) / 'corrected').exists())


if __name__ == '__main__':
    unittest.main()
