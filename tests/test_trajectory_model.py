import copy
from datetime import datetime, timedelta
import json
import unittest

import numpy as np

from timelapse import joint_model, trajectory_model as model


def fixture(n=160, events=((45, -1.), (105, -1.)), *, compensated=False,
            gap=None, rate=.04, cloud=False):
    seconds = np.arange(n, dtype=float)*3
    if gap is not None:
        seconds[gap:] += 11
    scene = -10+rate*seconds/3
    if cloud:
        scene -= 1.5*np.exp(-((np.arange(n)-n*.48)/14)**2)
    camera = np.zeros(n)
    adjustment = np.zeros(n)
    for index, delta in events:
        camera[index:] += delta
        if compensated:
            adjustment[index:] -= delta
    y = scene+1.5*(camera+adjustment)
    start = datetime(2026, 1, 1, 6)
    frames = [{'identity': {'ExposureTime': float(2**camera[i]), 'ISOSpeedRatings': 100.,
                            'FNumber': 8., 'DateTimeOriginal': (start+timedelta(seconds=float(t))).isoformat()},
               'original_controls': [float(adjustment[i]), -100., 0.]} for i, t in enumerate(seconds)]
    observations = [{'brightness': [float(v), float(v+1), float(v+2), -.3, -.4, -.5],
                     'channel_clip_fraction': [.01, 0., 0.]} for v in y]
    regions = [{'sky': float(v+1), 'sea': float(v+.3), 'buildings': float(v-.4),
                'sunrise': float(v+3)} for v in y]
    return {'frames': frames, 'observations': observations,
            'camera_events': [i for i, _ in events], 'boundaries': [gap] if gap else []}, regions


def rendered(plan):
    return [{'brightness': [v, v+1, v+2, -.3, -.4, -.5],
             'channel_clip_fraction': [.01, 0., 0.],
             'regions': {**{k: plan['regional_targets'][k][i] for k in model.TARGET_REGIONS},
                         'sunrise': plan['regional_observations']['sunrise'][i]}}
            for i, v in enumerate(plan['target_mid'])]


class TrajectoryPlanTests(unittest.TestCase):
    def test_camera_steps_are_removed_before_range_mapping(self):
        base, regions = fixture()
        before = json.dumps([base, regions], sort_keys=True)
        plan = model.build_plan(base, regions)
        self.assertEqual([e['status'] for e in plan['events']], ['applied', 'applied'])
        np.testing.assert_allclose([e['jumps'][0] for e in plan['events']], [-1.5, -1.5], atol=1e-10)
        target = np.array(plan['target_mid'])
        np.testing.assert_allclose(target, np.linspace(target[0], target[-1], len(target)), atol=1e-10)
        self.assertGreater(plan['scale'], 0)
        self.assertLess(plan['scale'], 1)
        self.assertEqual(json.dumps([base, regions], sort_keys=True), before)
        json.dumps(plan, allow_nan=False)

    def test_real_cloud_dimming_is_preserved_without_camera_event(self):
        base, regions = fixture(events=(), rate=.01, cloud=True)
        plan = model.build_plan(base, regions)
        self.assertEqual(plan['events'], [])
        target = np.array(plan['target_mid'])
        self.assertLess(np.min(target[15:]-target[:-15]), -.3)
        report = model.review_sequence(plan, rendered(plan))
        self.assertTrue(report['numeric_conditions_met'])
        self.assertFalse(report['visual_playback_verified'])
        self.assertTrue(all(row['downward_changes_are_not_failures'] for row in report['multiscale']))

    def test_already_compensated_camera_step_is_not_removed_twice(self):
        base, regions = fixture(events=((60, -1.),), compensated=True)
        plan = model.build_plan(base, regions)
        self.assertEqual(plan['events'][0]['status'], 'already-compensated')
        np.testing.assert_allclose(plan['step_correction_mid'], 0, atol=0)
        self.assertAlmostEqual(plan['scale'], 1.)

    def test_dense_negative_negative_positive_burst_uses_all_plateaus(self):
        base, regions = fixture(events=((60, -.4), (61, -1.), (63, .4)))
        plan = model.build_plan(base, regions)
        self.assertEqual(len(plan['events']), 1)
        self.assertEqual(plan['events'][0]['indices'], [60, 61, 63])
        np.testing.assert_allclose(plan['events'][0]['jumps'], [-.6, -1.5, .6], atol=1e-10)
        target = np.array(plan['target_mid'])
        self.assertLess(np.max(abs(np.diff(target, n=2))), 1e-10)

    def test_real_time_gap_is_not_inferred_as_camera_step(self):
        base, regions = fixture(events=((45, -1.),), gap=100)
        plan = model.build_plan(base, regions)
        self.assertEqual(plan['gaps'], [100])
        self.assertEqual(plan['camera_events'], [45])
        expected_gap = .04*14/3
        self.assertAlmostEqual(plan['natural_mid'][100]-plan['natural_mid'][99], expected_gap)
        self.assertAlmostEqual(plan['target_mid'][100]-plan['target_mid'][99], expected_gap*plan['scale'])
        report = model.review_sequence(plan, rendered(plan))
        self.assertTrue(any(not row['evaluated'] for row in report['event_windows']))
        self.assertTrue(report['numeric_conditions_met'])

    def test_camera_event_at_gap_is_unidentifiable(self):
        base, regions = fixture(events=((80, -1.),), gap=80)
        with self.assertRaisesRegex(ValueError, 'intersects a time gap'):
            model.build_plan(base, regions)

    def test_reversed_primary_region_evidence_is_rejected(self):
        base, regions = fixture(events=((60, -1.),))
        for row in regions[60:]:
            row['sea'] += 3
        with self.assertRaisesRegex(ValueError, 'primary_regions'):
            model.build_plan(base, regions)

    def test_insufficient_stable_support_is_rejected(self):
        base, regions = fixture(events=((2, -1.),))
        with self.assertRaisesRegex(ValueError, 'five stable samples'):
            model.build_plan(base, regions)

    def test_zero_and_negative_range_mapping_are_rejected(self):
        base, regions = fixture(events=(), rate=0)
        with self.assertRaisesRegex(ValueError, 'zero de-stepped'):
            model.build_plan(base, regions)
        base, regions = fixture(events=((60, -2.),), rate=.01)
        with self.assertRaisesRegex(ValueError, 'non-positive affine'):
            model.build_plan(base, regions)

    def test_missing_regions_do_not_silently_use_mid_as_spatial_evidence(self):
        base, regions = fixture()
        del regions[0]['sea']
        with self.assertRaisesRegex(ValueError, 'missing fixed-region'):
            model.build_plan(base, regions)


