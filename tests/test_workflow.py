"""Offline product round trips with actual JPEG/XMP and ffmpeg video encoding.

The synthetic renderer replaces Adobe only. Preparation, region selection,
measurement, trajectory checks, preview encoding and export receipts are real.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from timelapse import automatic, joint_feedback, preview_video, tone_xmp, trajectory, workflow, workspace
from timelapse.photoshop import Cancelled
from timelapse.provenance import xmp_evidence
from tests.test_refinement import linear_recipe


def capture_recipe(i, camera_event=None):
    stamp=(datetime(2026,10,12,6,7)+timedelta(seconds=i*3)).strftime("%Y:%m:%d %H:%M:%S")
    recipe=linear_recipe(i).replace(f"2026:10:12 06:07:{i*3:02}".encode(),stamp.encode())
    if camera_event is not None and i>=camera_event:
        recipe=recipe.replace(b'ExposureTime="1/250"',b'ExposureTime="1/125"')
    return recipe


def make_renderer(source, layout, natural_rate=.006):
    class SyntheticRenderer:
        identity = 'independent-synthetic-gradient-renderer-v1-'+layout
        calls = []

        def __init__(self, folder, cancelled):
            self.folder = Path(folder)
            self.cancelled = cancelled
            self.last_render = None

        def preflight(self):
            if self.cancelled():
                raise Cancelled('synthetic cancelled preflight')

        def render(self, raw, destination, edge):
            if self.cancelled():
                raise Cancelled('synthetic cancelled render')
            raw, destination = Path(raw), Path(destination)
            assert self.folder in raw.parents
            assert self.folder in destination.parents
            assert raw.parent != source
            assert raw.stat().st_ino != (source/raw.name).stat().st_ino
            assert raw.read_bytes() == (source/raw.name).read_bytes()
            aux = raw.with_suffix('.acr')
            assert aux.read_bytes() == (source/aux.name).read_bytes()
            i = int(raw.stem[1:])
            # No source XMP is also supported: the initial render contains the
            # actual default recipe and capture identity, just as Adobe does.
            xmp = raw.with_suffix('.xmp').read_bytes() if raw.with_suffix('.xmp').exists() else capture_recipe(i)
            e,h,w = tone_xmp.values(xmp)
            width,height = (384,256) if edge else (768,512)
            yy,xx = np.mgrid[0:height,0:width]/np.array([height-1,width-1])[:,None,None]
            if layout == 'diagonal':
                light = .035+.31*(.8*xx+.2*yy)
            else:
                light = .04+.30*np.minimum(1.,((xx-.65)**2+(yy-.4)**2)*2.5)
            shutter=xmp_evidence(xmp)['identity']['ExposureTime']
            camera=np.log2(shutter/.004)
            gain = natural_rate*i+camera+e+.004*(h+40)*np.sqrt(light)+.012*w*light
            linear = light[:,:,None]*np.array([1.,.92,.82])*2**gain[:,:,None]
            # The response is independent from the solver. If a master curve
            # is requested, approximate its visible white endpoint effect.
            curve = str(xmp_evidence(xmp)['crs'].get('ToneCurvePV2012',''))
            ceiling = next((v for v in (248,240,232) if '255, '+str(v) in curve),255)
            rgb = np.where(linear<=.0031308,linear*12.92,1.055*linear**(1/2.4)-.055)*ceiling/255
            image=Image.fromarray(np.round(np.clip(rgb,0,1)*255).astype('uint8'))
            destination.parent.mkdir(parents=True,exist_ok=True)
            image.save(destination,format='JPEG',quality=100,subsampling=0,xmp=xmp,
                       icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
            self.last_render={'raw':str(raw.resolve()),'native_size':[768,512],
                              'output_size':[width,height],'edge':edge}
            self.calls.append({'raw':str(raw),'jpg':str(destination),'index':i,'edge':edge,
                               'recipe_sha256':automatic.file_hash(raw.with_suffix('.xmp')) if raw.with_suffix('.xmp').exists() else None})
            return destination
    return SyntheticRenderer


class ProductWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            preview_video._tools()
        except ValueError as exc:
            raise unittest.SkipTest('Actual ffmpeg/ffprobe required for offline video round trip: '+str(exc))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.source=self.root/'source';self.source.mkdir()
        self.output=workspace.output_root(self.source)
        self.n=12
        self.old_execute=patch.object(automatic.Run,'execute',side_effect=AssertionError('old automatic solver must not run'))
        self.old_calibrate=patch.object(joint_feedback,'calibrate',side_effect=AssertionError('old calibration loop must not run'))
        self.old_execute.start();self.old_calibrate.start()
        self.addCleanup(self.old_execute.stop);self.addCleanup(self.old_calibrate.stop)

    def prepare(self, with_xmp=True, layout='diagonal', camera_event=None, natural_rate=.006):
        for i in range(self.n):
            (self.source/f'F{i:03}.ARW').write_bytes(f'isolated-synthetic-raw-{i}'.encode())
            (self.source/f'F{i:03}.acr').write_bytes(f'auxiliary-settings-{i}'.encode())
            if with_xmp:
                (self.source/f'F{i:03}.xmp').write_bytes(capture_recipe(i,camera_event))
        self.before=automatic.inputs(self.source)[1]
        self.hashes={p.name:automatic.file_hash(p) for p in self.source.iterdir() if p.is_file()}
        self.renderer=make_renderer(self.source,layout,natural_rate)

    def assert_sources_unchanged(self):
        self.assertEqual(automatic.inputs(self.source)[1],self.before)
        self.assertEqual({p.name:automatic.file_hash(p) for p in self.source.iterdir() if p.is_file()},self.hashes)

    def confirmation(self,result):
        return {'confirmed':True,'run_id':result['run_id'],'video_sha256':result['sha256'],
                **{k:result[k] for k in ('selection_sha256','target_sha256','effective_policy_sha256')}}

    def check_preview(self,result):
        self.assertEqual(result['state'],'awaiting-user-review')
        self.assertTrue(result['numeric_conditions_met'])
        self.assertTrue(result['encoded_verified'])
        self.assertFalse(result['full_export_authorized'])
        self.assertFalse(result['visual_playback_verified'])
        self.assertEqual(result['count'],self.n)
        self.assertEqual(result['frame_count'],self.n)
        self.assertEqual(automatic.file_hash(result['video']),result['sha256'])
        manifest=json.loads(Path(result['manifest']).read_text())
        self.assertEqual([frame['index'] for frame in manifest['frames']],list(range(1,self.n+1)))
        self.assertEqual(len(manifest['frames']),self.n)
        current=workflow.load_latest(self.output)
        self.assertEqual(current['version'],trajectory.PRODUCT_VERSION)
        plan=json.loads((Path(current['path'])/'trajectory-target.json').read_text())
        self.assertEqual(set(plan['regional_targets']),{'primary','secondary','check_a','check_b'})
        self.assertEqual(plan['regional_measurement_contract']['mode'],'automatic')
        self.assertFalse((self.output/'automatic-latest.json').exists())
        self.assert_sources_unchanged()

    def test_new_raw_direct_preview_then_explicit_native_export_and_idempotent_start(self):
        self.prepare()
        result=workflow.run(self.source,self.output,renderer_factory=self.renderer)
        self.check_preview(result)
        # This already smooth fixture needs only one initial render per RAW.
        # No old probe rounds, full-size references or solver iterations occur.
        self.assertEqual(len(self.renderer.calls),self.n)
        self.assertTrue(all(call['edge']==1600 and '/initial/raw/' in call['raw'] for call in self.renderer.calls))
        before=list(self.renderer.calls)
        self.assertEqual(workflow.run(self.source,self.output,renderer_factory=self.renderer),result)
        self.assertEqual(self.renderer.calls,before)
        with self.assertRaisesRegex(ValueError,'用户确认'):
            workflow.export(self.output,{'approved':True},renderer_factory=self.renderer)
        self.assertEqual(self.renderer.calls,before)
        final=workflow.export(self.output,self.confirmation(result),renderer_factory=self.renderer)
        self.assertEqual(final['state'],'done')
        self.assertEqual(final['video'],result['video'])
        self.assertEqual(final['sha256'],result['sha256'])
        self.assertTrue(final['delivery']['verified'])
        self.assertFalse(final['delivery']['visual_playback_verified'])
        self.assertEqual(len(list(Path(final['delivery']['jpgs']).glob('*.jpg'))),self.n)
        self.assertEqual(len(list(Path(final['delivery']['xmps']).glob('*.xmp'))),self.n)
        native=[call for call in self.renderer.calls if call['edge']==0]
        self.assertEqual([c['index'] for c in native],list(range(self.n)))
        for receipt in final['delivery']['frames']:
            self.assertEqual(receipt['render_evidence']['native_size'],[768,512])
            with Image.open(receipt['jpg']) as image:
                image.load();self.assertEqual(image.size,(768,512))
        completed=list(self.renderer.calls)
        self.assertEqual(workflow.run(self.source,self.output,renderer_factory=self.renderer),final)
        self.assertEqual(workflow.export(self.output,self.confirmation(result),renderer_factory=self.renderer),final)
        self.assertEqual(self.renderer.calls,completed)
        self.assert_sources_unchanged()

    def test_missing_sidecars_preparation_cancel_resume_and_export_cancel_resume(self):
        self.prepare(with_xmp=False,layout='rings')
        with self.assertRaises(Cancelled):
            workflow.run(self.source,self.output,renderer_factory=self.renderer,
                         cancelled=lambda:len(self.renderer.calls)>=3)
        prepared=workflow.load_preparation(self.output)
        self.assertEqual(prepared['state'],'stopped')
        self.assertEqual(len(self.renderer.calls),3)
        first=list(self.renderer.calls)
        result=workflow.resume(self.output,renderer_factory=self.renderer)
        self.check_preview(result)
        self.assertEqual(len(self.renderer.calls),self.n)
        self.assertEqual(self.renderer.calls[:3],first)
        self.assertEqual([c['index'] for c in self.renderer.calls],list(range(self.n)))
        prepared=workflow.load_preparation(self.output)
        base_folder=Path(prepared['phases']['第 1 轮准备校准']['result']['path'])
        for i in range(self.n):
            # Reconstructed baseline is justified by embedded actual JPEG
            # metadata despite the complete absence of input sidecars.
            recipe=(base_folder/'baseline'/f'F{i:03}.xmp').read_bytes()
            self.assertEqual(xmp_evidence(recipe)['identity']['ImageUniqueID'],f'F{i:03}')
            self.assertFalse((self.source/f'F{i:03}.xmp').exists())
        confirmation=self.confirmation(result)
        with self.assertRaises(Cancelled):
            workflow.export(self.output,confirmation,renderer_factory=self.renderer,
                            cancelled=lambda:sum(c['edge']==0 for c in self.renderer.calls)>=2)
        current=workflow.load_latest(self.output)
        self.assertEqual(current['state'],'stopped')
        self.assertEqual(len(list((Path(current['export']['path'])/'receipts').glob('*.json'))),2)
        final=workflow.export(self.output,confirmation,renderer_factory=self.renderer)
        self.assertEqual(final['state'],'done')
        self.assertEqual([c['index'] for c in self.renderer.calls if c['edge']==0],list(range(self.n)))
        self.assert_sources_unchanged()

    def test_camera_exposure_step_is_measured_and_corrected_before_complete_video(self):
        self.n=24
        event=self.n//2
        self.prepare(camera_event=event,natural_rate=.01)
        result=workflow.run(self.source,self.output,renderer_factory=self.renderer)
        self.check_preview(result)
        current=workflow.load_latest(self.output)
        folder=Path(current['path'])
        plan=json.loads((folder/'trajectory-target.json').read_text())
        self.assertEqual(plan['camera_events'],[event])
        self.assertEqual(len(plan['events']),1)
        self.assertEqual(plan['events'][0]['status'],'applied')
        self.assertEqual(plan['events'][0]['indices'],[event])
        selection=json.loads((folder/'selection.json').read_text())
        controls=[selection[str(i)]['controls'] for i in range(self.n)]
        self.assertTrue(any(abs(c[0])>.1 for c in controls[event:]))
        # The original recipe remains unchanged across the shutter event;
        # selected adjustments therefore prove the measured solver ran.
        self.assertTrue(all(tone_xmp.values((self.source/f'F{i:03}.xmp').read_bytes())==[0,-40,0]
                            for i in range(self.n)))
        trial_calls=[c for c in self.renderer.calls if '/initial/raw/' not in c['raw']]
        self.assertTrue(trial_calls)
        self.assertTrue(all(c['edge']==1600 for c in self.renderer.calls))
        self.assertTrue(result['review']['numeric_conditions_met'])
        self.assertEqual(result['review']['strict_failed_frames'],[])
        self.assertLessEqual(result['review']['primary_offset_adjusted_max_error_ev'],.02)
        for i in range(self.n):
            entry=selection[str(i)]
            recipe=Path(entry['xmp']).read_bytes()
            joint_feedback.match(recipe,Path(entry['jpg']).read_bytes(),f'F{i:03}')
            self.assertEqual(automatic.file_hash(entry['jpg']),entry['sha256'])
        self.assert_sources_unchanged()
