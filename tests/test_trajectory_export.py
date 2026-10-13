"""Real JPEG/XMP provenance and resume tests; synthetic RAW, never Adobe."""
from io import BytesIO
import json
from pathlib import Path
import unittest

from PIL import Image, ImageCms

from timelapse import automatic, trajectory, trajectory_export as export, trajectory_model, trajectory_regions
from timelapse.photoshop import Cancelled
from timelapse.project import digest
from tests import test_refinement as fixtures
from tests.test_joint_feedback import render


class ApprovedExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RecoveryTests('test_completed_trial_reused_after_new_runner_without_touching_sources_or_old_run')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.r = self.fixture.r
        self.r.__class__ = trajectory.Trajectory
        self.r.product = True
        self.r.primary_region = 'primary'
        self.r.region_contract = {
            'method': trajectory_regions.METHOD, 'mode': 'manual', 'per_frame_pixel_reselection': False,
            'rois': dict(zip(trajectory_regions.NAMES,
                            ([0,0,.5,.5], [.5,0,1,.5], [0,.5,.5,1], [.5,.5,1,1])))}
        entries = [self.r.history(i)[0] for i in range(self.r.n)]
        self.r.plan = trajectory_model.build_plan(self.r.base, [e['regions'] for e in entries], self.r.region_contract)
        self.assertTrue(trajectory_model.review_sequence(self.r.plan, entries)['numeric_conditions_met'])
        self.r.state.update(version=trajectory.PRODUCT_VERSION,
                            target_sha256=self.r.plan['plan_sha256'], state='awaiting-user-review')
        self.folder = self.r.folder
        automatic.atomic_json(self.folder/'trajectory-target.json', self.r.plan)
        self.r.state['target_file_sha256'] = automatic.file_hash(self.folder/'trajectory-target.json')
        self.entries = entries
        automatic.atomic_json(self.folder/'selection.json', {str(i): e for i,e in enumerate(entries)})
        self.policy = {'curve_white':255,'sky_offset_ev':0}
        automatic.atomic_json(self.folder/'effective-tone-policy.json', self.policy)
        self.r.state['effective_policy_sha256'] = automatic.file_hash(self.folder/'effective-tone-policy.json')
        self.manifest = {'frame_count':self.r.n, 'source_dimensions':list(self.r.context(0)['size']),
                         'frames':[{'index':i+1, 'jpg':str(Path(e['jpg']).resolve()),
                                    'sha256':e['sha256'], 'recipe_sha256':e['recipe_sha256']}
                                   for i,e in enumerate(entries)]}
        automatic.atomic_json(self.folder/'video-manifest.json', self.manifest)
        video = self.folder/'preview.mp4'; video.write_bytes(b'isolated encoder output; no real media')
        self.r.state['result'] = {
            'run_id': self.r.state['id'], 'video':str(video), 'sha256':digest(video.read_bytes()),
            'manifest':str(self.folder/'video-manifest.json'),
            'manifest_sha256':automatic.file_hash(self.folder/'video-manifest.json'),
            'target_sha256':self.r.plan['plan_sha256'],
            'effective_policy_sha256':self.r.state['effective_policy_sha256'],
            'effective_tone_policy':self.policy,
            'selection_sha256':automatic.file_hash(self.folder/'selection.json'),
            'numeric_conditions_met':True, 'state':'awaiting-user-review', 'verified':False}
        result = self.r.state['result']
        self.confirmation = {'confirmed':True, 'run_id':result['run_id'], 'video_sha256':result['sha256'],
                             **{f:result[f] for f in export.CONFIRM_FIELDS[2:]}}
        self.renderer = self.fixture.renderer
        self.renderer.render.side_effect = self.render_native
        self.calls = []

    def render_native(self, raw, destination, edge):
        self.assertEqual(edge, 0)
        self.assertNotEqual(raw.parent, self.r.source)
        self.assertNotEqual(raw.stat().st_ino, (self.r.source/raw.name).stat().st_ino)
        self.assertEqual((raw.parent/(raw.stem+'.acr')).read_bytes(), (self.r.source/(raw.stem+'.acr')).read_bytes())
        recipe = (raw.parent/(raw.stem+'.xmp')).read_bytes()
        index = int(raw.stem[1:])
        self.assertEqual(digest(recipe), self.entries[index]['recipe_sha256'])
        with Image.open(BytesIO(render(index, recipe))) as image:
            full = image.resize((288, 192))
            full.save(destination, format='JPEG', quality=100, xmp=recipe,
                      icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        self.renderer.last_render = {'raw':str(raw.resolve()), 'native_size':[288,192],
                                     'output_size':[288,192], 'edge':0}
        self.calls.append(index)

    def test_requires_explicit_exact_confirmation_before_renderer(self):
        for confirmation in (None, {}, {'approved':True}, {**self.confirmation,'confirmed':1},
                             *({**self.confirmation,k:'wrong'} for k in export.CONFIRM_FIELDS)):
            with self.subTest(confirmation=confirmation), self.assertRaisesRegex(ValueError,'用户确认'):
                export.export(self.r, confirmation)
        self.renderer.preflight.assert_not_called()
        self.assertFalse((self.folder/'export-approval.json').exists())

    def test_native_output_binds_recipe_and_preserves_inputs_and_preview(self):
        preview = self.r.state['result'].copy()
        result = export.export(self.r, self.confirmation)
        self.assertEqual(self.calls, list(range(5)))
        self.assertEqual(result['video'], preview['video'])
        self.assertEqual(result['sha256'], preview['sha256'])
        self.assertEqual(result['delivery']['count'],5)
        self.assertFalse(result['delivery']['visual_playback_verified'])
        self.assertFalse(result['verified'])
        self.assertTrue(result['preview_user_confirmed'])
        for file,data in self.fixture.protected.items():
            self.assertEqual(file.read_bytes(), data)
        again = export.export(self.r, self.confirmation)
        self.assertEqual(again['delivery'],result['delivery'])
        self.assertEqual(self.calls,list(range(5)))

    def test_false_numeric_flag_and_actual_numeric_failure_block(self):
        self.r.state['result']['numeric_conditions_met'] = False
        with self.assertRaisesRegex(ValueError,'数值检查'):
            export.export(self.r, self.confirmation)
        self.r.state['result']['numeric_conditions_met'] = True
        # Change and coherently rehash the target; actual image measurements
        # still fail and cannot be replaced by optimistic selection metadata.
        self.r.plan['target_mid'][2] += 1
        self.r.plan['plan_sha256'] = trajectory_model._hash(self.r.plan)
        automatic.atomic_json(self.folder/'trajectory-target.json', self.r.plan)
        self.r.state['target_file_sha256'] = automatic.file_hash(self.folder/'trajectory-target.json')
        self.r.state['target_sha256'] = self.r.plan['plan_sha256']
        self.r.state['result']['target_sha256'] = self.r.plan['plan_sha256']
        self.confirmation['target_sha256'] = self.r.plan['plan_sha256']
        with self.assertRaisesRegex(ValueError,'数值检查'):
            export.export(self.r, self.confirmation)
        self.renderer.preflight.assert_not_called()

    def test_primary_target_failure_is_remeasured_even_without_camera_events(self):
        self.policy['sky_offset_ev'] = .2
        automatic.atomic_json(self.folder/'effective-tone-policy.json', self.policy)
        sha = automatic.file_hash(self.folder/'effective-tone-policy.json')
        self.r.state['effective_policy_sha256'] = sha
        self.r.state['result']['effective_policy_sha256'] = sha
        self.confirmation['effective_policy_sha256'] = sha
        with self.assertRaisesRegex(ValueError,'数值检查'):
            export.export(self.r,self.confirmation)
        self.renderer.render.assert_not_called()

    def test_selection_claims_do_not_replace_actual_measurements(self):
        selection=json.loads((self.folder/'selection.json').read_text())
        selection['0']['brightness']=[99]*6
        selection['0']['controls']=[99]*4
        automatic.atomic_json(self.folder/'selection.json',selection)
        sha=automatic.file_hash(self.folder/'selection.json')
        self.r.state['result']['selection_sha256']=sha
        self.confirmation['selection_sha256']=sha
        result=export.export(self.r,self.confirmation)
        self.assertEqual(result['state'],'done')
        self.assertEqual(self.calls,list(range(5)))

    def test_changed_sources_stop_before_render(self):
        (self.r.source/'F000.xmp').write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError,'原素材'):
            export.export(self.r, self.confirmation)
        self.renderer.render.assert_not_called()

    def test_video_target_policy_manifest_and_selection_tampering(self):
        paths = ['preview.mp4','trajectory-target.json','effective-tone-policy.json',
                 'video-manifest.json','selection.json']
        for name in paths:
            file=self.folder/name; data=file.read_bytes()
            try:
                file.write_bytes(b'tampered')
                with self.subTest(file=name), self.assertRaises(ValueError):
                    export.export(self.r, self.confirmation)
            finally:
                file.write_bytes(data)
        self.renderer.render.assert_not_called()

    def test_actual_cached_jpeg_and_recipe_tampering_cannot_use_loaded_history(self):
        for key in ('jpg','xmp'):
            file=Path(self.entries[0][key]); data=file.read_bytes()
            try:
                file.write_bytes(b'tampered actual cache')
                with self.subTest(file=key), self.assertRaises(ValueError):
                    export.export(self.r, self.confirmation)
            finally:
                file.write_bytes(data)
        self.renderer.render.assert_not_called()

    def test_reordered_manifest_even_rehashed_is_rejected(self):
        frames=self.manifest['frames']; frames[0],frames[1]=frames[1],frames[0]
        automatic.atomic_json(self.folder/'video-manifest.json',self.manifest)
        self.r.state['result']['manifest_sha256']=automatic.file_hash(self.folder/'video-manifest.json')
        with self.assertRaisesRegex(ValueError,'顺序'):
            export.export(self.r,self.confirmation)
        self.renderer.render.assert_not_called()

    def test_cancellation_retains_completed_frames_and_resumes_exact_approval(self):
        def interrupted(raw, destination, edge):
            self.render_native(raw,destination,edge)
            if raw.stem=='F001':raise Cancelled('test interruption')
        self.renderer.render.side_effect=interrupted
        with self.assertRaises(Cancelled):export.export(self.r,self.confirmation)
        self.assertEqual(self.r.state['state'],'stopped')
        root=Path(self.r.state['export']['path'])
        self.assertTrue((root/'receipts/F000.json').exists())
        self.assertFalse((root/'receipts/F001.json').exists())
        partial=list((root/'attempts').glob('F001-*/render.jpg'))[0]
        before=partial.read_bytes()
        self.renderer.render.side_effect=self.render_native
        export.export(self.r,self.confirmation)
        self.assertEqual(self.calls,[0,1,1,2,3,4])
        self.assertEqual(partial.read_bytes(),before)

    def test_bad_dimensions_recipe_color_and_truncated_output_never_receive_receipt(self):
        for defect in ('dimensions','recipe','color','truncated','evidence'):
            with self.subTest(defect=defect):
                def defective(raw,destination,edge):
                    self.render_native(raw,destination,edge)
                    if defect=='dimensions':self.renderer.last_render['native_size']=[300,200]
                    elif defect=='evidence':self.renderer.last_render=None
                    elif defect=='truncated':destination.write_bytes(destination.read_bytes()[:-200])
                    elif defect=='recipe':
                        recipe=(raw.parent/(raw.stem+'.xmp')).read_bytes().replace(b'Contrast2012="10"',b'Contrast2012="20"')
                        destination.write_bytes(render(0,recipe))
                    else:
                        with Image.open(destination) as image:
                            image.load(); image.save(destination,format='JPEG',xmp=(raw.parent/(raw.stem+'.xmp')).read_bytes())
                self.renderer.render.side_effect=defective
                with self.assertRaises((ValueError,OSError)):
                    export.export(self.r,self.confirmation)
                root=Path(self.r.state['export']['path'])
                self.assertFalse((root/'receipts/F000.json').exists())

    def test_completed_file_tampering_stops_without_overwrite(self):
        result=export.export(self.r,self.confirmation)
        file=Path(result['delivery']['jpgs'])/'F000.jpg';file.write_bytes(b'tamper')
        with self.assertRaisesRegex(ValueError,'校验'):
            export.export(self.r,self.confirmation)
        self.assertEqual(file.read_bytes(),b'tamper')
        self.assertEqual(self.calls,list(range(5)))

    def test_source_change_inside_renderer_stops_before_publishing(self):
        def changed(raw,destination,edge):
            self.render_native(raw,destination,edge)
            (self.r.source/'F000.xmp').write_bytes(b'source edit')
        self.renderer.render.side_effect=changed
        with self.assertRaisesRegex(ValueError,'原素材'):
            export.export(self.r,self.confirmation)
        root=Path(self.r.state['export']['path'])
        self.assertFalse((root/'JPG/F000.jpg').exists())
        self.assertFalse((root/'receipts/F000.json').exists())

    def test_old_preview_version_does_not_infer_native_size(self):
        self.r.state['version']=trajectory.VERSION
        with self.assertRaisesRegex(ValueError,'旧版'):
            export.export(self.r,self.confirmation)
        self.renderer.render.assert_not_called()