class TrajectoryReviewTests(unittest.TestCase):
    def setUp(self):
        base, regions = fixture()
        self.base = base
        self.plan = model.build_plan(base, regions)

    def test_numeric_pass_does_not_authorize_export_or_visual_acceptance(self):
        result = model.review_sequence(self.plan, rendered(self.plan))
        self.assertTrue(result['numeric_conditions_met'])
        self.assertFalse(result['verified'])
        self.assertFalse(result['visual_playback_verified'])
        self.assertIn('explicit user approval', result['delivery_gate'])

    def test_zero_output_residual_cannot_accept_old_smoothed_camera_steps(self):
        bad = copy.deepcopy(self.plan)
        old = joint_model.targets([o['brightness'] for o in self.base['observations']], [], 31)
        bad['target_mid'] = [v[0] for v in old]
        bad['plan_sha256'] = model._hash(bad)
        result = model.review_sequence(bad, rendered(bad))
        self.assertEqual(result['max_mid_error_ev'], 0)
        self.assertFalse(result['target_integrity']['valid'])
        self.assertFalse(result['numeric_conditions_met'])
        self.assertGreater(result['target_integrity']['reconstructed_target_max_error_ev'], .1)

    def test_fixed_thresholds_reject_mid_adjacent_and_clipping(self):
        for mode in ('mid', 'adjacent', 'clip'):
            rows = rendered(self.plan)
            if mode == 'mid':
                rows[30]['brightness'][0] += .041
            elif mode == 'adjacent':
                rows[30]['brightness'][0] += .03
                rows[31]['brightness'][0] -= .03
            else:
                rows[30]['channel_clip_fraction'][0] += .00101
            self.assertFalse(model.review_sequence(self.plan, rows)['numeric_conditions_met'])

    def test_sky_event_window_is_independent_of_mid_error(self):
        rows = rendered(self.plan)
        rows[60]['regions']['sky'] += .1
        result = model.review_sequence(self.plan, rows)
        self.assertEqual(result['max_mid_error_ev'], 0)
        self.assertTrue(result['failed_sky_event_windows'])
        self.assertFalse(result['numeric_conditions_met'])

    def test_sky_dip_and_recovery_cannot_hide_behind_equal_window_endpoints(self):
        rows = rendered(self.plan)
        for i in range(40, 51):
            rows[i]['regions']['sky'] -= .12*(1-abs(i-45)/6)
        result = model.review_sequence(self.plan, rows)
        event = next(row for row in result['event_windows'] if row['frames'] == [46] and row['radius'] == 15)
        self.assertAlmostEqual(event['regional']['sky']['change_error_ev'], 0)
        self.assertGreater(event['regional']['sky']['window_residual_range_ev'], .08)
        self.assertFalse(result['numeric_conditions_met'])

    def test_sea_and_sunrise_changes_remain_visible_diagnostics(self):
        rows = rendered(self.plan)
        rows[60]['regions']['sea'] += .4
        rows[60]['regions']['sunrise'] += .5
        result = model.review_sequence(self.plan, rows)
        self.assertTrue(result['numeric_conditions_met'])
        event = next(row for row in result['event_windows'] if row['frames'] == [46] and row['radius'] == 15)
        self.assertAlmostEqual(event['regional']['sea']['change_error_ev'], .4)

    def test_mutated_plan_or_missing_render_is_rejected(self):
        bad = copy.deepcopy(self.plan)
        bad['target_mid'][0] += .01
        with self.assertRaisesRegex(ValueError, 'content hash mismatch'):
            model.review_sequence(bad, rendered(bad))
        with self.assertRaisesRegex(ValueError, 'every frozen frame'):
            model.review_sequence(self.plan, rendered(self.plan)[:-1])

    def test_regional_target_cannot_be_replaced_by_its_own_render(self):
        bad = copy.deepcopy(self.plan)
        bad['regional_targets']['sky'][60] += .2
        bad['plan_sha256'] = model._hash(bad)
        report = model.review_sequence(bad, rendered(bad))
        self.assertFalse(report['target_integrity']['valid'])
        self.assertFalse(report['numeric_conditions_met'])


