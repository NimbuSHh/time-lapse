"""Legacy compatibility is read-only; actual synthetic JPEG/XMP backs approval."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from timelapse import automatic, legacy_preview, tone_xmp, trajectory, trajectory_model, workflow
from timelapse.project import digest
from timelapse.photoshop import Cancelled
from tests import test_refinement as fixtures


class LegacyPreviewTests(unittest.TestCase):
    def setUp(self):
        def larger(i,xmp):
            yy,xx=np.mgrid[0:128,0:192]/np.array([127,191])[:,None,None]
            exposure,highlights,whites=tone_xmp.values(xmp)
            light=.035+.31*(.8*xx+.2*yy)
            linear=light[:,:,None]*np.array([1.,.92,.82])*2**(.006*i+exposure+.002*(highlights+40)+.002*whites)
            rgb=np.where(linear<=.0031308,linear*12.92,1.055*linear**(1/2.4)-.055)
            image=Image.fromarray(np.round(np.clip(rgb,0,1)*255).astype('uint8'))
            out=BytesIO()
            image.save(out,format='JPEG',xmp=xmp,quality=100,subsampling=0,
                icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
            return out.getvalue()
        self.render_patch=patch.object(fixtures,'render',side_effect=larger)
        self.render_patch.start();self.addCleanup(self.render_patch.stop)
        self.fixture=fixtures.RecoveryTests('test_completed_trial_reused_after_new_runner_without_touching_sources_or_old_run')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.r=self.fixture.r
        self.r.__class__=trajectory.Trajectory
        self.r.product=False
        self.r.primary_region='sky'
        self.output=self.fixture.output
        self.folder=self.fixture.old_folder/'轨迹修正-legacy'
        self.folder.mkdir()
        self.r.folder=self.folder
        self.r.state.update(id=self.folder.name,path=str(self.folder),version=trajectory.VERSION)
        self.r.history_roots=[self.folder]
        baseline=[self.r.history(i)[0] for i in range(self.r.n)]
        self.r.plan=trajectory_model.build_plan(self.r.base,[e['regions'] for e in baseline])
        # Force real trial receipts, distinct from the reconstructed baseline.
        self.entries=[self.r.render(i,[0,-40,1,255]) for i in range(self.r.n)]
        self.r.state.update(state='awaiting-user-review',target_sha256=self.r.plan['plan_sha256'])
        automatic.atomic_json(self.folder/'trajectory-target.json',self.r.plan)
        self.r.state['target_file_sha256']=automatic.file_hash(self.folder/'trajectory-target.json')
        self.policy={'curve_white':255,'sky_offset_ev':0.,'target_sha256':self.r.plan['plan_sha256']}
        automatic.atomic_json(self.folder/'effective-tone-policy.json',self.policy)
        self.r.state['effective_policy_sha256']=automatic.file_hash(self.folder/'effective-tone-policy.json')
        automatic.atomic_json(self.folder/'selection.json',{str(i):e for i,e in enumerate(self.entries)})
        manifest={'frame_count':self.r.n,'source_dimensions':list(self.r.context(0)['size']),
                  'frames':[{'index':i+1,'jpg':e['jpg'],'sha256':e['sha256'],'recipe_sha256':e['recipe_sha256']}
                            for i,e in enumerate(self.entries)]}
        automatic.atomic_json(self.folder/'video-manifest.json',manifest)
        video=self.folder/'preview.mp4';video.write_bytes(b'isolated video encoder fixture')
        self.r.state['result']={'video':str(video),'sha256':digest(video.read_bytes()),
            'manifest':str(self.folder/'video-manifest.json'),
            'manifest_sha256':automatic.file_hash(self.folder/'video-manifest.json'),
            'target_sha256':self.r.plan['plan_sha256'],
            'selection_sha256':automatic.file_hash(self.folder/'selection.json'),
            'effective_policy_sha256':self.r.state['effective_policy_sha256'],
            'effective_tone_policy':self.policy,'numeric_conditions_met':True,'state':'awaiting-user-review'}
        self.r.save()
        automatic.atomic_json(self.output/'trajectory-latest.json',{'path':str(self.folder)})
        self.before={p:p.read_bytes() for p in self.fixture.root.rglob('*') if p.is_file()}
        self.renderer=self.fixture.renderer
        self.old_identity=deepcopy(self.renderer.identity)
        self.renderer.identity='new-native-dimension-response'
        self.renderer.compatible_identities=[self.old_identity]
        self.calls=[]
        def full(raw,jpg,edge):
            self.assertEqual(edge,0)
            recipe=raw.with_suffix('.xmp').read_bytes()
            with Image.open(BytesIO(larger(int(raw.stem[1:]),recipe))) as image:
                image.resize((288,192)).save(jpg,format='JPEG',xmp=recipe,quality=100,
                    icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
            self.renderer.last_render={'raw':str(raw.resolve()),'native_size':[288,192],
                                       'output_size':[288,192],'edge':0}
            self.calls.append(raw.stem)
        self.renderer.render.side_effect=full
        self.renderer.render.reset_mock()

    def runner(self):
        return legacy_preview.LegacyRunner(self.output,renderer_factory=self.fixture.factory)

    def confirmation(self,runner):
        r=runner.state['result']
        return {'confirmed':True,'run_id':r['run_id'],'video_sha256':r['sha256'],
                **{k:r[k] for k in ('selection_sha256','target_sha256','effective_policy_sha256')}}

    def unchanged(self):
        for path,data in self.before.items():
            self.assertEqual(path.read_bytes(),data,str(path))

    def test_load_and_constructor_never_migrate_or_write_legacy_files(self):
        displayed=legacy_preview.load(self.output)
        self.assertEqual(displayed['result']['run_id'],self.r.state['id'])
        self.assertTrue(displayed['result']['export_available'])
        runner=self.runner()
        self.assertTrue(runner.legacy_compatible)
        self.assertFalse(runner.product)
        self.assertEqual(runner.state['result']['run_id'],self.r.state['id'])
        self.assertNotIn('run_id',json.loads((self.folder/'state.json').read_text())['result'])
        self.assertFalse((self.folder/legacy_preview.STATE_FILE).exists())
        self.renderer.preflight.assert_not_called()
        self.unchanged()

    def test_stopped_same_source_preparation_keeps_previous_preview_exportable(self):
        prepared={'id':'new-preparation','state':'stopped','raw_dir':str(self.runner().source)}
        # The original source is authoritative; a different sequence cannot
        # accidentally inherit this preview or its approval.
        with patch.object(workflow,'load_preparation',return_value=prepared):
            displayed=workflow.load_latest(self.output)
            self.assertEqual(displayed['result']['run_id'],self.r.state['id'])
            self.assertEqual(displayed['pending_preparation'],prepared['id'])
            confirmation=self.confirmation(self.runner())
            result=workflow.export(self.output,confirmation,renderer_factory=self.fixture.factory)
            self.assertEqual(result['state'],'done')
            self.assertEqual(len(self.calls),5)
            prepared['raw_dir']=str(self.output/'different-source')
            self.assertEqual(workflow.load_latest(self.output),prepared)
            with self.assertRaisesRegex(ValueError,'尚未准备完成'):
                workflow.export(self.output,confirmation,renderer_factory=self.fixture.factory)
        self.unchanged()

    def test_exact_approved_legacy_preview_exports_once_with_current_native_evidence(self):
        runner=self.runner()
        recipe=Path(self.entries[0]['xmp']).read_bytes()
        expected=digest(json.dumps([runner.context(0)['source'],digest(recipe),1600,self.old_identity],sort_keys=True).encode())
        self.assertEqual(runner.cache_key(0,recipe),expected)
        self.assertNotEqual(runner.cache_key(0,recipe,edge=0),expected)
        confirmation=self.confirmation(runner)
        result=runner.export(confirmation)
        self.assertEqual(result['state'],'done')
        self.assertEqual(len(self.calls),5)
        self.assertTrue(all(e['renderer']==self.renderer.identity for e in result['delivery']['frames']))
        self.assertEqual(legacy_preview.load(self.output)['state'],'done')
        again=self.runner().export(confirmation)
        self.assertEqual(again,result)
        self.assertEqual(len(self.calls),5)
        self.unchanged()

    def test_only_selected_receipts_are_read_and_tampering_is_rejected(self):
        ignored=self.folder/'frames/F000/unselected';ignored.mkdir()
        (ignored/'receipt.json').write_text('intentionally invalid rejected trial')
        runner=self.runner()
        selected=runner.history(0)[0]
        self.assertEqual(selected['sha256'],self.entries[0]['sha256'])
        rec=Path(selected['jpg']).parent/'receipt.json'
        record=json.loads(rec.read_text());record['key']='wrong-source';automatic.atomic_json(rec,record)
        runner.histories.clear()
        with self.assertRaisesRegex(ValueError,'校验'):
            runner.history(0)
        self.renderer.render.assert_not_called()

    def test_incomplete_legacy_is_displayed_but_refused_for_export(self):
        state=json.loads((self.folder/'state.json').read_text())
        del state['result']['selection_sha256']
        automatic.atomic_json(self.folder/'state.json',state)
        displayed=legacy_preview.load(self.output)
        self.assertEqual(displayed['result']['video'],state['result']['video'])
        self.assertFalse(displayed['result']['export_available'])
        self.assertTrue(displayed['result']['export_block_reason'])
        with self.assertRaisesRegex(ValueError,'缺少'):
            self.runner()
        self.renderer.render.assert_not_called()

    def test_incompatible_renderer_and_changed_original_stop(self):
        self.renderer.compatible_identities=[]
        with self.assertRaisesRegex(ValueError,'不兼容'):
            self.runner()
        self.renderer.compatible_identities=[self.old_identity]
        runner=self.runner()
        (self.fixture.old_folder/'run.json').write_bytes(b'original edited')
        with self.assertRaisesRegex(ValueError,'改变'):
            runner.save()
        self.assertFalse((self.folder/legacy_preview.STATE_FILE).exists())

    def test_sky_review_uses_frozen_offset_and_strict_mid_errors(self):
        runner=self.runner()
        observations=[deepcopy(e) for e in self.entries]
        # This tests numeric interpretation, independent of the file-based
        # validation above: a frozen .15 EV contrast offset remains .15 EV.
        self.policy['sky_offset_ev']=.15
        automatic.atomic_json(self.folder/'effective-tone-policy.json',self.policy)
        runner.state['effective_policy_sha256']=automatic.file_hash(self.folder/'effective-tone-policy.json')
        for i,obs in enumerate(observations):
            obs['brightness'][0]=runner.plan['target_mid'][i]
            obs['regions']['sky']=runner.plan['regional_targets']['sky'][i]+.15
        self.assertTrue(runner.review_entries(observations)['numeric_conditions_met'])
        observations[2]['regions']['sky']-=.15
        self.assertFalse(runner.review_entries(observations)['numeric_conditions_met'])
        observations[2]['regions']['sky']+=.15
        observations[2]['brightness'][0]+=.013
        self.assertFalse(runner.review_entries(observations)['numeric_conditions_met'])

    def test_cancelled_export_resumes_from_separate_record_without_legacy_edits(self):
        runner=self.runner();confirmation=self.confirmation(runner)
        runner.cancelled=lambda:len(self.calls)>=2
        with self.assertRaises(Cancelled):
            runner.export(confirmation)
        self.assertEqual(legacy_preview.load(self.output)['state'],'stopped')
        final=self.runner().export(confirmation)
        self.assertEqual(final['state'],'done')
        self.assertEqual(self.calls,['F000','F001','F002','F003','F004'])
        self.unchanged()
