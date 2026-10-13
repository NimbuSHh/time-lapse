"""Independent synthetic physics, plus real JPEG/XMP I/O. No Adobe claims."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from timelapse import joint_model as model, joint_feedback as pipeline, tone_xmp
from timelapse.project import digest
from timelapse.server import App
from tests.test_provenance import recipe


def sidecar(i=0, controls=(0, -40, 0)):
    xmp = recipe(frame=f'F{i:03}', date=f'2026:10:12 06:07:{i*3:02}')
    xmp = xmp.replace(b'c:Exposure2012="0"', b'c:Exposure2012="0" c:Highlights2012="-40" c:Whites2012="0"', 1)
    return tone_xmp.update(xmp, controls)


def render(i, xmp):
    e, h, w = tone_xmp.values(xmp)
    # Deliberately heterogeneous responses; the production code does not know
    # these coefficients. A camera event leaves mostly a highlight discontinuity.
    levels = np.array([[.09,.07,.06],[.32,.23,.12],[.60,.53,.40]])
    gain_h, gain_w = np.array([.001,.013,.003]), np.array([.0005,.002,.016])
    step = np.array([.025,.20,.14]) if i >= 6 else np.zeros(3)
    level = levels * 2**((.01*i + step + .93*e + gain_h*(h+40) + gain_w*w)[:, None])
    linear = np.repeat(np.repeat(level[None,:,:], 64, axis=0), 32, axis=1)
    rgb = np.where(linear <= .0031308, linear*12.92, 1.055*linear**(1/2.4)-.055)
    out = BytesIO()
    Image.fromarray(np.round(np.clip(rgb,0,1)*255).astype('uint8')).save(out,format='JPEG',quality=100,
        subsampling=0,xmp=xmp,icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    return out.getvalue()


class ToneXmpTests(unittest.TestCase):
    def test_three_controls_preserve_unrelated_bytes_and_nested_look(self):
        x = sidecar()
        spans = tone_xmp.scalar_spans(x)
        y = tone_xmp.update(x, [-.37,-100,-13])
        self.assertEqual(tone_xmp.values(y),[-.37,-100.,-13.])
        def rest(data):
            for a,b in sorted(tone_xmp.scalar_spans(data).values(),reverse=True):
                data=data[:a]+b'#'+data[b:]
            return data
        self.assertEqual(rest(x),rest(y))
        self.assertIn(b'c:Exposure2012="0.17"',y)

    def test_child_fields_and_alternate_prefixes(self):
        x=sidecar().replace(b'c:Highlights2012="-40"',b'').replace(b'c:Whites2012="0"',b'')
        x=x.replace(b'<c:Look>',b'<c:Highlights2012> -40 </c:Highlights2012><c:Whites2012>0</c:Whites2012><c:Look>')
        x=x.replace(b'xmlns:c=',b'xmlns:z=').replace(b'c:',b'z:')
        self.assertEqual(tone_xmp.values(tone_xmp.update(x,[.3,-50,8])),[.3,-50,8])

    def test_missing_duplicate_entity_and_limits_rejected(self):
        x=sidecar()
        bad=[x.replace(b'c:Highlights2012="-40"',b''),
             x.replace(b'<c:Look>',b'<c:Highlights2012>0</c:Highlights2012><c:Look>'),
             x.replace(b'c:Highlights2012="-40"',b'c:Highlights2012="&#45;40"')]
        for data in bad:
            with self.assertRaises(ValueError): tone_xmp.values(data)
        for controls in ([6,0,0],[0,-101,0],[0,0,1.5],[float('nan'),0,0]):
            with self.assertRaises(ValueError): tone_xmp.update(x,controls)

    def test_reconstructed_jpeg_recipe_is_raw_setting_not_already_applied(self):
        jpg=render(0,sidecar().replace(b'AlreadyApplied="False"',b'AlreadyApplied="True"'))
        x,e=tone_xmp.recipe_from_jpeg(jpg)
        self.assertIn(b'AlreadyApplied="False"',x)
        self.assertEqual(tone_xmp.values(x),[0,-40,0])


class JointModelTests(unittest.TestCase):
    def setUp(self):
        self.jac=np.array([[1.,.001,.0005],[.8,.015,.003],[.7,.005,.018],[.6,.006,.012],[.8,.003,.010],[.9,.002,.015]])
        self.y=np.array([-4.,-2.,-1.,-.6,-.8,-.7])

    def test_joint_response_improves_independent_render_and_preserves_midtone(self):
        current=np.array([0.,-40.,0.]); probes=np.asarray(model.probes(current))
        py=self.y+(probes-current)@self.jac.T
        requested=np.array([.005,-.10,-.12,-.08,-.07,-.09])
        result=model.solve(self.y,self.y+requested,current,probes,py)
        self.assertFalse(result['blocked'])
        rerender=self.y+self.jac@(np.array(result['final'])-current)
        self.assertLess(np.linalg.norm((rerender-self.y-requested)*model.WEIGHTS),np.linalg.norm(requested*model.WEIGHTS)*.5)
        self.assertLess(abs(rerender[0]-self.y[0]),.025)
        self.assertTrue(np.any(np.asarray(result['delta'])[1:]!=0))
        self.assertFalse(result['rendered'])

    def test_coupled_quantization_finds_improvement_missed_by_continuous_rounding(self):
        # Reduced numeric regression from a failed calibrated frame, no media.
        y=np.array([-5.395220168169152,-3.4138286620696903,-2.2970372298459023,
                    -.004089432175897387,-.9576839598975296,-1.6650704110935606])
        target=np.array([-5.388012404806227,-3.4098857438169308,-2.2927219204801172,
                         -.049582729358622915,-.9529432532605145,-1.6585156848839138])
        jac=np.array([[1.49406765,.0024240379,.0023956017],[1.49088317,.006929464,.0041453017],
          [1.44974366,.0079294491,.0051230472],[.09806756,-.0000341358,.0004361029],
          [.93243844,.005946043,.0065675218],[1.33102346,.0075136617,.0044383931]])
        current=np.array([0.,-100.,0.]); probes=np.array(model.probes(current))
        result=model.solve(y,target,current,probes,y+(probes-current)@jac.T)
        self.assertFalse(result['blocked'])
        predicted=y+jac@(np.array(result['final'])-current)
        simple_better=y+jac@np.array([0,1,0])
        self.assertLess(np.linalg.norm((predicted-target)*model.WEIGHTS),np.linalg.norm((y-target)*model.WEIGHTS)-1e-4)
        self.assertLessEqual(np.linalg.norm((predicted-target)*model.WEIGHTS),np.linalg.norm((simple_better-target)*model.WEIGHTS)+1e-8)
        self.assertTrue(np.all(predicted[3:]<=np.maximum(y[3:],model.CEILING)+1e-8))
        self.assertAlmostEqual(result['final'][0]*100,round(result['final'][0]*100))
        self.assertTrue(all(v==round(v) for v in result['final'][1:]))

    def test_unreachable_channel_target_holds_without_claiming_quality_pass(self):
        y=np.array([-4.,-2.,-1.,-.01,-1.,-2.]); target=y.copy(); target[3]-=.1
        current=np.array([0.,-100.,0.]); probes=np.array(model.probes(current))
        jac=np.zeros((6,3)); jac[:3]=np.diag([1.,.01,.01])
        result=model.solve(y,target,current,probes,y+(probes-current)@jac.T)
        self.assertFalse(result['blocked']); self.assertTrue(result['needs_review'])
        self.assertEqual(result['status'],'unchanged-at-discrete-optimum')
        self.assertEqual(result['final'],current.tolist()); self.assertFalse(result['rendered'])
        self.assertAlmostEqual(result['weighted_error_before'],result['weighted_error_predicted'])

    def test_singular_responses_never_invent_a_solution(self):
        current=np.array([0.,-100.,0.]); p=np.array(model.probes(current))
        py=np.tile(self.y,(3,1))
        result=model.solve(self.y,self.y-.2,current,p,py)
        self.assertTrue(result['blocked']); self.assertEqual(result['final'],current.tolist())

    def test_probe_limit_and_exact_one_parameter_changes(self):
        for controls in ([5,100,100],[-5,-100,-100],[0,-100,0]):
            p=np.asarray(model.probes(controls)); delta=p-controls
            self.assertTrue(np.all(np.count_nonzero(delta,axis=1)==1))
            for values in p:
                self.assertTrue(all(lo<=v<=hi for v,(lo,hi) in zip(values,tone_xmp.LIMITS)))

    def test_mixed_perturbations_rejected_and_cumulative_bound_holds(self):
        current=np.array([1.99,-40.,0.]); p=np.asarray(model.probes(current)); py=self.y+(p-current)@self.jac.T
        result=model.solve(self.y,self.y+.5,current,p,py,baseline=[0,-40,0])
        self.assertLessEqual(result['final'][0],2.)
        p[0,1]+=2
        with self.assertRaises(ValueError): model.solve(self.y,self.y+.1,current,p,py)

    def test_target_preserves_slow_ramp_and_segments_but_caps_channels(self):
        y=np.repeat(np.linspace(-3,-2,31)[:,None],6,axis=1)
        np.testing.assert_allclose(model.targets(y),y,atol=1e-12)
        y[15:,:3]+=1; y[:,3:]=0
        t=np.array(model.targets(y,[15]))
        np.testing.assert_allclose(t[:,:3],y[:,:3],atol=1e-12)
        self.assertTrue(np.all(t[:,3:]<=model.CEILING))
        smooth=np.array(model.targets(y))
        self.assertLess(abs(smooth[15,1]-smooth[14,1]),.2)

    def test_saturated_channel_is_a_solver_constraint(self):
        controls=np.array([0.,-50.,0.]); p=np.asarray(model.probes(controls))
        y=self.y.copy(); y[3]=0
        target=y+np.array([.15,.15,.1,-.05,.1,.1])
        result=model.solve(y,target,controls,p,y+(p-controls)@self.jac.T)
        if not result['blocked']:
            independent=y+self.jac@(np.asarray(result['final'])-controls)
            self.assertLessEqual(independent[3],.005)
            self.assertLess(np.linalg.norm((independent-target)*model.WEIGHTS),np.linalg.norm((y-target)*model.WEIGHTS))
        else:
            self.assertEqual(result['delta'],[0,0,0])

    def test_reverse_exposure_response_rejected(self):
        controls=np.array([0.,-40.,0.]); p=np.asarray(model.probes(controls))
        jac=self.jac.copy(); jac[:,0]*=-1
        result=model.solve(self.y,self.y-.1,controls,p,self.y+(p-controls)@jac.T)
        self.assertTrue(result['blocked']); self.assertEqual(result['status'],'inconsistent-slider-response')


class JointPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.previews=self.root/'original'; self.previews.mkdir()
        for i in range(12): (self.previews/f'F{i:03}.jpg').write_bytes(render(i,sidecar(i)))
        self.output=self.root/'out'

    def tearDown(self): self.tmp.cleanup()

    def prepare(self):
        return pipeline.prepare(self.previews,self.output,mid_roi=[0,0,.3,1],high_roi=[.34,0,1,1],window=9)

    def probes(self, package, subset=range(12)):
        dirs=[]
        for folder in ('baseline','probe-exposure','probe-highlights','probe-whites'):
            dest=self.root/(folder+'-jpg'); dest.mkdir(exist_ok=True)
            for i in subset:
                x=(Path(package)/folder/f'F{i:03}.xmp').read_bytes()
                (dest/f'F{i:03}.jpg').write_bytes(render(i,x))
            dirs.append(dest)
        return dirs

    def test_held_frames_form_complete_candidate_but_still_fail_strict_review(self):
        prepared=self.prepare(); p=Path(prepared['path']); dirs=self.probes(p)
        def held(y,target,controls,*args,**kwargs):
            return {'current':controls,'final':controls,'delta':[0.,0.,0.], 'prediction':y,
                    'status':'unchanged-at-discrete-optimum','blocked':False,'needs_review':True,'rendered':False}
        with patch.object(model,'solve',side_effect=held): result=pipeline.calibrate(p,*dirs,self.output)
        self.assertTrue(result['ready']); self.assertEqual(result['held_count'],12)
        self.assertEqual(len(list((Path(result['path'])/'corrected').glob('*.xmp'))),12)
        checked=pipeline.verify(result['path'],dirs[0],self.output)
        self.assertTrue(checked['acr_verified']); self.assertFalse(checked['numerically_accepted'])

    def test_real_io_synthetic_joint_roundtrip_and_frozen_second_round(self):
        before={p:digest(p.read_bytes()) for p in self.previews.iterdir()}
        prepared=self.prepare(); p=Path(prepared['path']); self.assertFalse(prepared['ready'])
        self.assertFalse((p/'corrected').exists())
        d=json.loads((p/'manifest.json').read_text())
        result=pipeline.calibrate(p,*self.probes(p),self.output)
        q=Path(result['path']); m=json.loads((q/'manifest.json').read_text())
        self.assertTrue(result['ready'],[s['status'] for s in m['solutions']])
        verified=self.root/'verified'; verified.mkdir()
        for i in range(12):
            x=(q/'corrected'/f'F{i:03}.xmp').read_bytes()
            (verified/f'F{i:03}.jpg').write_bytes(render(i,x))
        review=pipeline.verify(q,verified,self.output)
        self.assertTrue(review['acr_verified'])
        rm=json.loads((Path(review['path'])/'manifest.json').read_text())
        actual=np.array([o['brightness'] for o in rm['observations']])
        baseline=np.array([o['brightness'] for o in d['observations']]); target=np.array(d['target'])
        self.assertLess(np.linalg.norm(actual-target),np.linalg.norm(baseline-target))
        second=pipeline.prepare(verified,self.output,source=q)
        m2=json.loads((Path(second['path'])/'manifest.json').read_text())
        self.assertEqual(m2['target_sha256'],d['target_sha256'])
        self.assertEqual(m2['target'],d['target'])
        self.assertEqual([f['original_controls'] for f in m2['frames']],[f['controls'] for f in d['frames']])
        for path,sha in before.items(): self.assertEqual(digest(path.read_bytes()),sha)

    def test_partial_calibration_only_emits_explicit_samples(self):
        p=Path(self.prepare()['path']); result=pipeline.calibrate(p,*self.probes(p,range(5)),self.output)
        q=Path(result['path']); self.assertFalse(result['ready']); self.assertEqual(result['calibrated_count'],5)
        self.assertFalse((q/'corrected').exists()); self.assertEqual(len(list((q/'sample-corrected').glob('*.xmp'))),5)
        with self.assertRaisesRegex(ValueError,'小样'): pipeline.verify(q,self.previews,self.output)
        self.assertEqual(result['issues'][0]['count'], 7)
        self.assertIn('补齐', result['issues'][0]['action'])

    def test_second_round_clipping_budget_stays_anchored_to_first_reference(self):
        # Two individually sub-threshold increases must not accumulate into a
        # new clipping allowance. Use real JPEG pixels and both round manifests.
        def held(y,target,controls,*args,**kwargs):
            return {'current':controls,'final':controls,'delta':[0.,0.,0.], 'prediction':y,
                    'status':'unchanged-at-discrete-optimum','blocked':False,'needs_review':True,'rendered':False}

        def clipped_render(i, xmp, count):
            with Image.open(BytesIO(render(i,xmp))) as im:
                rgb=np.array(im.convert('RGB')); profile=im.info['icc_profile']
            rgb[0,:count]=[255,0,0]
            out=BytesIO()
            Image.fromarray(rgb).save(out,format='JPEG',quality=100,subsampling=0,xmp=xmp,icc_profile=profile)
            return out.getvalue()

        initial=Path(self.prepare()['path'])
        with patch.object(model,'solve',side_effect=held):
            first=Path(pipeline.calibrate(initial,*self.probes(initial),self.output)['path'])
        first_jpg=self.root/'first-corrected'; first_jpg.mkdir()
        for i in range(12):
            xmp=(first/'corrected'/f'F{i:03}.xmp').read_bytes()
            (first_jpg/f'F{i:03}.jpg').write_bytes(clipped_render(i,xmp,4))
        first_review=pipeline.verify(first,first_jpg,self.output)
        first_metrics=json.loads((Path(first_review['path'])/'verification.json').read_text())
        self.assertEqual(first_metrics['channel_clipping_worsened_frames'],[])

        prepared=Path(pipeline.prepare(first_jpg,self.output,source=first)['path'])
        probe_dirs=self.probes(prepared)
        for folder,dest in zip(('baseline','probe-exposure','probe-highlights','probe-whites'),probe_dirs):
            for i in range(12):
                xmp=(prepared/folder/f'F{i:03}.xmp').read_bytes()
                (dest/f'F{i:03}.jpg').write_bytes(clipped_render(i,xmp,4))
        with patch.object(model,'solve',side_effect=held):
            second=pipeline.calibrate(prepared,*probe_dirs,self.output)
        self.assertTrue(second['ready'])
        second_path=Path(second['path'])
        second_manifest=json.loads((second_path/'manifest.json').read_text())
        second_jpg=self.root/'second-corrected'; second_jpg.mkdir()
        for i in range(12):
            xmp=(second_path/'corrected'/f'F{i:03}.xmp').read_bytes()
            (second_jpg/f'F{i:03}.jpg').write_bytes(clipped_render(i,xmp,8))
        result=pipeline.verify(second_path,second_jpg,self.output)
        review=json.loads((Path(result['path'])/'manifest.json').read_text())
        before=np.array([o['channel_clip_fraction'] for o in second_manifest['observations']])
        after=np.array([o['channel_clip_fraction'] for o in review['observations']])
        self.assertTrue(np.all(after<=before+.001))
        self.assertEqual(review['metrics']['channel_clipping_worsened_frames'],list(range(12)))
        self.assertFalse(result['numerically_accepted'])
        self.assertEqual(review['metrics']['thresholds'],{
            'max_tone_error_ev':.08,'max_adjacent_tone_residual_ev':.06,
            'clip_fraction_increase':.001,'max_channel_tail_error_ev':.02})

    def test_guided_default_folders_work_without_manual_path_selection(self):
        p = Path(self.prepare()['path'])
        dirs = [p/'jpg'/name for name in ('baseline', 'probe-exposure', 'probe-highlights', 'probe-whites')]
        self.assertTrue(all(folder.is_dir() for folder in dirs))
        with self.assertRaisesRegex(ValueError, '基准小图尚未导出'):
            pipeline.calibrate(p, *dirs, self.output)
        for folder in dirs:
            for i in range(12):
                xmp = (p/folder.name/f'F{i:03}.xmp').read_bytes()
                (folder/f'F{i:03}.jpg').write_bytes(render(i, xmp))
        result = pipeline.calibrate(p, *dirs, self.output)
        self.assertTrue(result['ready'])
        self.assertEqual(result['issues'], [])
        self.assertEqual(len(list((Path(result['path'])/'corrected').glob('*.xmp'))), 12)
        with self.assertRaisesRegex(ValueError, '曝光小图无法读取'):
            pipeline.calibrate(p, dirs[0], p/'not-exported', *dirs[2:], self.output)

    def test_baseline_pixels_must_reproduce_original_before_fitting(self):
        p=Path(self.prepare()['path']); dirs=self.probes(p)
        # Same metadata but different pixels: a settings match alone is not enough.
        x=(p/'baseline'/'F000.xmp').read_bytes()
        (dirs[0]/'F000.jpg').write_bytes(render(10,x))
        result=pipeline.calibrate(p,*dirs,self.output)
        m=json.loads((Path(result['path'])/'manifest.json').read_text())
        self.assertFalse(result['ready'])
        self.assertEqual(result['issues'][0]['reason'], '基准显影与原小图不一致')
        self.assertEqual(m['solutions'][0]['status'],'baseline-render-not-reproduced')
        self.assertFalse((Path(result['path'])/'sample-corrected'/'F000.xmp').exists())

    def test_missing_one_probe_frame_and_forged_baseline_fail(self):
        p=Path(self.prepare()['path']); dirs=self.probes(p)
        f=dirs[3]/'F000.jpg'; old=f.read_bytes(); f.unlink()
        with self.assertRaisesRegex(ValueError,'完全相同'): pipeline.calibrate(p,*dirs,self.output)
        f.write_bytes(old)
        path=p/'manifest.json'; m=json.loads(path.read_text()); m['frames'][0]['original_controls'][0]+=1
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'原始参数基线'): pipeline.calibrate(p,*dirs,self.output)

    def test_wrong_recipe_identity_and_corrupted_probe_rejected(self):
        p=Path(self.prepare()['path']); dirs=self.probes(p)
        f=dirs[1]/'F000.jpg'; old=f.read_bytes()
        f.write_bytes(render(0,sidecar(0)))
        with self.assertRaisesRegex(ValueError,'参数'): pipeline.calibrate(p,*dirs,self.output)
        f.write_bytes(old)
        f.write_bytes((dirs[1]/'F001.jpg').read_bytes())
        with self.assertRaisesRegex(ValueError,'身份'): pipeline.calibrate(p,*dirs,self.output)
        f.write_bytes(old)
        x=p/'probe-exposure'/'F000.xmp'; x.write_bytes(x.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'快照'): pipeline.calibrate(p,*dirs,self.output)

    def test_target_tampering_and_source_mutation_stop_publication(self):
        p=Path(self.prepare()['path']); dirs=self.probes(p)
        f=p/'manifest.json'; m=json.loads(f.read_text()); m['target'][0][0]+=.1; f.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'目标指纹'): pipeline.calibrate(p,*dirs,self.output)
        output=self.root/'mutation-output'
        def mutate(i,n):
            if i==n:
                x=self.previews/'F000.jpg'; x.write_bytes(x.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'输入发生变化'): pipeline.prepare(self.previews,output,progress=mutate)
        self.assertEqual(list(output.iterdir()),[])

    def test_prepare_rejects_output_overlap(self):
        with self.assertRaisesRegex(ValueError,'输入目录'): pipeline.prepare(self.previews,self.previews/'out')

    def test_api_independent_of_original_analysis_and_correct_dispatch(self):
        app=App(self.root)
        try:
            with patch.object(app,'submit',side_effect=lambda fn:fn(None)):
                regions=app.dispatch('/api/joint/regions',{'previews':str(self.previews)})
                result=app.dispatch('/api/joint/prepare',{'previews':str(self.previews)})
            self.assertEqual(regions['selection']['analysed_frames'],12)
            self.assertEqual(result['count'],12); self.assertIsNone(app.project)
        finally: app.pool.shutdown()


if __name__=='__main__': unittest.main()
