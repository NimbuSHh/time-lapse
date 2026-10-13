import copy
import unittest

import numpy as np

from timelapse import refinement_model as model


TARGET = np.array([-4., -2., -1., -.05, -.2, -.3])


def measured(exposure=0., mid=.3, *, tone=None, tail=None, clip=None, controls=None):
    error = np.array(list(tone if tone is not None else [mid]*3)+list(tail if tail is not None else [0.]*3))
    return {'controls': controls or [exposure, -100., 0.],
            'brightness': (TARGET+error).tolist(),
            'channel_clip_fraction': clip if clip is not None else [.01, 0., 0.]}


class AssessmentTests(unittest.TestCase):
    def test_tail_near_old_weighted_noise_is_not_accepted(self):
        row = measured(mid=0, tail=[.045, 0, 0])
        self.assertLess(np.linalg.norm((np.array(row['brightness'])-TARGET)*[2, 2, 1, .8, .8, .8]), .04)
        result = model.assess(row, TARGET, [.01, 0, 0])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['violations']['tail'], [0])
        self.assertAlmostEqual(result['normalized_max'], 2.25)

    def test_tail_improvement_does_not_hide_increased_clipping(self):
        result = model.assess(measured(mid=0, tail=[-.2]*3, clip=[.0115, 0, 0]), TARGET, [.01, 0, 0])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['violations']['tail'], [])
        self.assertEqual(result['violations']['clip'], [0])
        self.assertTrue(model.assess(measured(mid=0, tail=[-.2]*3), TARGET, [.01, 0, 0])['accepted'])

    def test_clipping_budget_uses_supplied_first_reference(self):
        row = measured(mid=0, clip=[.0116, 0, 0])
        self.assertTrue(model.assess(row, TARGET, [.0108, 0, 0])['accepted'])
        self.assertFalse(model.assess(row, TARGET, [.01, 0, 0])['accepted'])

    def test_neighbors_use_target_residual_not_absolute_brightness(self):
        row = measured(tone=[.04, 0, 0])
        self.assertTrue(model.assess(row, TARGET, [.01, 0, 0])['accepted'])
        result = model.assess(row, TARGET, [.01, 0, 0], [[-.04, 0, 0]])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['violations']['adjacent'], [[0, 0]])

    def test_exact_thresholds_are_not_loosened(self):
        target = np.zeros(6)
        row = {'brightness': [.08, 0, 0, .02, 0, 0], 'channel_clip_fraction': [.001, 0, 0]}
        self.assertTrue(model.assess(row, target, [0, 0, 0])['accepted'])
        for field, index in [('brightness', 0), ('brightness', 3), ('channel_clip_fraction', 0)]:
            changed = copy.deepcopy(row)
            changed[field][index] += 1e-9
            self.assertFalse(model.assess(changed, target, [0, 0, 0])['accepted'])

    def test_invalid_measurements_fail_instead_of_becoming_candidates(self):
        for clip in ([float('nan'), 0, 0], [-.1, 0, 0], [1.1, 0, 0]):
            with self.assertRaises(ValueError):
                model.assess(measured(clip=clip), TARGET, [.01, 0, 0])
        with self.assertRaises(ValueError):
            model.assess(measured(), TARGET, [.01, 0, 0], [[0, 0]])


