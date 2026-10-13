import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from timelapse import auto_regions, joint_feedback, joint_model
from tests import test_joint_feedback as fixtures


class AutomaticRegionTests(unittest.TestCase):
    def sequence(self):
        frames=[]
        for i in range(24):
            a=np.zeros((96,128,3))+0.002
            a[:,:48]=.05*2**(i*.01)
            a[:,48:88]=.20*2**(i*.01)
            a[:,88:]=.50*2**(i*.01)
            # A moving white spot should not become a persistent reference.
            a[i*4:(i+1)*4,64:68]=1.
            frames.append(a)
        return frames

    def test_selects_repeatable_distinct_fixed_regions_and_avoids_clipped_spot(self):
        frames=self.sequence(); stats=[auto_regions.statistics(a) for a in frames]
        selection=auto_regions.choose(stats)
        self.assertEqual(selection,auto_regions.choose(stats))
        mid, high=auto_regions.masks(selection,frames[0].shape)
        self.assertFalse(np.any(mid&high))
        self.assertGreater(high[:,48:88].sum(),0)
        self.assertGreater(high[:,88:].sum(),0)
        for a in frames:
            self.assertGreater(np.mean(a[high]),np.mean(a[mid])*1.6)
        # Selection itself is independent of the subsequently rendered pixels.
        modified=frames[0].copy(); modified[:, :48]*=1.2
        observed=joint_model.measure(modified,None,None,frames[0],selection)
        before=joint_model.measure(frames[0],None,None,frames[0],selection)
        self.assertGreater(observed['brightness'][0],before['brightness'][0])

    def test_flat_black_and_all_white_sequences_require_manual_help(self):
        for value in (0.,.1,1.):
            stats=[auto_regions.statistics(np.full((96,128,3),value)) for _ in range(6)]
            with self.assertRaisesRegex(ValueError,'手动'): auto_regions.choose(stats)

    def test_grid_validation_rejects_overlap_and_invalid_indices(self):
        selected=auto_regions.choose([auto_regions.statistics(a) for a in self.sequence()])
        for key,value in [('mid_tiles',[0]*16),('high_tiles',[999]*8),('rows',0),('method','unknown')]:
            with self.assertRaises(ValueError): auto_regions.masks({**selected,key:value},(96,128,3))


class AutomaticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.JointPipelineTests(); self.fixture.setUp()

    def tearDown(self): self.fixture.tearDown()

    def test_default_auto_complete_feedback_and_next_round_freeze(self):
        f=self.fixture
        result=joint_feedback.prepare(f.previews,f.output)
        p=Path(result['path']); m=json.loads((p/'manifest.json').read_text())
        self.assertEqual(m['region_mode'],'auto')
        self.assertEqual(result['measurement_selection'],m['measurement_selection'])
        expected=joint_feedback.suggest_regions(f.previews)['selection']
        self.assertEqual(expected,m['measurement_selection'])
        solution=joint_feedback.calibrate(p,*f.probes(p),f.output)
        s=Path(solution['path']); sm=json.loads((s/'manifest.json').read_text())
        self.assertTrue(solution['ready'],[row['status'] for row in sm['solutions']])
        latest=f.root/'latest'; latest.mkdir()
        for i in range(12):
            x=(s/'corrected'/f'F{i:03}.xmp').read_bytes()
            (latest/f'F{i:03}.jpg').write_bytes(fixtures.render(i,x))
        with patch('timelapse.auto_regions.choose',side_effect=AssertionError('must keep frozen grid')):
            next_result=joint_feedback.prepare(latest,f.output,source=s)
        nm=json.loads((Path(next_result['path'])/'manifest.json').read_text())
        self.assertEqual(nm['measurement_sha256'],m['measurement_sha256'])
        self.assertEqual(nm['target'],m['target'])
        self.assertEqual(nm['measurement_selection'],m['measurement_selection'])

    def test_grid_tampering_is_detected_before_calibration(self):
        f=self.fixture; p=Path(joint_feedback.prepare(f.previews,f.output)['path']); dirs=f.probes(p)
        path=p/'manifest.json'; m=json.loads(path.read_text()); m['measurement_selection']['mid_tiles'].pop()
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'指纹'): joint_feedback.calibrate(p,*dirs,f.output)

    def test_no_sequence_writes_and_manual_compatibility(self):
        f=self.fixture; before={p:p.read_bytes() for p in f.previews.iterdir()}
        joint_feedback.suggest_regions(f.previews)
        p=Path(f.prepare()['path']); m=json.loads((p/'manifest.json').read_text())
        self.assertEqual(m['region_mode'],'manual'); self.assertIsNone(m['measurement_selection'])
        for path,data in before.items(): self.assertEqual(path.read_bytes(),data)


if __name__=='__main__': unittest.main()
