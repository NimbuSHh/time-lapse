"""Output continuity across adjustment pauses, without erasing protected edits."""
import unittest
import numpy as np
from timelapse.analysis import infer, correct, bridge_gaps


def gap_report(y, gaps=(40,), cameras=None, exposures=None):
    n = len(y)
    times = np.arange(n, dtype=float) * 3
    for i in gaps:
        times[i:] += 11  # Three-second cadence, fourteen-second adjustment pause.
    return infer([{'tiles': [float(v)] * 64, 'valid': [True] * 64} for v in y],
                 [0.] * n if exposures is None else exposures,
                 [0.] * n if cameras is None else cameras, times.tolist(), 'manual')


class GapSmoothingTests(unittest.TestCase):
    def test_pause_has_continuous_local_transition_and_preserves_outer_frames(self):
        y = np.linspace(-5, -4, 90)
        y[40:] += .09
        report = gap_report(y)
        before = correct(report, [0] * 90, [], strength=.6)
        after = correct(report, [0] * 90, [], strength=.6, smooth_gaps=True)
        span, = after['gap_joins']
        a, b = span['start_index'], span['end_index']
        self.assertEqual(span['indices'], [40])
        delta = np.asarray(after['target']) - before['target']
        np.testing.assert_allclose(delta[:a+1], 0, atol=1e-12)
        np.testing.assert_allclose(delta[b:], 0, atol=1e-12)
        differences = np.diff(after['target'])
        self.assertLess(abs(differences[39] - differences[38]), .001)
        self.assertLess(abs(differences[40] - differences[39]), .001)
        self.assertTrue(np.all(differences > 0))
        self.assertGreater(before['target'][40] - before['target'][39], .09)
        self.assertTrue(after['settings']['smooth_gaps'])

    def test_gap_option_is_independent_of_flicker_strength_and_keeps_endpoints(self):
        y = np.linspace(-5, -4, 90); y[40:] += .09
        report = gap_report(y)
        unchanged = correct(report, [0] * 90, [], strength=0, smooth_gaps=False)
        np.testing.assert_allclose(unchanged['target'], y, atol=1e-12)
        joined = correct(report, [0] * 90, [], strength=0, start=.2, end=-.3, smooth_gaps=True)
        self.assertEqual(len(joined['gap_joins']), 1)
        self.assertAlmostEqual(joined['target'][0], y[0] + .2)
        self.assertAlmostEqual(joined['target'][-1], y[-1] - .3)
        self.assertTrue(joined['calibration_required'])

    def test_camera_step_or_existing_xmp_edit_at_gap_stays_protected(self):
        y = np.linspace(-5, -4, 90); y[40:] -= 1
        for cameras, exposures in [(np.r_[np.zeros(40), -np.ones(50)], np.zeros(90)),
                                   (np.zeros(90), np.r_[np.zeros(40), np.ones(50)])]:
            with self.subTest(camera=bool(cameras[40])):
                report = gap_report(y, cameras=cameras, exposures=exposures)
                out = correct(report, exposures, [], strength=1, smooth_gaps=True)
                self.assertEqual(out['gap_joins'], [])
                np.testing.assert_allclose(out['target'], y, atol=1e-12)

    def test_nearby_unconfirmed_boundary_is_not_crossed(self):
        y = np.linspace(-5, -4, 90); y[40:] += .09; y[45:] -= 1
        cameras = np.r_[np.zeros(45), -np.ones(45)]
        report = gap_report(y, cameras=cameras)
        out = correct(report, [0] * 90, [], strength=1, smooth_gaps=True)
        self.assertEqual(out['gap_joins'][0]['end_index'], 44)
        self.assertAlmostEqual(out['target'][45] - out['target'][44], y[45] - y[44])

    def test_overlapping_gap_windows_share_one_continuous_span(self):
        y = np.linspace(-5, -4, 90); y[40:] += .06; y[44:] += .04
        report = gap_report(y, gaps=(40, 44))
        out = correct(report, [0] * 90, [], smooth_gaps=True)
        self.assertEqual(out['gap_joins'][0]['indices'], [40, 44])
        self.assertEqual(len(out['gap_joins']), 1)
        differences = np.diff(out['target'])
        self.assertLess(np.max(np.abs(np.diff(differences[37:47]))), .003)

    def test_flat_and_descending_joins_do_not_overshoot(self):
        for levels in [(0., 0.), (0., -.1), (-.1, 0.)]:
            with self.subTest(levels=levels):
                y = np.r_[np.full(20, levels[0]), np.full(20, levels[1])]
                out, spans = bridge_gaps(y, [20], [], 15)
                self.assertEqual(len(spans), 1)
                self.assertGreaterEqual(out.min(), min(levels) - 1e-12)
                self.assertLessEqual(out.max(), max(levels) + 1e-12)
                self.assertTrue(np.isfinite(out).all())

    def test_short_edge_gaps_are_not_extrapolated(self):
        y = np.arange(20, dtype=float)
        out, spans = bridge_gaps(y, [1, 19], [], 15)
        self.assertEqual(spans, [])
        np.testing.assert_array_equal(out, y)

    def test_option_requires_boolean(self):
        y = np.arange(90, dtype=float)
        with self.assertRaisesRegex(ValueError, '布尔值'):
            correct(gap_report(y), [0] * 90, [], smooth_gaps='false')


if __name__ == '__main__':
    unittest.main()