class ExposureSuggestionTests(unittest.TestCase):
    def suggest(self, history, **kwargs):
        return model.suggest(history, TARGET, [.01, 0, 0], 0., **kwargs)

    def test_only_real_measurement_can_be_accepted(self):
        result = self.suggest([measured(mid=0)])
        self.assertEqual(result['status'], 'accepted-measurement')
        self.assertFalse(result['needs_render'])
        self.assertIsNone(result['candidate'])
        result = self.suggest([measured(mid=0, tail=[.045, 0, 0])])
        self.assertEqual(result['status'], 'needs-other-controls')
        self.assertFalse(result['assessment']['accepted'])

    def test_first_step_is_bounded_probe_and_preserves_other_controls(self):
        history = [measured(mid=-.5, controls=[0, -80, -33, 248])]
        original = copy.deepcopy(history)
        result = self.suggest(history)
        self.assertEqual(result['candidate'], [.2, -80, -33, 248])
        self.assertEqual(result['status'], 'needs-render')
        self.assertTrue(result['needs_render'])
        self.assertFalse(result['assessment']['accepted'])
        self.assertEqual(history, original)

    def test_secant_uses_observed_slope_not_one_ev_assumption(self):
        history = [measured(0, -.6), measured(.2, -.2)]
        result = self.suggest(history)
        self.assertEqual(result['candidate'][0], .3)
        self.assertIsNone(result['bracket'])

    def test_measured_bracket_precedes_extrapolation(self):
        history = [measured(0, -.3), measured(.4, .5)]
        result = self.suggest(history)
        self.assertEqual(result['bracket'], [0, .4])
        self.assertEqual(result['candidate'][0], .15)
        self.assertLessEqual(abs(result['candidate'][0]-history[result['measured_best_index']]['controls'][0]), .2)

    def test_flat_and_reversed_response_do_not_trigger_repeated_probes(self):
        for mid in (-.3, -.4):
            result = self.suggest([measured(0, -.3), measured(.2, mid)])
            self.assertEqual(result['status'], 'inconsistent-response')
            self.assertFalse(result['needs_render'])

    def test_curve_and_highlight_variants_do_not_supply_false_secant(self):
        first = measured(0, -.7, controls=[0, -100, 0, 255])
        second = measured(.2, -.3, controls=[.2, -90, -10, 245])
        result = self.suggest([first, second])
        self.assertEqual(result['candidate'], [.4, -90, -10, 245])
        self.assertIn('First bounded', result['reason'])

    def test_no_duplicate_render_when_bracket_grid_exhausted(self):
        result = self.suggest([measured(0, -.3), measured(.01, .4)])
        self.assertEqual(result['status'], 'stalled')
        self.assertIsNone(result['candidate'])

    def test_same_recipe_measurement_conflict_is_explicit(self):
        result = self.suggest([measured(0, -.5), measured(0, -.3)])
        self.assertEqual(result['status'], 'inconsistent-measurement')
        self.assertIsNone(result['candidate'])

    def test_duplicate_rows_do_not_fake_stagnation_history(self):
        result = self.suggest([measured(0, -.3)]*8)
        self.assertTrue(result['needs_render'])
        self.assertEqual(result['candidate'][0], .2)

    def test_distinct_nonimproving_trials_stop(self):
        history = [measured(0, -.3)]
        history += [measured(e, -.3-e) for e in [.1, .2, .3, .4]]
        result = self.suggest(history)
        self.assertEqual(result['status'], 'stalled')
        self.assertFalse(result['needs_render'])

    def test_cumulative_and_global_exposure_bounds(self):
        result = self.suggest([measured(1.95, -.3)], max_step=.75)
        self.assertEqual(result['candidate'][0], 2.)
        self.assertEqual(self.suggest([measured(2., -.3)])['status'], 'stalled')
        self.assertEqual(model.quantize_controls([5.9, -120, 110, 260], 4.), [5., -100., 100., 255.])
        candidate = model.quantize_controls([2.01, -99.4, 1.6, 219], -.001)
        self.assertEqual(candidate, [1.99, -99., 2., 220.])
        with self.assertRaises(ValueError):
            self.suggest([measured()], max_step=1.01)

    def test_incompatible_fixed_neighbors_are_not_hidden(self):
        result = self.suggest([measured()], neighbor_residuals=[[-.08, 0, 0], [.08, 0, 0]])
        self.assertEqual(result['status'], 'neighbors-incompatible')
        self.assertIsNone(result['candidate'])


if __name__ == '__main__':
    unittest.main()
