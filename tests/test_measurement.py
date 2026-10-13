"""Independent JPEG fixtures for ROI geometry and frozen-support regression tests."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from timelapse import analysis
from timelapse.demo import create
from timelapse.project import Project, digest, verify_manifest


ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


def jpeg(codes):
    codes = np.asarray(codes, dtype=np.uint8)
    if codes.ndim == 2:
        codes = np.repeat(codes[..., None], 3, axis=2)
    output = BytesIO()
    Image.fromarray(codes).save(output, format='JPEG', quality=100, subsampling=0, icc_profile=ICC)
    return output.getvalue()


def linear_jpeg(ev, shape=(256, 256)):
    linear = 2. ** np.asarray(ev)
    codes = np.where(linear <= .0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - .055)
    return jpeg(np.broadcast_to(np.rint(codes * 255), shape))


def run_meter(images, roi=(0., 0., 1., 1.)):
    from timelapse.measurement import SequenceMeter
    with SequenceMeter(roi) as meter:
        for data in images:
            meter.add(data)
        return meter.finish()


class MeasurementGeometryTests(unittest.TestCase):
    def test_large_roi_mixed_tiles_and_small_clean_roi_both_retain_signal(self):
        # Most old tiles contain 25% clipped pixels, despite ample stable
        # midtone support. A 96px clean island leaves only two old tiles valid.
        codes = np.full((512, 512), 96, dtype=np.uint8)
        codes[:, np.arange(512) % 64 < 16] = 255
        codes[208:304, 208:304] = 96
        image = jpeg(codes)
        self.assertLess(sum(analysis.measure(image, [0, 0, 1, 1])['valid']), 8)
        large, large_diagnostics, large_contract = run_meter([image] * 5)
        small, small_diagnostics, small_contract = run_meter([image] * 5, (208 / 512, 208 / 512, 304 / 512, 304 / 512))
        self.assertTrue(large_diagnostics['passed'])
        self.assertTrue(small_diagnostics['passed'])
        self.assertGreater(large_diagnostics['support']['pixels'], small_diagnostics['support']['pixels'])
        self.assertLess(large_diagnostics['support']['coverage'], small_diagnostics['support']['coverage'])
        for measures, diagnostic, contract in ((large, large_diagnostics, large_contract), (small, small_diagnostics, small_contract)):
            report = analysis.infer(measures, [0.] * 5, [0.] * 5, list(range(5)), 'manual', measurement=diagnostic, contract=contract)
            expected = np.log2(((96 / 255 + .055) / 1.055) ** 2.4)
            np.testing.assert_allclose(report['measured'], expected, atol=.015)

    def test_uniform_two_ev_change_is_not_removed_as_temporal_instability(self):
        levels = np.linspace(-5., -3., 9)
        images = [linear_jpeg(v) for v in levels]
        measures, diagnostic, contract = run_meter(images)
        report = analysis.infer(measures, [0.] * 9, [0.] * 9, list(range(9)), 'manual', measurement=diagnostic, contract=contract)
        np.testing.assert_allclose(report['measured'], levels, atol=.025)
        self.assertGreater(report['measured'][-1] - report['measured'][0], 1.95)

    def test_single_pixel_roi_cannot_create_independent_samples_by_resizing(self):
        image = linear_jpeg(-4., (1001, 1501))
        # A one-source-pixel crop must fail irrespective of its normalized
        # area or the fact it could be enlarged and split into 64 grid cells.
        with self.assertRaises(ValueError):
            run_meter([image] * 5, (.3, .3, .3 + 1 / 1501, .3 + 1 / 1001))

    def test_pixel_budget_can_fail_while_coverage_and_time_stability_pass(self):
        from timelapse.measurement import MeasurementError
        image = linear_jpeg(-4.)
        with self.assertRaises(MeasurementError) as caught:
            run_meter([image] * 5, (0., 0., 30 / 256, 30 / 256))
        diagnostics = caught.exception.measurement
        checks = {item['key']: item for item in diagnostics['checks']}
        self.assertIsNone(checks['coverage']['passed'])
        self.assertIsNone(checks['stability']['passed'])
        self.assertEqual(checks['stability']['kind'], 'diagnostic')
        self.assertFalse(checks['pixels']['passed'])
        self.assertEqual(checks['pixels']['value'], 900)

    def test_low_area_ratio_does_not_reject_enough_fixed_pixels(self):
        from timelapse.measurement import measure_frozen, brightness
        codes = np.zeros((256, 256), dtype=np.uint8)
        codes[:16] = 96
        image = jpeg(codes)
        measures, diagnostic, contract = run_meter([image] * 5)
        self.assertTrue(diagnostic['passed'])
        checks = {item['key']: item for item in diagnostic['checks']}
        self.assertIsNone(checks['coverage']['passed'])
        self.assertEqual(checks['coverage']['kind'], 'diagnostic')
        self.assertEqual(checks['coverage']['value'], .0625)
        self.assertTrue(checks['pixels']['passed'])
        self.assertEqual(checks['pixels']['value'], 4096)
        report = analysis.infer(measures, [0.] * 5, [0.] * 5, list(range(5)), 'manual', measurement=diagnostic, contract=contract)
        self.assertEqual(brightness(measure_frozen(image, [0, 0, 1, 1], contract), report), report['measured'][0])
        frozen_v2 = deepcopy(contract)
        frozen_v2['version'] = 'roi-fixed-support-v2'
        with self.assertRaisesRegex(ValueError, '冻结像素支持不足'):
            measure_frozen(image, [0, 0, 1, 1], frozen_v2)

    def test_existing_v2_frozen_contract_keeps_exact_brightness(self):
        from timelapse.measurement import measure_frozen
        image = linear_jpeg(-4.)
        _, _, contract = run_meter([image] * 5)
        old = deepcopy(contract)
        old['version'] = 'roi-fixed-support-v2'
        self.assertEqual(measure_frozen(image, [0, 0, 1, 1], old), measure_frozen(image, [0, 0, 1, 1], contract))


    def test_sparse_cells_do_not_outvote_larger_fixed_support(self):
        codes = np.zeros((256, 256), dtype=np.uint8)
        codes[:64] = 96  # 16 cells, 16384 pixels
        for row in range(2, 8):
            for col in range(8):
                codes[row * 32:row * 32 + 8, col * 32:col * 32 + 8] = 150
        measures, diagnostic, contract = run_meter([jpeg(codes)] * 5)
        report = analysis.infer(measures, [0.] * 5, [0.] * 5, list(range(5)), 'manual', measurement=diagnostic, contract=contract)
        expected = np.log2(((96 / 255 + .055) / 1.055) ** 2.4)
        np.testing.assert_allclose(report['measured'], expected, atol=.015)
        self.assertGreater(np.median(measures[0]['tiles']), expected + 1.)

    def test_noninteger_nested_rois_use_identical_original_pixel_lattice(self):
        from timelapse.measurement import decode
        yy, xx = np.mgrid[:1001, :1501]
        image = jpeg(40 + ((xx * 7 + yy * 13) % 140))
        full = decode(image, [0, 0, 1, 1])
        subset = decode(image, [.123, .173, .777, .839])
        geometry = subset[-1]
        self.assertEqual(geometry['stride'], 3)
        left, top, right, bottom = geometry['box']
        xs = np.arange(0, 1501, 3)
        ys = np.arange(0, 1001, 3)
        expected = full[0][np.ix_((ys >= top) & (ys < bottom), (xs >= left) & (xs < right))]
        np.testing.assert_array_equal(subset[0], expected)
        self.assertEqual(geometry['shape'], list(expected.shape))
        self.assertLess(expected.size, geometry['roi_pixels'])


class MeasurementDiagnosticsTests(unittest.TestCase):
    def test_moving_clipped_patch_uses_union_of_excluded_pixels_and_names_frames(self):
        from timelapse.measurement import measure_frozen
        plain = np.full((256, 256), 96, dtype=np.uint8)
        first, second = plain.copy(), plain.copy()
        first[:32, :32] = 255
        second[:32, 32:64] = 255
        images = [jpeg(first), jpeg(second), jpeg(plain), jpeg(plain), jpeg(plain)]
        measures, diagnostics, contract = run_meter(images)
        self.assertEqual(diagnostics['support']['pixels'], 256 * 256 - 2 * 32 * 32)
        cells = diagnostics['cells']
        self.assertEqual(cells[0]['clipped_frames'], [1])
        self.assertEqual(cells[1]['clipped_frames'], [2])
        self.assertEqual(cells[0]['dark_frames'], [])
        self.assertEqual(diagnostics['frames'][0]['failed_cells'], [0])
        self.assertEqual(diagnostics['frames'][1]['failed_cells'], [1])
        self.assertEqual(diagnostics['frames'][2]['failed_cells'], [])
        for data, measured in zip(images, measures):
            self.assertEqual(measure_frozen(data, [0, 0, 1, 1], contract)['weights'], measured['weights'])

    def test_one_channel_clipping_is_rejected_even_with_midtone_luminance(self):
        codes = np.full((256, 256, 3), 96, dtype=np.uint8)
        codes[:64, :] = (255, 40, 40)
        _, diagnostics, _ = run_meter([jpeg(codes)] * 5)
        self.assertAlmostEqual(diagnostics['support']['coverage'], .75)
        self.assertEqual(diagnostics['cells'][0]['clipped_frames'], [1, 2, 3, 4, 5])
        self.assertEqual(diagnostics['cells'][0]['dark_frames'], [])
        self.assertTrue(all(not c['selected'] for c in diagnostics['cells'][:16]))

    def test_local_midtone_change_marks_both_adjacent_frames_without_rejecting_pixels(self):
        plain = np.full((256, 256), 96, dtype=np.uint8)
        left, right = plain.copy(), plain.copy()
        left[:32, :32] = 160
        right[:32, 32:64] = 160
        images = [jpeg(plain), jpeg(left), jpeg(right), jpeg(plain), jpeg(plain)]
        _, diagnostics, _ = run_meter(images)
        cells = diagnostics['cells']
        self.assertEqual(cells[0]['variation_frames'], [1, 2, 3])
        self.assertEqual(cells[1]['variation_frames'], [2, 3, 4])
        self.assertTrue(cells[0]['selected'])
        self.assertTrue(cells[1]['selected'])
        self.assertTrue(cells[2]['selected'])
        self.assertEqual(diagnostics['frames'][1]['variation_cells'], [0, 1])
        self.assertEqual(cells[0]['clipped_frames'], [])
        self.assertEqual(cells[0]['dark_frames'], [])
        self.assertTrue(diagnostics['passed'])
        self.assertEqual(diagnostics['support']['pixels'], 256 * 256)
        self.assertEqual(diagnostics['support']['temporal_retention'], 1.)

    def test_lost_candidate_pixels_do_not_reject_sufficient_fixed_support(self):
        from timelapse.measurement import MeasurementError
        plain = np.full((256, 256), 96, dtype=np.uint8)
        first, second = plain.copy(), plain.copy()
        first[:64, :] = 255
        second[64:128, :] = 255
        measures, diagnostic, contract = run_meter([jpeg(first), jpeg(second), jpeg(first), jpeg(second), jpeg(first)])
        self.assertTrue(diagnostic['passed'])
        report = analysis.infer(measures, [0.] * 5, [0.] * 5, list(range(5)), 'manual', measurement=diagnostic, contract=contract)
        expected = np.log2(((96 / 255 + .055) / 1.055) ** 2.4)
        np.testing.assert_allclose(report['measured'], expected, atol=.015)
        checks = {c['key']: c for c in diagnostic['checks']}
        self.assertIsNone(checks['coverage']['passed'])
        self.assertTrue(checks['pixels']['passed'])
        self.assertIsNone(checks['stability']['passed'])
        self.assertEqual(checks['stability']['kind'], 'diagnostic')
        self.assertAlmostEqual(checks['stability']['value'], 2 / 3)
        self.assertEqual(diagnostic['support']['best_frame_pixels'], 256 * 192)
        self.assertEqual(diagnostic['support']['pixels'], 256 * 128)
        self.assertEqual(diagnostic['cells'][0]['clipped_frames'], [1, 3, 5])
        self.assertEqual(diagnostic['cells'][16]['clipped_frames'], [2, 4])

    def test_50_point_8_percent_retention_keeps_identical_measurement_basis(self):
        from timelapse.measurement import measure_frozen, brightness
        plain = np.full((512, 512), 96, dtype=np.uint8)
        clipped = plain.copy()
        clipped[:, :252] = 255
        images = [jpeg(plain), jpeg(clipped), jpeg(plain), jpeg(clipped), jpeg(plain)]
        measures, diagnostic, contract = run_meter(images)
        self.assertTrue(diagnostic['passed'])
        self.assertAlmostEqual(diagnostic['support']['temporal_retention'], .508, delta=.002)
        report = analysis.infer(measures, [0.] * 5, [0.] * 5, list(range(5)), 'manual', measurement=diagnostic, contract=contract)
        for image, measured in zip(images, measures):
            self.assertEqual(measured['weights'], contract['weights'])
            frozen = measure_frozen(image, [0, 0, 1, 1], contract)
            self.assertEqual(brightness(frozen, report), brightness(measured, report))
        np.testing.assert_allclose(report['measured'], report['measured'][0], atol=.015)

    def test_static_scene_nonlinear_tone_response_marks_variation_but_preserves_signal(self):
        # The scene never moves. A fixed tone curve has different gain in its
        # dark and bright regions when the exposure increases by one EV.
        scene = np.full((256, 256), .03)
        scene[:64] = .5
        exposures = [1., 1., 2., 2., 2.]
        images, expected = [], []
        for exposure in exposures:
            linear = scene * exposure
            rendered = linear / (1 + linear)
            images.append(linear_jpeg(np.log2(rendered)))
            expected.append(np.log2(.03 * exposure / (1 + .03 * exposure)))
        measures, diagnostic, contract = run_meter(images)
        self.assertTrue(diagnostic['passed'])
        self.assertEqual(diagnostic['support']['temporal_retention'], 1.)
        self.assertEqual(diagnostic['support']['pixels'], 256 * 256)
        self.assertEqual(diagnostic['cells'][0]['variation_frames'], [2, 3])
        self.assertTrue(diagnostic['cells'][0]['selected'])
        self.assertEqual(diagnostic['cells'][16]['variation_frames'], [])
        report = analysis.infer(measures, [0.] * 5, [0.] * 5, list(range(5)),
                                'manual', measurement=diagnostic, contract=contract)
        np.testing.assert_allclose(report['measured'], expected, atol=.025)

    def test_fully_white_and_black_report_separate_reasons_and_exact_frames(self):
        from timelapse.measurement import MeasurementError
        for code, reason, other in ((255, 'clipped_frames', 'dark_frames'), (0, 'dark_frames', 'clipped_frames')):
            with self.subTest(code=code), self.assertRaises(MeasurementError) as caught:
                run_meter([jpeg(np.full((256, 256), code))] * 5)
            diagnostics = caught.exception.measurement
            self.assertFalse(diagnostics['passed'])
            self.assertEqual(diagnostics['support']['pixels'], 0)
            self.assertEqual(diagnostics['support']['coverage'], 0.)
            self.assertIsNone(diagnostics['support']['temporal_retention'])
            for cell in diagnostics['cells']:
                self.assertEqual(cell[reason], [1, 2, 3, 4, 5])
                self.assertEqual(cell[other], [])
                self.assertEqual(cell['failed_frames'], [1, 2, 3, 4, 5])


class FrozenMeasurementTests(unittest.TestCase):
    def test_project_reads_and_decodes_each_jpeg_once_and_closes_cache(self):
        from collections import Counter
        from timelapse import measurement, project
        cache = tempfile.SpooledTemporaryFile(max_size=1)  # Exercise disk spill.
        with tempfile.TemporaryDirectory() as temp:
            paths = create(Path(temp) / 'demo', count=12)
            with patch.object(project, 'safe_read', wraps=project.safe_read) as reads, \
                 patch.object(measurement, 'decode', wraps=measurement.decode) as decodes, \
                 patch.object(measurement.tempfile, 'SpooledTemporaryFile', return_value=cache):
                p = Project(**paths)
            jpeg_reads = Counter(str(call.args[0]) for call in reads.call_args_list
                                 if Path(call.args[0]).suffix.lower() == '.jpg')
            self.assertEqual(jpeg_reads, Counter(str(f.jpeg) for f in p.frames))
            self.assertEqual(decodes.call_count, 12)
            self.assertTrue(cache.closed)
            for frame in p.frames:
                expected = measurement.measure_frozen(frame.jpeg.read_bytes(), p.roi, p.report['measurement_contract'])
                self.assertEqual(frame.measure, expected)

    def test_cache_matches_original_fixed_support_measurement_exactly(self):
        from timelapse.measurement import decode, summarize, SequenceMeter
        rng = np.random.default_rng(27)
        base = rng.integers(30, 180, (128, 192, 3), dtype=np.uint8)
        images = []
        for i in range(5):
            codes = base.copy()
            codes[i * 8:i * 8 + 8] = 255
            codes[:, i * 8:i * 8 + 8] = 0
            images.append(jpeg(codes))
        decoded = [decode(image, [0, 0, 1, 1]) for image in images]
        support = np.logical_and.reduce([item[1] for item in decoded])
        expected = [summarize(item, support) for item in decoded]
        with SequenceMeter([0, 0, 1, 1]) as meter:
            for image in images:
                meter.add(image)
            with patch('timelapse.measurement.decode', side_effect=AssertionError('must reuse samples')):
                actual, _, _ = meter.finish()
        self.assertEqual(actual, expected)
        self.assertTrue(meter.samples.closed)

    def test_cache_closes_on_decode_or_measurement_failure(self):
        from timelapse.measurement import SequenceMeter, MeasurementError
        image = linear_jpeg(-4.)
        with self.assertRaises(ValueError):
            with SequenceMeter([0, 0, 1, 1]) as meter:
                meter.add(image)
                meter.add(linear_jpeg(-4., (128, 128)))
        self.assertTrue(meter.samples.closed)
        with self.assertRaises(MeasurementError):
            with SequenceMeter([0, 0, 1, 1]) as meter:
                meter.add(jpeg(np.zeros((128, 128))))
                meter.finish()
        self.assertTrue(meter.samples.closed)

    def test_progress_counts_source_frames_once_then_summarizes(self):
        from timelapse.server import App
        with tempfile.TemporaryDirectory() as temp:
            paths = create(Path(temp) / 'demo', count=12)
            events = []
            p = Project(**paths, srgb=True, progress=lambda *event: events.append(event))
            self.assertEqual(len(p.frames), 12)
            self.assertEqual(events[:12], [(i, 12, 'analysis') for i in range(1, 13)])
            self.assertEqual(events[12:], [(12, 12, 'finalize')])
            app = App(temp)
            try:
                def task(progress):
                    progress(7, 12, 'brightness')
                    return 'ok'
                key = app.submit(task)['job']
                app.pool.shutdown(wait=True)
                self.assertEqual(app.jobs[key]['progress'], [7, 12])
                self.assertEqual(app.jobs[key]['phase'], 'brightness')
                self.assertEqual(app.jobs[key]['state'], 'done')
            finally:
                app.pool.shutdown(wait=True)

    def test_async_analysis_failure_includes_frame_names_and_invalidates_old_snapshot(self):
        from timelapse.server import App
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = create(root / 'demo', count=5)
            app = App(root)
            try:
                app.project = Project(**paths, srgb=True)
                old_id = app.project.id
                invalid = root / 'white'
                invalid.mkdir()
                for path in Path(paths['preview_dir']).glob('*.jpg'):
                    (invalid / path.name).write_bytes(jpeg(np.full((128, 192), 255)))
                started = app.dispatch('/api/analyze', {**paths, 'preview_dir': str(invalid), 'srgb': True})
                app.pool.shutdown(wait=True)
                job = app.jobs[started['job']]
                self.assertEqual(job['state'], 'error')
                self.assertFalse(job['measurement']['passed'])
                self.assertEqual([f['name'] for f in job['measurement']['frames']], [f'DSC_{i:04d}.jpg' for i in range(1, 6)])
                self.assertIsNone(app.project)
                for route in ('/api/calculate', '/api/export'):
                    with self.subTest(route=route), self.assertRaisesRegex(ValueError, '快照已失效'):
                        app.dispatch(route, {'id': old_id})
            finally:
                app.pool.shutdown(wait=True)

    def test_v2_feedback_reuses_frozen_contract_and_target_without_reanalysis(self):
        from tests.test_feedback_pipeline import FeedbackPipelineTests
        fixture = FeedbackPipelineTests('test_two_round_export_preserves_goal_baseline_and_sources')
        fixture.setUp()
        try:
            images = [p.read_bytes() for p in sorted(fixture.original.glob('*.jpg'))]
            measures, diagnostic, contract = run_meter(images)
            report = analysis.infer(measures, [0.] * fixture.n, [0.] * fixture.n,
                                    list(range(fixture.n)), 'manual', measurement=diagnostic, contract=contract)
            frozen_target = deepcopy(fixture.frozen['result']['target'])
            fixture.frozen['report'] = report
            (fixture.source / 'verification.json').write_text(json.dumps(fixture.frozen))
            source_files = [p for folder in (fixture.source, fixture.original, fixture.current, fixture.actual)
                            for p in folder.rglob('*') if p.is_file()]
            before = {p: digest(p.read_bytes()) for p in source_files}
            with patch('timelapse.measurement.SequenceMeter', side_effect=AssertionError('feedback cannot reselect support')), \
                 patch('timelapse.analysis.measure', side_effect=AssertionError('v2 feedback cannot use legacy meter')):
                result = fixture.run_build()
            self.assertTrue(result['ready'])
            saved = json.loads((Path(result['path']) / 'manifest.json').read_text())
            self.assertEqual(saved['result']['target'], frozen_target)
            self.assertEqual(saved['report']['measurement_contract'], contract)
            self.assertEqual(before, {p: digest(p.read_bytes()) for p in source_files})
        finally:
            fixture.tearDown()

    def test_new_export_and_verification_preserve_mask_goal_and_all_source_bytes(self):
        from timelapse.measurement import brightness, measure_frozen
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = create(root / 'demo', count=30)
            previews = Path(paths['preview_dir'])
            for path in previews.glob('*.jpg'):
                with Image.open(path) as im:
                    codes = np.asarray(im).copy()
                codes[:32, :48] = 255
                path.write_bytes(jpeg(codes))
            inputs = list(Path(paths['raw_dir']).iterdir()) + list(previews.iterdir())
            before = {path: digest(path.read_bytes()) for path in inputs}
            project = Project(**paths, srgb=True)
            result = project.calculate({'selected': [15]})
            output = project.export(root / 'out', result['revision'], project.id)
            saved = Path(output['path']) / 'manifest.json'
            manifest = json.loads(saved.read_text())
            contract = manifest['report']['measurement_contract']
            self.assertEqual(contract, project.report['measurement_contract'])
            self.assertEqual(manifest['result']['target'], result['target'])
            frozen = deepcopy(manifest)
            rendered = root / 'rerendered'
            rendered.mkdir()
            expected, images = [], []
            for frame in manifest['frames']:
                with Image.open(previews / frame['preview']) as im:
                    codes = np.asarray(im).copy()
                # Previously rejected pixels become perfectly usable. Their
                # inclusion would change the measurement basis of the goal.
                codes[:32, :48] = codes[64:96, 96:144]
                data = jpeg(codes)
                images.append(data)
                (rendered / frame['preview']).write_bytes(data)
                measured = measure_frozen(data, manifest['roi'], contract)
                self.assertEqual(measured['weights'], contract['weights'])
                expected.append(brightness(measured, manifest['report']))
            _, renewed, _ = run_meter(images)
            self.assertGreater(renewed['support']['pixels'], sum(contract['weights']))
            with patch('timelapse.measurement.SequenceMeter', side_effect=AssertionError('never reselect frozen support')), \
                 patch('timelapse.analysis.measure', side_effect=AssertionError('never switch v2 back to legacy')):
                checked = verify_manifest(manifest, rendered)
            self.assertEqual(checked['actual'], expected)
            self.assertEqual(manifest, frozen)
            self.assertEqual(json.loads(saved.read_text()), frozen)
            self.assertEqual(before, {path: digest(path.read_bytes()) for path in inputs})

    def test_new_frozen_support_failure_does_not_select_newly_usable_pixels(self):
        from timelapse.measurement import measure_frozen
        plain = np.full((256, 256), 96, dtype=np.uint8)
        plain[:32, :] = 255
        _, _, contract = run_meter([jpeg(plain)] * 5)
        changed = plain.copy()
        changed[:32, :] = 96  # this would offer replacement pixels
        changed[32:64, :] = 255  # original support is now invalid
        measured = measure_frozen(jpeg(changed), [0, 0, 1, 1], contract)
        self.assertEqual(measured['weights'], contract['weights'])
        self.assertTrue(all(w == 0 for w in measured['weights'][:8]))
        self.assertFalse(all(measured['valid'][j] for j, w in enumerate(contract['weights']) if w))

    def test_dark_tail_can_be_certified_without_changing_frozen_statistic(self):
        from timelapse.measurement import measure_frozen, brightness, comparability
        original = np.full((256, 256), 96, dtype=np.uint8)
        _, _, contract = run_meter([jpeg(original)] * 5)
        report = {'common_tiles': list(range(64)), 'measurement_contract': contract}
        changed = original.copy()
        changed[::4, ::4] = 0
        m = measure_frozen(jpeg(changed), [0, 0, 1, 1], contract)
        check = comparability(m, report)
        self.assertFalse(check['strict_support_valid'])
        self.assertTrue(check['comparable'])
        self.assertEqual(check['dark_pixels'], 4096)
        self.assertEqual(check['brightness_bounds_ev'], [brightness(m, report)] * 2)
        self.assertEqual(m['weights'], contract['weights'])
        old = deepcopy(report)
        old['measurement_contract']['version'] = 'roi-fixed-support-v2'
        self.assertFalse(comparability(m, old)['comparable'])

    def test_censoring_that_can_move_median_is_not_certified(self):
        from timelapse.measurement import measure_frozen, comparability
        original = np.full((256, 256), 96, dtype=np.uint8)
        _, _, contract = run_meter([jpeg(original)] * 5)
        report = {'common_tiles': list(range(64)), 'measurement_contract': contract}
        for color in ((0, 0, 0), (255, 255, 255), (255, 20, 20)):
            with self.subTest(color=color):
                changed = np.empty((256, 256, 3), dtype=np.uint8)
                changed[:] = color
                m = measure_frozen(jpeg(changed), [0, 0, 1, 1], contract)
                check = comparability(m, report)
                self.assertFalse(check['comparable'])
                self.assertLess(*check['brightness_bounds_ev'])

    def test_verification_accepts_certified_dark_tail_but_blocks_lost_median(self):
        from timelapse.measurement import brightness
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = np.full((256, 256), 96, dtype=np.uint8)
            original_data = jpeg(original)
            measures, _, contract = run_meter([original_data] * 5)
            report = {'common_tiles': list(range(64)), 'measurement_contract': contract,
                      'events': [], 'boundaries': []}
            manifest = {'roi': [0, 0, 1, 1], 'dimensions': [256, 256], 'report': report,
                        'result': {'target': [brightness(m, report) for m in measures], 'settings': {'selected': []}},
                        'frames': [{'name': f'f{i}.ARW', 'preview_sha256': digest(original_data)} for i in range(5)]}
            frozen = deepcopy(manifest)
            changed = original.copy()
            changed[::4, ::4] = 0
            for i in range(5):
                (root / f'f{i}.jpg').write_bytes(jpeg(changed))
            before = {p: digest(p.read_bytes()) for p in root.iterdir()}
            with patch('timelapse.measurement.SequenceMeter', side_effect=AssertionError('no reselection')):
                checked = verify_manifest(manifest, root)
            self.assertEqual(checked['rmse_ev'], 0.)
            self.assertEqual(len(checked['measurement_checks']), 5)
            self.assertIn('5 帧含失效参考像素', checked['note'])
            self.assertEqual(manifest, frozen)
            self.assertEqual(before, {p: digest(p.read_bytes()) for p in root.iterdir()})
            (root / 'f0.jpg').write_bytes(jpeg(np.zeros_like(original)))
            with self.assertRaisesRegex(ValueError, '1 帧.*无法证明最终测光值不受影响'):
                verify_manifest(manifest, root)
            self.assertEqual(manifest, frozen)
            diagnostic = verify_manifest(manifest, root, diagnostic=True)
            self.assertEqual(diagnostic['measurement_status'], 'needs-review')
            self.assertEqual(diagnostic['total_frames'], 5)
            self.assertEqual(diagnostic['comparable_frames'], 4)
            self.assertEqual(len(diagnostic['actual']), 5)
            self.assertEqual(len(diagnostic['_images']), 5)
            row = diagnostic['uncertain_frames'][0]
            self.assertEqual(row['frame'], 1)
            self.assertFalse(row['comparable'])
            self.assertGreater(row['interval_width_ev'], 1)
            self.assertLessEqual(diagnostic['rmse_bounds_ev'][0], diagnostic['rmse_ev'])
            self.assertGreaterEqual(diagnostic['rmse_bounds_ev'][1], diagnostic['rmse_ev'])
            self.assertIn('4 帧含失效参考像素', diagnostic['note'])
            self.assertNotIn('5 帧含失效参考像素', diagnostic['note'])
            self.assertEqual(manifest, frozen)
            # Unknown legacy uncertainty must remain unknown, never a zero-width pass.
            legacy = deepcopy(manifest)
            legacy['report']['measurement_contract']['version'] = 'roi-fixed-support-v2'
            legacy_review = verify_manifest(legacy, root, diagnostic=True)
            self.assertIsNone(legacy_review['rmse_bounds_ev'])
            self.assertTrue(all(row['interval_width_ev'] is None for row in legacy_review['uncertain_frames']))


    def test_legacy_manifest_uses_exact_legacy_meter_without_new_selection(self):
        """A saved goal without a metering contract must keep its old semantics."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = create(root / 'demo', count=12)
            files = sorted(Path(paths['preview_dir']).glob('*.jpg'))
            old = [analysis.measure(p.read_bytes(), [0, 0, 1, 1]) for p in files]
            report = analysis.infer(old, [0.] * 12, [0.] * 12, list(range(12)), 'manual')
            manifest = {'roi': [0, 0, 1, 1], 'dimensions': list(old[0]['dimensions']),
                        'report': report,
                        'result': {'target': deepcopy(report['measured']), 'settings': {'selected': []}},
                        'frames': [{'name': p.stem + '.ARW', 'preview_sha256': digest(p.read_bytes())} for p in files]}
            before = deepcopy(manifest)
            with patch('timelapse.measurement.SequenceMeter', side_effect=AssertionError('frozen legacy support cannot be reselected')):
                result = verify_manifest(manifest, paths['preview_dir'])
            self.assertEqual(result['actual'], report['measured'])
            self.assertEqual(result['rmse_ev'], 0.)
            self.assertEqual(manifest, before)


if __name__ == '__main__':
    unittest.main()