class GeneralTrajectoryTests(unittest.TestCase):
    def build(self, **kwargs):
        from timelapse import trajectory_regions
        base, legacy = fixture(**kwargs)
        rows = [dict(zip(trajectory_regions.NAMES, (row['sky'], row['sea'], row['buildings'], row['sunrise'])))
                for row in legacy]
        contract = {'method': trajectory_regions.METHOD, 'mode': 'manual',
                    'per_frame_pixel_reselection': False,
                    'rois': dict(zip(trajectory_regions.NAMES,
                                    ([0, 0, .4, .4], [.6, 0, 1, .4], [0, .6, .4, 1], [.6, .6, 1, 1])))}
        return model.build_plan(base, rows, region_contract=contract)

    def render(self, plan):
        return [{'brightness': [v, v+1, v+2, -.3, -.4, -.5],
                 'channel_clip_fraction': [.01, 0., 0.],
                 'regions': {name: values[i] for name, values in plan['regional_targets'].items()}}
                for i, v in enumerate(plan['target_mid'])]

    def test_flat_light_and_equal_endpoint_cloud_are_supported(self):
        for cloud in (False, True):
            plan = self.build(events=(), rate=0, cloud=cloud)
            self.assertEqual(plan['version'], model.VERSION)
            self.assertEqual(plan['scale'], 1)
            self.assertTrue(model.review_sequence(plan, self.render(plan))['numeric_conditions_met'])
            if cloud:
                self.assertGreater(np.ptp(plan['target_mid']), 1)
            else:
                np.testing.assert_allclose(plan['target_mid'], plan['observed_mid'])

    def test_darkening_and_camera_change_that_reverses_observed_endpoints(self):
        dark = self.build(events=(), rate=-.02)
        self.assertLess(dark['target_mid'][-1], dark['target_mid'][0])
        plan = self.build(events=((60, -2.),), rate=.01)
        self.assertLess(plan['observed_mid'][-1], plan['observed_mid'][0])
        self.assertGreater(plan['target_mid'][-1], plan['target_mid'][0])
        self.assertEqual(plan['scale'], 1)
        self.assertTrue(model.review_sequence(plan, self.render(plan))['numeric_conditions_met'])

    def test_primary_region_validation_uses_frozen_new_contract(self):
        plan = self.build()
        self.assertNotIn('sunrise_role', plan)
        self.assertEqual(set(plan['regional_targets']), {'primary', 'secondary', 'check_a', 'check_b'})
        rows = self.render(plan)
        rows[60]['regions']['primary'] += .11
        review = model.review_sequence(plan, rows)
        self.assertFalse(review['numeric_conditions_met'])
        self.assertTrue(review['failed_primary_event_windows'])
        self.assertNotIn('failed_sky_event_windows', review)
        self.assertEqual(review['max_mid_error_ev'], 0)

    def test_old_plan_keeps_version_hash_and_review_contract(self):
        base, rows = fixture()
        plan = model.build_plan(base, rows)
        self.assertEqual(plan['version'], model.LEGACY_VERSION)
        self.assertNotIn('primary_region', plan)
        self.assertIn('sunrise_role', plan)
        review = model.review_sequence(plan, rendered(plan))
        self.assertTrue(review['numeric_conditions_met'])
        self.assertIn('failed_sky_event_windows', review)

    def test_new_plan_does_not_weaken_camera_gap_or_source_edit_evidence(self):
        with self.assertRaisesRegex(ValueError, 'intersects a time gap'):
            self.build(events=((80, -1.),), gap=80)
        from timelapse import trajectory_regions
        base, rows = fixture(events=((60, -1.),))
        base['frames'][60]['original_controls'][1] = -50
        generic = [dict(zip(trajectory_regions.NAMES, row.values())) for row in rows]
        contract = self.build()['regional_measurement_contract']
        with self.assertRaisesRegex(ValueError, 'changing original controls'):
            model.build_plan(base, generic, contract)


if __name__ == '__main__':
    unittest.main()
