"""Different regional tone responses are not evidence of an unmeasurable step."""
import unittest
import numpy as np
from timelapse.analysis import infer, correct, event_residual


def sequence(changes, gains, weights=None):
    n = 90
    cameras = np.zeros(n)
    for index, delta in changes:
        cameras[index:] += delta
    trend = np.linspace(-5, -4, n)
    tiles = trend[:, None] + cameras[:, None] * np.asarray(gains)
    measures = [{'tiles': row.tolist(), 'valid': [True] * 64,
                 'weights': weights or [1] * 64} for row in tiles]
    return infer(measures, [0] * n, cameras.tolist(), list(range(n)), 'manual',
                 {'passed': True} if weights else None, {'test': True} if weights else None)


class EventEvidenceTests(unittest.TestCase):
    def test_isolated_regional_gains_remain_selectable(self):
        report = sequence([(30, -1.0589)], np.linspace(.55, 1.6, 64))
        event = report['events'][0]
        self.assertGreater(event['tile_dispersion'], .2)
        self.assertEqual(event['spatial_support'], 1.)
        self.assertTrue(event['selectable'])
        result = correct(report, [0] * 90, [30], strength=1)
        self.assertLess(abs(event_residual(result['target'], event, report['boundaries'])['remaining_jump']), 1e-10)

    def test_dense_regional_gains_correct_every_plateau(self):
        report = sequence([(30, -.36), (31, -1.32), (33, .68)], np.linspace(.55, 1.6, 64))
        event = report['events'][0]
        self.assertGreater(event['tile_dispersion'], .2)
        self.assertEqual(event['spatial_support'], 1.)
        self.assertTrue(event['selectable'])
        result = correct(report, [0] * 90, [30], strength=1)
        self.assertLess(event_residual(result['target'], event, report['boundaries'])['max_component_jump'], 1e-10)
        self.assertAlmostEqual(result['target'][0], report['measured'][0])
        self.assertAlmostEqual(result['target'][-1], report['measured'][-1])

    def test_opposing_regions_allow_choice_with_specific_evidence(self):
        report = sequence([(30, -1)], [1.] * 40 + [-1.] * 24)
        event = report['events'][0]
        self.assertEqual(event['spatial_support'], .625)
        self.assertTrue(event['selectable'])
        self.assertFalse(event['recommended'])
        self.assertIsNotNone(event['jump'])
        self.assertEqual([c['code'] for c in event['checks'] if not c['passed']], ['spatial'])
        self.assertIn('62.5%', event['reason'])
        result = correct(report, [0] * 90, [30])
        self.assertEqual(result['event_warnings'], [{'index':30, 'reason':event['reason']}])
        self.assertTrue(result['calibration_required'])
        self.assertLess(abs(event_residual(result['target'], event, report['boundaries'])['remaining_jump']), 1e-10)

    def test_warning_event_cannot_supply_response_to_other_events(self):
        from tests.test_core import report
        camera = np.zeros(90); camera[30:] -= 1; camera[60:] -= 1
        y = np.linspace(-5, -4, 90) + camera; y[60:] += 2.5
        r = report(y, camera.tolist())
        self.assertTrue(r['events'][0]['recommended'])
        self.assertFalse(r['events'][1]['recommended'])
        result = correct(r, [0]*90, [30,60])
        self.assertEqual(result['response']['support'], 1)
        self.assertAlmostEqual(result['response']['gain'], 1.)
        self.assertEqual([e['index'] for e in result['event_warnings']], [60])
        for event in r['events']:
            self.assertLess(abs(event_residual(result['target'], event, r['boundaries'])['remaining_jump']), 1e-10)

    def test_missing_estimate_is_not_accepted_by_backend(self):
        r = sequence([(1, -1)], [1.] * 64)
        self.assertIsNone(r['events'][0]['jump'])
        self.assertFalse(r['events'][0]['selectable'])
        with self.assertRaisesRegex(ValueError, '不可估计'):
            correct(r, [0]*90, [1])

    def test_fixed_pixel_weights_determine_support(self):
        for changes in ([(30, -1)], [(30, -.36), (31, -1.32), (33, .68)]):
            with self.subTest(changes=changes):
                report = sequence(changes, [1.5] * 16 + [-1.] * 48, [100] * 16 + [1] * 48)
                event = report['events'][0]
                self.assertTrue(event['selectable'])
                self.assertAlmostEqual(event['spatial_support'], 1600 / 1648)

    def test_zero_net_burst_uses_independent_regional_responses(self):
        report = sequence([(15, -1), (30, -.5), (31, .5)], np.linspace(.55, 1.6, 64))
        self.assertTrue(report['events'][1]['selectable'])
        self.assertEqual(report['events'][1]['spatial_support'], 1.)


if __name__ == '__main__':
    unittest.main()
