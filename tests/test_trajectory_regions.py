import copy
import unittest

import numpy as np

from timelapse import trajectory_regions as regions


def scenes(layout='horizontal', n=15):
    """Photometric patches with no sky/sea orientation assumptions."""
    yy, xx = np.mgrid[:96, :128]
    if layout == 'horizontal':
        value = (.03 + .55*xx/127)
    elif layout == 'vertical':
        value = (.03 + .55*yy/95)
    elif layout == 'radial':
        value = .03+.5*((xx-64)**2/64**2+(yy-48)**2/48**2)/2
    else:
        raise AssertionError(layout)
    return [np.repeat((value*(.9+.01*i))[:, :, None], 3, axis=2) for i in range(n)]


class FrozenRegionTests(unittest.TestCase):
    def test_different_compositions_select_real_distinct_stable_regions(self):
        selected = []
        for layout in ('horizontal', 'vertical', 'radial'):
            images = scenes(layout)
            stats = [regions.statistics(image) for image in images]
            frozen = regions.choose(stats)
            selected.append(frozen['tiles'])
            before = copy.deepcopy(frozen)
            measured = [regions.measure(image, frozen) for image in images]
            self.assertGreater(measured[0]['primary']-measured[0]['secondary'], np.log2(1.4))
            for name in regions.NAMES:
                np.testing.assert_allclose([row[name]-measured[0][name] for row in measured],
                                           np.log2((.9+.01*np.arange(15))/.9), atol=1e-10)
            self.assertEqual(frozen, before)
            self.assertEqual(frozen, regions.choose(stats))
            self.assertEqual(len(set(sum(frozen['tiles'].values(), []))), sum(map(len, frozen['tiles'].values())))
        self.assertNotEqual(selected[0], selected[1])
        self.assertNotEqual(selected[1], selected[2])

    def test_constant_and_dark_or_clipped_images_cannot_invent_regions(self):
        for value in (0., .1, 1.):
            stats = [regions.statistics(np.full((96, 128, 3), value))]*10
            with self.assertRaises(ValueError):
                regions.choose(stats)

    def test_moving_local_patch_is_excluded_but_camera_step_is_not(self):
        images = scenes()
        for i, image in enumerate(images):
            image[:48, :64] *= .2 if i % 2 else 1
            if i >= 7:
                image *= .6
        frozen = regions.choose([regions.statistics(image) for image in images])
        for mask in regions.masks(frozen, images[0].shape).values():
            self.assertFalse(mask[:48, :64].any())
        measurements = [regions.measure(image, frozen) for image in images]
        expected = np.log2((.9+.07)/(.9+.06)*.6)
        for name in regions.NAMES:
            self.assertAlmostEqual(measurements[7][name]-measurements[6][name], expected)

    def test_manual_rectangles_remain_exact_and_validate_quality(self):
        manual = dict(zip(regions.NAMES, ([.6, 0, 1, .4], [.1, 0, .4, .4],
                                         [.1, .6, .4, 1], [.6, .6, 1, 1])))
        images = scenes()
        frozen = regions.choose([regions.statistics(image) for image in images], manual_rois=manual)
        self.assertEqual(frozen['rois'], manual)
        self.assertEqual(set(regions.measure(images[0], frozen)), set(regions.NAMES))
        manual['primary'][0] = 0
        self.assertEqual(frozen['rois']['primary'][0], .6)
        bad = copy.deepcopy(frozen)
        bad['rois']['primary'] = bad['rois']['secondary']
        with self.assertRaisesRegex(ValueError, '重叠'):
            regions.validate(bad)
        with self.assertRaises(ValueError):
            regions.choose([regions.statistics(np.zeros((96, 128, 3))) for _ in range(5)],
                           frozen['rois'])

    def test_contract_cannot_reselect_or_overlap_cells(self):
        frozen = regions.choose([regions.statistics(image) for image in scenes()])
        frozen['tiles']['primary'][0] = frozen['tiles']['secondary'][0]
        with self.assertRaises(ValueError):
            regions.measure(scenes()[0], frozen)
        frozen = regions.choose([regions.statistics(image) for image in scenes()])
        frozen['per_frame_pixel_reselection'] = True
        with self.assertRaises(ValueError):
            regions.validate(frozen)


if __name__ == '__main__':
    unittest.main()
