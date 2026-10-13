"""Exposure-only calibration using independent synthetic JPEG renders, never ACR."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from timelapse.exposure_calibration import prepare, calibrate
from timelapse import analysis
from timelapse.measurement import brightness_bounds, response_bounds, VERSION, LEGACY_VERSION
from timelapse.project import Project, digest
from timelapse.server import App
from timelapse.xmp import parse
from tests.test_feedback_pipeline import rendered
from tests.test_provenance import recipe


class ExposureCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / 'raw'
        self.original = self.root / 'original-jpg'
        self.raw.mkdir()
        self.original.mkdir()
        self.n = 12
        # Midtones keep 8-bit JPEG quantization below the gain tolerance for a
        # 0.2 EV probe; dark constant-color tiles amplify one-code-value errors.
        self.scene = np.linspace(-3.5, -3.1, self.n) + np.array([.06 * (-1)**i for i in range(self.n)])
        # Independent synthetic render transfer, deliberately different from 1:1.
        self.gains = np.linspace(1.4, 1.8, self.n)
        self.make_project()

    def tearDown(self):
        self.tmp.cleanup()

    def make_project(self, exposure=0., offset=.35):
        for i in range(self.n):
            stem = f'FRAME{i:03}'
            xmp = recipe(exposure=str(exposure), frame=stem)
            (self.raw / (stem + '.ARW')).write_bytes(b'synthetic RAW placeholder ' + stem.encode())
            (self.raw / (stem + '.xmp')).write_bytes(xmp)
            (self.original / (stem + '.jpg')).write_bytes(rendered(self.scene[i], xmp))
        self.project = Project(self.raw, self.original)
        self.project.calculate({'selected': [], 'strength': 1., 'window': 5, 'start': offset, 'end': offset})
        self.assertEqual(self.project.report['events'], [])
        self.assertTrue(self.project.result['calibration_required'])

    def snapshot_sources(self):
        return {str(p): digest(p.read_bytes()) for folder in (self.raw, self.original) for p in folder.iterdir()}

    def pair(self, package=None):
        p = self.project
        self.revision = p.result['revision']
        package = package or prepare(p, self.root / 'calibrations', self.revision)
        self.package = package
        self.folder = Path(package['path'])
        self.baseline = self.root / ('baseline-' + package['calibration_id'])
        self.probe = self.root / ('probe-' + package['calibration_id'])
        self.baseline.mkdir()
        self.probe.mkdir()
        for i, frame in enumerate(p.frames):
            baseline = (self.folder / 'baseline' / frame.xmp.name).read_bytes()
            probe = (self.folder / 'probe-exposure' / frame.xmp.name).read_bytes()
            delta = parse(probe).exposure - parse(baseline).exposure
            (self.baseline / frame.jpeg.name).write_bytes(rendered(self.scene[i], baseline))
            (self.probe / frame.jpeg.name).write_bytes(rendered(self.scene[i] + self.gains[i] * delta, probe))
        return package

    def run_calibration(self, **kwargs):
        return calibrate(self.project, self.revision, self.package['calibration_id'], self.baseline, self.probe, **kwargs)

    def assert_rejected_without_mutation(self, **kwargs):
        result, response = copy.deepcopy(self.project.result), copy.deepcopy(self.project.exposure_response)
        with self.assertRaises(ValueError):
            self.run_calibration(**kwargs)
        self.assertEqual(self.project.result, result)
        self.assertEqual(self.project.exposure_response, response)

    def test_prepare_calibrate_export_verify_preserves_goal_and_source_files(self):
        p = self.project
        target = copy.deepcopy(p.result['target'])
        before = self.snapshot_sources()
        with self.assertRaises(ValueError):
            p.export(self.root / 'exports', p.result['revision'])
        package = self.pair()
        self.assertEqual(package['count'], self.n)
        self.assertTrue((self.folder / 'manifest.json').is_file())
        for frame in p.frames:
            original = frame.source
            baseline = (self.folder / 'baseline' / frame.xmp.name).read_bytes()
            probe = (self.folder / 'probe-exposure' / frame.xmp.name).read_bytes()
            self.assertEqual(baseline, original.data)
            self.assertEqual(probe, original.updated(original.exposure + .2))
        result = self.run_calibration()
        self.assertEqual(result, p.result)
        self.assertEqual(result['target'], target)
        self.assertFalse(result['calibration_required'])
        self.assertNotEqual(result['revision'], self.revision)
        self.assertEqual(result['response']['kind'], 'measured-exposure')
        self.assertGreater(result['response']['gain'], 1.3)
        np.testing.assert_allclose(result['response']['gains'], self.gains, atol=.15)
        np.testing.assert_allclose(result['delta'], np.asarray(result['brightness_delta']) / result['response']['gains'], atol=1e-10)
        exported = p.export(self.root / 'exports', result['revision'])
        manifest = json.loads((Path(exported['path']) / 'manifest.json').read_text())
        self.assertEqual(manifest['result']['target'], target)
        actual = self.root / 'corrected-jpg'
        actual.mkdir()
        for i, frame in enumerate(p.frames):
            corrected = (Path(exported['path']) / 'corrected' / frame.xmp.name).read_bytes()
            delta = parse(corrected).exposure - frame.source.exposure
            (actual / frame.jpeg.name).write_bytes(rendered(self.scene[i] + self.gains[i] * delta, corrected))
        verified = p.verify(actual, exported['package_id'])
        self.assertLess(verified['rmse_ev'], .04)
        self.assertEqual(self.snapshot_sources(), before)

    def test_calibration_reused_for_new_target_but_not_a_new_analysis(self):
        self.pair()
        old = self.run_calibration()
        result = self.project.calculate({'selected': [], 'strength': .3, 'window': 5, 'start': -.2, 'end': .1})
        self.assertNotEqual(result['target'], old['target'])
        self.assertFalse(result['calibration_required'])
        self.assertEqual(result['response']['gains'], old['response']['gains'])
        fresh = Project(self.raw, self.original)
        self.assertIsNone(fresh.exposure_response)
        self.assertTrue(fresh.calculate(result['settings'])['calibration_required'])

    def test_wrong_recipe_or_frame_identity_rejected_without_changing_preview(self):
        self.pair()
        path = self.probe / 'FRAME000.jpg'
        original = path.read_bytes()
        for changes in ({'contrast': '11'}, {'frame': 'WRONG000'}):
            with self.subTest(changes=changes):
                kwargs = {'frame': 'FRAME000', 'exposure': '.2', **changes}
                path.write_bytes(rendered(self.scene[0] + .3, recipe(**kwargs)))
                self.assert_rejected_without_mutation()
                path.write_bytes(original)

    def test_missing_embedded_recipe_rejected(self):
        self.pair()
        (self.probe / 'FRAME000.jpg').write_bytes(rendered(self.scene[0] + .3, b''))
        self.assert_rejected_without_mutation()

    def test_baseline_must_reproduce_original_brightness(self):
        self.pair()
        xmp = (self.folder / 'baseline' / 'FRAME000.xmp').read_bytes()
        (self.baseline / 'FRAME000.jpg').write_bytes(rendered(self.scene[0] + .2, xmp))
        self.assert_rejected_without_mutation()

    def test_no_response_or_negative_or_out_of_range_response_rejected(self):
        self.pair()
        xmp = (self.folder / 'probe-exposure' / 'FRAME000.xmp').read_bytes()
        for change in (0., -.3, 1.):
            with self.subTest(change=change):
                (self.probe / 'FRAME000.jpg').write_bytes(rendered(self.scene[0] + change, xmp))
                self.assert_rejected_without_mutation()

    def test_missing_frame_rejected(self):
        self.pair()
        (self.probe / 'FRAME000.jpg').unlink()
        self.assert_rejected_without_mutation()

    def test_stale_target_and_stale_prepare_revision_rejected(self):
        self.pair()
        self.project.calculate({'selected': [], 'strength': .5, 'window': 5, 'start': -.1, 'end': .1})
        self.assert_rejected_without_mutation()
        with self.assertRaises(ValueError):
            prepare(self.project, self.root / 'unused', self.revision)
        self.assertFalse((self.root / 'unused').exists())

    def test_existing_calibration_pair_can_apply_to_new_target(self):
        self.pair()
        before = self.snapshot_sources()
        result = self.project.calculate({'selected': [], 'strength': .5, 'window': 5, 'start': -.1, 'end': .1})
        target = copy.deepcopy(result['target'])
        self.revision = result['revision']
        calibrated = self.run_calibration()
        self.assertEqual(calibrated['target'], target)
        self.assertFalse(calibrated['calibration_required'])
        self.assertEqual(calibrated['response']['calibration_id'], self.package['calibration_id'])
        self.assertEqual(self.snapshot_sources(), before)

    def bounded_measurements(self, radius):
        """Exercise calibration at the frozen-meter seam; JPEG/metadata remain real."""
        original = analysis.measure_for_report
        def measured(*args):
            result = original(*args)
            result['valid'] = [False] * 64
            result['tile_bounds'] = [[v - radius, v + radius] for v in result['tiles']]
            return result
        return patch('timelapse.exposure_calibration.analysis.measure_for_report', side_effect=measured)

    def test_small_measurement_intervals_allow_conservative_positive_and_negative_probes(self):
        for offset in (.35, -.35):
            with self.subTest(offset=offset):
                self.make_project(offset=offset)
                self.pair()
                with self.bounded_measurements(.001):
                    result = self.run_calibration()
                response = result['response']
                central_delta = (np.asarray(result['target']) - response['baseline_measured']) / response['gains']
                self.assertTrue(np.all(np.abs(result['delta']) < np.abs(central_delta)))
                self.assertTrue(np.all(np.asarray(result['delta']) * offset > 0))
                for entry, gain in zip(response['evidence'], response['gains']):
                    self.assertLess(entry['gain_bounds'][0], gain)
                    self.assertGreater(entry['gain_bounds'][1], gain)
                    self.assertAlmostEqual(entry['gain_bounds'][1] - entry['gain_bounds'][0], .02)
                    self.assertEqual(np.sign(entry['exposure_delta']), np.sign(offset))

    def test_target_inside_baseline_interval_preserves_original_exposure(self):
        self.pair()
        self.project.result = self.project.calculate({'selected': [], 'strength': 0., 'window': 5, 'start': 0., 'end': 0.})
        self.revision = self.project.result['revision']
        with self.bounded_measurements(.001):
            result = self.run_calibration()
        self.assertEqual(result['delta'], [0.] * self.n)
        self.assertEqual(result['final'], self.project.exposures)

    def test_wide_probe_interval_does_not_publish_calibration(self):
        self.pair()
        original = analysis.measure_for_report
        def measured(data, *args):
            result = original(data, *args)
            # Preserve the baseline-reproduction check; make only probe
            # evidence too wide to establish a positive response.
            from timelapse.provenance import jpeg_evidence
            if jpeg_evidence(data)['exposure'] != 0:
                result['valid'] = [False] * 64
                result['tile_bounds'] = [[v - .5, v + .5] for v in result['tiles']]
            return result
        previous = copy.deepcopy(self.project.result)
        with patch('timelapse.exposure_calibration.analysis.measure_for_report', side_effect=measured):
            with self.assertRaisesRegex(ValueError, '响应区间.*无法完整支持'):
                self.run_calibration()
        self.assertEqual(self.project.result, previous)
        self.assertIsNone(self.project.exposure_response)

    def test_invalid_legacy_support_reports_unknown_interval(self):
        self.project.report['measurement_contract']['version'] = LEGACY_VERSION
        self.pair()
        previous = copy.deepcopy(self.project.result)
        with self.bounded_measurements(.001):
            with self.assertRaisesRegex(ValueError, '旧版测光规则不能给出有效区间'):
                self.run_calibration()
        self.assertEqual(self.project.result, previous)
        self.assertIsNone(self.project.exposure_response)

    def test_target_changed_during_calibration_rejects_stale_publication(self):
        self.pair()
        latest = []
        def changed(done, total, stage):
            if done == total:
                latest.append(self.project.calculate({'selected': [], 'strength': .5, 'window': 5, 'start': -.1, 'end': .1}))
        with self.assertRaisesRegex(ValueError, '当前预览已失效'):
            self.run_calibration(progress=changed)
        self.assertEqual(self.project.result, latest[0])
        self.assertIsNone(self.project.exposure_response)

    def test_changed_sources_rejected(self):
        self.pair()
        for path in (self.raw / 'FRAME000.xmp', self.original / 'FRAME000.jpg', self.raw / 'FRAME000.ARW'):
            with self.subTest(path=path.name):
                content = path.read_bytes()
                path.write_bytes(content + b'changed')
                self.assert_rejected_without_mutation()
                path.write_bytes(content)
                # A restored RAW still has a different mtime, so it is last.

    def test_modified_calibration_package_rejected(self):
        self.pair()
        for path in (self.folder / 'baseline' / 'FRAME000.xmp', self.folder / 'probe-exposure' / 'FRAME000.xmp', self.folder / 'manifest.json'):
            with self.subTest(path=str(path.relative_to(self.folder))):
                content = path.read_bytes()
                path.write_bytes(content + b'\n')
                self.assert_rejected_without_mutation()
                path.write_bytes(content)

    def test_written_project_cannot_prepare_or_apply_calibration(self):
        self.pair()
        self.project.written = True
        self.assert_rejected_without_mutation()
        with self.assertRaises(ValueError):
            prepare(self.project, self.root / 'unused', self.revision)
        self.assertFalse((self.root / 'unused').exists())

    def test_probe_direction_follows_target_and_reverses_at_exposure_limits(self):
        for exposure, offset, expected in ((0., -.2, -.2), (5., .2, 4.8), (-5., -.2, -4.8)):
            with self.subTest(exposure=exposure, offset=offset):
                self.make_project(exposure=exposure, offset=offset)
                self.pair()
                self.assertAlmostEqual(parse((self.folder / 'probe-exposure' / 'FRAME000.xmp').read_bytes()).exposure, expected)
                result = self.run_calibration()
                self.assertFalse(result['calibration_required'])
                if exposure:
                    self.assertTrue(result['blocked'])
                    with self.assertRaises(ValueError):
                        self.project.export(self.root / 'exports', result['revision'])

    def test_source_changed_during_calibration_does_not_publish_partial_response(self):
        self.pair()
        def changed(done, total, stage):
            if done == total:
                source = self.raw / 'FRAME000.xmp'
                source.write_bytes(source.read_bytes() + b'\n')
        self.assert_rejected_without_mutation(progress=changed)

    def test_failed_recalibration_keeps_existing_measured_response(self):
        self.pair()
        self.run_calibration()
        self.pair()
        (self.probe / 'FRAME000.jpg').write_bytes(rendered(self.scene[0], b''))
        self.assert_rejected_without_mutation()
        self.assertEqual(self.project.result['response']['kind'], 'measured-exposure')

    def test_api_prepare_calibrate_export_and_stale_snapshot(self):
        # The app directory is separate from user media and exported packages.
        app = App(self.root / 'app')
        app.project = self.project
        p = self.project
        try:
            with patch.object(app, 'submit', side_effect=lambda fn: fn(None)):
                for route in ('/api/exposure/prepare', '/api/exposure/calibrate'):
                    with self.subTest(route=route), self.assertRaises(ValueError):
                        app.dispatch(route, {'id': 'stale-snapshot', 'revision': p.result['revision']})
                package = app.dispatch('/api/exposure/prepare', {'id': p.id, 'revision': p.result['revision']})
                self.pair(package)
                old_revision = self.revision
                result = app.dispatch('/api/exposure/calibrate', {'id': p.id, 'revision': self.revision,
                    'calibration_id': package['calibration_id'], 'baseline_previews': str(self.baseline),
                    'exposure_previews': str(self.probe)})
                self.assertFalse(result['calibration_required'])
                with self.assertRaises(ValueError):
                    app.dispatch('/api/export', {'id': p.id, 'revision': old_revision})
                exported = app.dispatch('/api/export', {'id': p.id, 'revision': result['revision']})
                self.assertEqual(exported['count'], self.n)
                self.assertEqual(len(list((Path(exported['path']) / 'corrected').glob('*.xmp'))), self.n)
        finally:
            app.pool.shutdown()

    def test_prepare_refuses_output_inside_sources(self):
        for source in (self.raw, self.original):
            with self.subTest(source=source.name), self.assertRaises(ValueError):
                prepare(self.project, source / 'calibrations', self.project.result['revision'])
            self.assertFalse((source / 'calibrations').exists())


class MeasurementIntervalTests(unittest.TestCase):
    def test_exact_bounded_and_unknown_legacy_are_distinct(self):
        measure = {'tiles': [-3.], 'valid': [True], 'weights': [1024], 'tile_bounds': [[-3.01, -2.99]]}
        report = {'common_tiles': [0], 'measurement_contract': {'version': VERSION}}
        self.assertEqual(brightness_bounds(measure, report), [-3., -3.])
        measure['valid'] = [False]
        self.assertEqual(brightness_bounds(measure, report), [-3.01, -2.99])
        report['measurement_contract']['version'] = LEGACY_VERSION
        self.assertIsNone(brightness_bounds(measure, report))
        self.assertIsNone(response_bounds(None, [-2.5, -2.4], .2))

    def test_response_interval_handles_both_probe_directions(self):
        np.testing.assert_allclose(response_bounds([-3.01, -2.99], [-2.81, -2.79], .2), [.9, 1.1])
        np.testing.assert_allclose(response_bounds([-3.01, -2.99], [-3.21, -3.19], -.2), [.9, 1.1])
        with self.assertRaises(ValueError):
            response_bounds([-3., -3.], [-2.8, -2.8], 0.)


if __name__ == '__main__':
    unittest.main()
