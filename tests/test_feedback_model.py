"""Independent numerical renders exercise feedback; these are not Adobe ACR tests."""
import unittest
from unittest.mock import patch

import numpy as np

from timelapse.feedback import propose


def rmse(values):
    return float(np.sqrt(np.mean(np.square(values))))


class FeedbackModelTests(unittest.TestCase):
    def test_one_point_five_response_corrects_in_exposure_units(self):
        scene = np.linspace(-6, -3, 40)
        reference = scene - 1.2
        current = np.full(40, 1.2)
        actual = reference + 1.5 * current
        result = propose(reference, actual, np.zeros(40), current, scene)
        np.testing.assert_allclose(result['gain'], 1.5)
        np.testing.assert_allclose(result['delta'], -.32)
        np.testing.assert_allclose(result['final'], .88)
        self.assertEqual(result['blocked'], [])
        # Re-render from independent known physics, never the proposal prediction.
        rerender = reference + 1.5 * np.asarray(result['final'])
        self.assertLess(rmse(rerender - scene), .13)
        self.assertGreater(rmse(actual - scene), .59)

    def test_nonlinear_brightness_and_exposure_response_improves_two_rounds(self):
        n = 41
        scene = np.linspace(-6.4, -3.6, n)
        original = scene - np.linspace(.6, 1.05, n)
        gain = np.linspace(1.25, 1.7, n)

        def render(exposure):
            exposure = np.asarray(exposure)
            return original + gain * exposure + .12 * exposure**2

        baseline = np.zeros(n)
        first_exposure = scene - original
        first_render = render(first_exposure)
        first = propose(render(baseline), first_render, baseline,
                        first_exposure, scene, baseline_exposure=baseline)
        second_render = render(first['final'])
        second = propose(first_render, second_render, first_exposure,
                         first['final'], first['target'], baseline_exposure=baseline)
        third_render = render(second['final'])
        self.assertEqual(first['blocked'], [])
        self.assertEqual(second['blocked'], [])
        self.assertLess(rmse(second_render - scene), .35 * rmse(first_render - scene))
        self.assertLess(rmse(third_render - scene), .025)
        self.assertEqual(first['target'], scene.tolist())
        self.assertEqual(second['target'], first['target'])

    def test_camera_gain_does_not_substitute_for_paired_xmp_gain(self):
        scene = np.linspace(-5, -4, 30)
        camera = np.repeat([0., -.7, -.4], 10)
        original = scene + 1.1 * camera
        first_exposure = -1.1 * camera
        actual = original + 1.8 * first_exposure
        result = propose(original, actual, np.zeros(30), first_exposure, scene)
        rerender = original + 1.8 * np.asarray(result['final'])
        self.assertLess(rmse(rerender - scene), .21 * rmse(actual - scene))
        np.testing.assert_allclose(result['gain'][10:], 1.8)
        self.assertEqual(result['blocked'], [])

    def test_negative_exposure_pair_and_positive_residual(self):
        reference = np.full(6, -3.)
        current = np.full(6, -.8)
        actual = reference + 1.5 * current
        target = np.full(6, -3.9)
        result = propose(reference, actual, np.zeros(6), current, target)
        np.testing.assert_allclose(result['gain'], 1.5)
        np.testing.assert_allclose(result['delta'], .16)
        np.testing.assert_allclose(result['final'], -.64)

    def test_tiny_exposure_uses_three_neighbor_pairs_without_division(self):
        current = np.array([.3, .4, .5, 0., 1e-12, 0., 0.])
        reference = np.full(7, -5.)
        actual = reference + 1.5 * current
        target = actual.copy()
        target[4] += .6
        result = propose(reference, actual, np.zeros(7), current, target)
        self.assertEqual(result['statuses'][4], 'local-secant')
        self.assertAlmostEqual(result['gain'][4], 1.5)
        self.assertAlmostEqual(result['delta'][4], .32, places=9)
        self.assertEqual(result['blocked'], [])

    def test_two_pairs_are_not_enough_for_neighbor_fallback(self):
        current = np.array([.3, .4, 0., 0., 0.])
        reference = np.full(5, -5.)
        actual = reference + 1.5 * current
        target = actual.copy()
        target[2] += .6
        result = propose(reference, actual, np.zeros(5), current, target)
        self.assertEqual(result['blocked'], [2])
        self.assertEqual(result['statuses'][2], 'unsupported')
        self.assertIsNone(result['gain'][2])
        self.assertEqual(result['final'][2], current[2])

    def test_near_zero_residual_holds_without_calibration(self):
        result = propose([-5.] * 3, [-5.] * 3, [0.] * 3, [0.] * 3,
                         [-5.019, -5., -4.981])
        self.assertEqual(result['statuses'], ['within-noise'] * 3)
        self.assertEqual(result['blocked'], [])
        self.assertEqual(result['delta'], [0.] * 3)

    def test_boundary_prevents_borrowing_gain_from_another_segment(self):
        reference = np.full(8, -5.)
        current = np.array([.3, .4, .5, .6, 0., 0., 0., .4])
        actual = reference + 1.5 * current
        target = actual.copy()
        target[4] += .6
        target[7] += .3
        result = propose(reference, actual, np.zeros(8), current, target,
                         boundaries=[4])
        self.assertEqual(result['blocked'], [4])
        self.assertEqual(result['statuses'][7], 'same-frame-secant')
        self.assertAlmostEqual(result['delta'][7], .16)

    def test_inconsistent_same_frame_response_cannot_borrow_good_neighbors(self):
        for bad_gain in [-1., .2, 4.5]:
            with self.subTest(gain=bad_gain):
                reference = np.full(7, -5.)
                current = np.full(7, .5)
                actual = reference + 1.5 * current
                actual[3] = reference[3] + bad_gain * current[3]
                target = actual.copy()
                target[3] += .3
                result = propose(reference, actual, np.zeros(7), current, target)
                self.assertEqual(result['blocked'], [3])
                self.assertEqual(result['statuses'][3], 'inconsistent-response')

    def test_small_brightness_response_is_not_used_as_calibration(self):
        result = propose([-5.] * 5, [-4.94] * 5, [0.] * 5, [.2] * 5,
                         [-4.5] * 5)
        self.assertEqual(result['blocked'], list(range(5)))
        self.assertEqual(result['statuses'], ['inconsistent-response'] * 5)

    def test_large_exposure_without_response_cannot_borrow_neighbors(self):
        original = np.full(9, -4.)
        actual = np.full(9, -3.25)
        actual[4] = -4.
        result = propose(original, actual, [0.] * 9, [.5] * 9, [-3.5] * 9)
        self.assertIn(4, result['blocked'])
        self.assertEqual(result['statuses'][4], 'inconsistent-response')
        self.assertEqual(result['final'][4], .5)

    def test_step_limit_is_reported_and_final_is_quantized(self):
        result = propose([-5.] * 3, [-4.8] * 3, [0.] * 3, [.2] * 3,
                         [-3.8] * 3, max_step=.247)
        self.assertEqual(result['limited'], [0, 1, 2])
        self.assertEqual(result['blocked'], [])
        for old, final, delta in zip([.2] * 3, result['final'], result['delta']):
            self.assertLessEqual(abs(delta), .247 + 1e-12)
            self.assertAlmostEqual(final * 100, round(final * 100))
            self.assertAlmostEqual(final - old, delta)

    def test_cumulative_limit_uses_original_baseline_and_blocks_instead_of_clips(self):
        result = propose([-5.] * 3, [-4.6] * 3, [1.55] * 3, [1.95] * 3,
                         [-4.] * 3, baseline_exposure=[0.] * 3)
        self.assertEqual(result['blocked'], [0, 1, 2])
        self.assertEqual(result['final'], [1.95] * 3)
        np.testing.assert_allclose(result['baseline_delta'], 1.95)

    def test_absolute_exposure_limit_blocks_even_with_small_baseline_delta(self):
        result = propose([-5.] * 3, [-4.5] * 3, [4.4] * 3, [4.9] * 3,
                         [-4.] * 3, baseline_exposure=[4.] * 3)
        self.assertEqual(result['blocked'], [0, 1, 2])
        self.assertEqual(result['final'], [4.9] * 3)

    def test_dense_group_feedback_keeps_target_without_event_reinference(self):
        n = 45
        target = np.linspace(-5.2, -4.2, n)
        camera = np.zeros(n)
        for index, jump in ((15, -.36), (16, -.9), (18, .68)):
            camera[index:] += jump
        original = target + camera
        current = -camera
        actual = original + 1.5 * current
        with patch('timelapse.analysis.infer', side_effect=AssertionError('Do not re-infer')):
            result = propose(original, actual, np.zeros(n), current, target)
        rerender = original + 1.5 * np.asarray(result['final'])
        self.assertEqual(result['target'], target.tolist())
        self.assertEqual(result['blocked'], [])
        # .01 EV exposure quantization adds up to .005 * response_gain.
        self.assertLess(rmse(rerender - target), .2 * rmse(actual - target) + .005 * 1.5)
        self.assertLess(np.max(np.abs(np.diff(rerender - target))), .1)

    def test_mismatched_lengths_and_nonfinite_inputs_are_rejected(self):
        args = [[-5.] * 4, [-4.5] * 4, [0.] * 4, [.5] * 4, [-4.7] * 4]
        for position in range(5):
            with self.subTest(length_position=position):
                broken = [list(v) for v in args]
                broken[position].pop()
                with self.assertRaises(ValueError):
                    propose(*broken)
            for invalid in (float('nan'), float('inf'), -float('inf')):
                with self.subTest(position=position, invalid=invalid):
                    broken = [list(v) for v in args]
                    broken[position][1] = invalid
                    with self.assertRaises(ValueError):
                        propose(*broken)
        with self.assertRaises(ValueError):
            propose(*args, baseline_exposure=[0.])
        with self.assertRaises(ValueError):
            propose(*args, baseline_exposure=[0., 0., float('nan'), 0.])

    def test_small_intervals_improve_independent_positive_and_negative_renders(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                reference = np.full(5, -5.)
                current = np.full(5, direction * .6)
                actual = reference + 1.5 * current
                target = actual + direction * .3
                before = np.column_stack((reference - .001, reference + .001))
                after = np.column_stack((actual - .001, actual + .001))
                result = propose(reference, actual, np.zeros(5), current, target,
                                 reference_bounds=before, actual_bounds=after)
                self.assertEqual(result['blocked'], [])
                for bounds in result['gain_bounds']:
                    np.testing.assert_allclose(bounds, [1.5 - .002/.6, 1.5 + .002/.6])
                rerender = reference + 1.5 * np.asarray(result['final'])
                self.assertLess(rmse(rerender - target), .25 * rmse(actual - target))
                predicted = np.asarray(result['prediction_bounds_ev'])
                self.assertTrue(np.all(predicted[:, 0] <= rerender))
                self.assertTrue(np.all(predicted[:, 1] >= rerender))

    def test_goal_within_nonzero_interval_holds_without_claiming_exact_agreement(self):
        result = propose([-5.], [-4.25], [0.], [.5], [-4.1],
                         reference_bounds=[[-5., -5.]], actual_bounds=[[-4.5, -4.]])
        self.assertEqual(result['final'], [.5])
        self.assertEqual(result['statuses'], ['within-measurement-interval'])
        self.assertEqual(result['blocked'], [])
        self.assertEqual(result['actual_bounds_ev'], [[-4.5, -4.]])
        self.assertEqual(result['prediction_bounds_ev'], [[-4.5, -4.]])

    def test_unknown_interval_blocks_even_when_observation_equals_target(self):
        for key in ('reference_bounds', 'actual_bounds'):
            with self.subTest(key=key):
                result = propose([-5.], [-4.25], [0.], [.5], [-4.25], **{key: [None]})
                self.assertEqual(result['blocked'], [0])
                self.assertEqual(result['statuses'], ['measurement-unknown'])
                self.assertEqual(result['final'], [.5])
                self.assertIsNone(result['gain_bounds'][0])

    def test_interval_crossing_response_direction_cannot_borrow_neighbors(self):
        reference = np.full(5, -5.)
        actual = np.full(5, -4.25)
        bounds = [[v, v] for v in actual]
        bounds[2] = [-5.1, -3.4]
        target = actual.copy()
        target[2] = -3.
        result = propose(reference, actual, np.zeros(5), np.full(5, .5), target, actual_bounds=bounds)
        self.assertEqual(result['blocked'], [2])
        self.assertEqual(result['statuses'][2], 'inconsistent-response')
        self.assertEqual(result['final'][2], .5)

    def test_signal_threshold_applies_to_whole_measurement_interval(self):
        result = propose([-5.], [-4.9], [0.], [.2], [-4.5], actual_bounds=[[-4.93, -4.87]])
        self.assertEqual(result['blocked'], [0])
        self.assertEqual(result['statuses'], ['inconsistent-response'])

    def test_neighbor_response_propagates_intervals_without_crossing_boundaries(self):
        current = np.array([.3, .4, .5, 0., 0.])
        reference = np.full(5, -5.)
        actual = reference + 1.5 * current
        target = actual.copy()
        target[3:] += .6
        kwargs = {'reference_bounds': np.column_stack((reference-.001, reference+.001)),
                  'actual_bounds': np.column_stack((actual-.001, actual+.001)), 'boundaries': [4]}
        result = propose(reference, actual, np.zeros(5), current, target, **kwargs)
        self.assertEqual(result['blocked'], [4])
        self.assertEqual(result['statuses'][3], 'local-secant')
        np.testing.assert_allclose(result['gain_bounds'][3], [1.495, 1.505])
        rerender = reference[3] + 1.5 * result['final'][3]
        self.assertLess(abs(rerender - target[3]), .13)
        self.assertEqual(result['final'][4], 0.)

    def test_uncertain_quantization_cannot_cross_supported_residual(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                current = direction * .503
                actual = -5. + direction * 1.95
                target = actual + direction * .022
                result = propose([-5.], [actual], [0.], [current], [target],
                                 actual_bounds=[[actual-.001, actual+.001]])
                # No two-decimal value fits between the current exposure and
                # the conservatively supported endpoint; retain the original.
                self.assertEqual(result['final'], [current])
                self.assertEqual(result['blocked'], [])
                upper_gain = result['gain_bounds'][0][1]
                self.assertLessEqual(abs(result['delta'][0]) * upper_gain, .021)

    def test_invalid_or_noncontaining_intervals_rejected(self):
        args = ([-5.], [-4.25], [0.], [.5], [-4.])
        for bounds in ([], [[-4.2, -4.1]], [[-4., -4.5]], [[-4.5, float('inf')]], [[-4.5]]):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                propose(*args, actual_bounds=bounds)


if __name__ == '__main__':
    unittest.main()
