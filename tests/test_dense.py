"""Short camera plateaus must be inferred and selected as one bounded burst."""
from pathlib import Path
import re
import tempfile
import unittest
import numpy as np
from PIL import Image
from timelapse.analysis import infer, correct, event_residual
from timelapse.demo import create
from timelapse.project import Project, digest
from tests.test_sequence import simulate_linear_render


def fixture_arrays(n=90, changes=((30,-.36),(31,-1.32),(33,.68)), gain=1.5):
    camera=np.zeros(n)
    for i,d in changes: camera[i:]+=d
    y=np.linspace(-4.5,-2.5,n)+gain*camera
    measures=[{'tiles':[float(v)]*64,'valid':[True]*64} for v in y]
    return y,camera,measures


def infer_arrays(camera,measures,exposures=None,times=None):
    n=len(camera)
    return infer(measures, exposures if exposures is not None else [0]*n,
                 camera.tolist(),times if times is not None else list(range(n)),'manual')


class DenseTests(unittest.TestCase):
    def test_single_frame_platform_is_measured_and_grouped(self):
        y,camera,measures=fixture_arrays()
        r=infer_arrays(camera,measures);e=r['events'][0]
        self.assertTrue(e['selectable']);self.assertEqual(e['indices'],[30,31,33])
        np.testing.assert_allclose([v['jump'] for v in e['components']],np.array([-.36,-1.32,.68])*1.5,atol=1e-10)
        c=correct(r,[0]*len(y),[30],strength=1)
        np.testing.assert_allclose(c['target'],np.linspace(y[0],y[-1],len(y)),atol=1e-10)
        q=event_residual(c['target'],e,r['boundaries'])
        self.assertLess(q['max_component_jump'],1e-10)
        with self.assertRaises(ValueError):correct(r,[0]*len(y),[31])

    def test_unselected_group_is_not_flattened(self):
        y,camera,measures=fixture_arrays()
        r=infer_arrays(camera,measures);c=correct(r,[0]*len(y),[],strength=1)
        np.testing.assert_allclose(c['target'],y,atol=1e-10)

    def test_missing_time_independent_edit_or_bad_metadata_refused(self):
        y,camera,measures=fixture_arrays()
        times=np.arange(len(y),dtype=float);times[32:]+=20
        self.assertFalse(infer_arrays(camera,measures,times=times.tolist())['events'][0]['selectable'])
        edits=np.zeros(len(y));edits[32:]=.3
        event=infer_arrays(camera,measures,exposures=edits.tolist())['events'][0]
        self.assertFalse(event['selectable'])
        self.assertIn('独立的 XMP',event['reason'])
        camera[29]+=.1
        self.assertFalse(infer_arrays(camera,measures)['events'][0]['selectable'])

    def test_edited_burst_measures_only_remaining_image_steps(self):
        for compensation in (0., .75, 1.):
            with self.subTest(compensation=compensation):
                y,camera,_=fixture_arrays(gain=1.5)
                exposures=-compensation*camera
                rendered=y+1.5*exposures
                measures=[{'tiles':[float(v)]*64,'valid':[True]*64} for v in rendered]
                report=infer_arrays(camera,measures,exposures=exposures.tolist())
                event=report['events'][0]
                self.assertTrue(event['selectable'])
                np.testing.assert_allclose([c['jump'] for c in event['components']],
                    np.array([-.36,-1.32,.68])*1.5*(1-compensation),atol=1e-10)
                result=correct(report,exposures.tolist(),[30],strength=0)
                np.testing.assert_allclose(result['target'],np.linspace(rendered[0],rendered[-1],len(y)),atol=1e-10)
                np.testing.assert_allclose(np.array(result['final'])-exposures,result['delta'],atol=1e-10)
                if compensation:
                    self.assertFalse(event['recommended'])
                    self.assertIsNone(event['response_gain'])
                    self.assertEqual(result['response']['support'],0)
                    self.assertEqual(result['calibration_required'],compensation!=1.)

    def test_cancelled_xmp_edits_do_not_become_response_evidence(self):
        y,camera,measures=fixture_arrays()
        exposures=np.zeros(len(y));exposures[31:33]=.4
        report=infer_arrays(camera,measures,exposures=exposures.tolist())
        event=report['events'][0]
        self.assertEqual(event['xmp_delta'],0.)
        self.assertTrue(event['selectable'])
        self.assertFalse(event['recommended'])
        self.assertEqual([c['xmp_delta'] for c in event['components']],[0.,.4,-.4])
        # A transient in the natural curve makes smoothing across a protected
        # edit observable, even though the group's net XMP change is zero.
        report['measured'][32]+=.1
        result=correct(report,exposures.tolist(),[30],strength=1)
        baseline=np.linspace(y[0],y[-1],len(y))
        np.testing.assert_allclose(result['target'][31:33],baseline[31:33]+[0.,.1],atol=1e-10)
        self.assertEqual(result['response']['support'],0)
        self.assertTrue(result['calibration_required'])

    def test_smooth_xmp_ramp_does_not_block_image_measurement(self):
        y,camera,measures=fixture_arrays()
        report=infer_arrays(camera,measures,exposures=np.linspace(0,1,len(y)).tolist())
        self.assertTrue(report['events'][0]['selectable'])
        self.assertFalse(report['events'][0]['recommended'])

    def test_missing_metadata_has_a_specific_reason(self):
        _,camera,measures=fixture_arrays()
        cameras=camera.tolist();cameras[26]=None
        event=infer(measures,[0]*90,cameras,list(range(90)),'manual')['events'][0]
        self.assertFalse(event['selectable'])
        self.assertIn('缺失拍摄参数',event['reason'])
        self.assertNotIn('曝光编辑',event['reason'])

    def test_inconsistent_transient_and_tile_disagreement_warn_but_allow_selection(self):
        for inconsistent_tiles in (False,True):
            with self.subTest(tiles=inconsistent_tiles):
                y,camera,measures=fixture_arrays()
                for i in (31,32):
                    if inconsistent_tiles:
                        measures[i]['tiles']=[y[i]+(-.6 if j<32 else .6) for j in range(64)]
                    else: measures[i]['tiles']=[y[i]+1]*64
                report = infer_arrays(camera,measures)
                event = report['events'][0]
                self.assertTrue(event['selectable'])
                self.assertFalse(event['recommended'])
                result = correct(report, [0]*len(y), [30], strength=0)
                self.assertEqual(result['event_warnings'][0]['index'], 30)
                self.assertEqual(result['response']['support'], 0)
                self.assertTrue(result['calibration_required'])
                self.assertLess(event_residual(result['target'],event,report['boundaries'])['max_component_jump'], 1e-10)

    def test_net_zero_needs_independent_response_evidence(self):
        y,camera,measures=fixture_arrays(changes=((30,-.5),(31,.5)))
        report = infer_arrays(camera,measures)
        self.assertTrue(report['events'][0]['selectable'])
        self.assertFalse(report['events'][0]['recommended'])
        self.assertTrue(correct(report, [0]*len(y), [30])['calibration_required'])
        y,camera,measures=fixture_arrays(changes=((15,-1),(30,-.5),(31,.5)))
        self.assertTrue(infer_arrays(camera,measures)['events'][1]['selectable'])

    def test_verification_cannot_hide_dip_in_net_sum(self):
        y,camera,measures=fixture_arrays()
        r=infer_arrays(camera,measures);target=np.linspace(-4,-3,len(y))
        target[31:33]-=1
        q=event_residual(target,r['events'][0],r['boundaries'])
        self.assertAlmostEqual(q['remaining_jump'],0)
        self.assertAlmostEqual(q['max_component_jump'],1)

    def test_real_file_pipeline_exports_and_verifies_components(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);paths=create(root/'synthetic',90)
            y,camera,_=fixture_arrays(gain=1)
            for i in range(90):
                stem=f'DSC_{i+1:04d}';x=Path(paths['raw_dir'])/(stem+'.xmp')
                x.write_text(re.sub(r'exif:ExposureTime="[^"]+"',f'exif:ExposureTime="{2**camera[i]/30}"',x.read_text()))
                linear=2**y[i];srgb=12.92*linear if linear<=.0031308 else 1.055*linear**(1/2.4)-.055
                Image.new('RGB',(192,128),(round(srgb*255),)*3).save(Path(paths['preview_dir'])/(stem+'.jpg'),quality=98)
            p=Project(**paths,srgb=True);e=p.report['events'][0]
            self.assertTrue(e['selectable'])
            before=[digest(f.xmp.read_bytes()) for f in p.frames]
            r=p.calculate({'selected':[30],'strength':1})
            out=p.export(root/'out',r['revision'],p.id)
            original_check=p.verify(paths['preview_dir'],out['package_id'])
            self.assertGreater(original_check['events'][0]['max_component_jump'],1)
            rendered=simulate_linear_render(p,root/'rendered')
            check=p.verify(rendered,out['package_id'])
            self.assertEqual(len(check['events'][0]['components']),3)
            self.assertLess(check['events'][0]['max_component_jump'],.06)
            self.assertLess(check['rmse_ev'],.025)
            self.assertEqual(before,[digest(f.xmp.read_bytes()) for f in p.frames])

if __name__=='__main__': unittest.main()
