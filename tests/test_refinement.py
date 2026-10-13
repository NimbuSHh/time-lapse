"""Offline safety and sample-gate checks; never invokes Adobe or real media."""
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from timelapse import automatic, joint_feedback as joint, refinement, tone_xmp
from timelapse.photoshop import Cancelled
from timelapse.project import digest
from timelapse.provenance import xmp_evidence
from tests.test_joint_feedback import sidecar, render


def linear_recipe(i=0):
    curve = b'''<c:ToneCurveName2012>Linear</c:ToneCurveName2012>
      <c:ToneCurvePV2012><r:Seq><r:li>0, 0</r:li><r:li>255, 255</r:li></r:Seq></c:ToneCurvePV2012>
      <c:ToneCurvePV2012Red><r:Seq><r:li>0, 0</r:li><r:li>128, 120</r:li><r:li>255, 255</r:li></r:Seq></c:ToneCurvePV2012Red>'''
    return sidecar(i).replace(b'<c:Look>', curve+b'<c:Look>', 1)


class RecipeTests(unittest.TestCase):
    def test_white_endpoint_preserves_look_channel_curve_identity_and_other_settings(self):
        before = linear_recipe()
        after = refinement.recipe(before, [.25, -70, -20, 240])
        self.assertEqual(tone_xmp.values(after), [.25, -70, -20])
        a, b = xmp_evidence(before), xmp_evidence(after)
        allowed = {'Exposure2012', 'Highlights2012', 'Whites2012', 'ToneCurvePV2012', 'ToneCurveName2012', 'CurveRefineSaturation'}
        self.assertEqual({k:v for k,v in a['crs'].items() if k not in allowed},
                         {k:v for k,v in b['crs'].items() if k not in allowed})
        self.assertEqual(a['identity'], b['identity'])
        self.assertEqual(b['crs']['ToneCurveName2012'], 'Custom')
        self.assertEqual(b['crs']['CurveRefineSaturation'], '1E+2')
        self.assertIn('255, 240', str(b['crs']['ToneCurvePV2012']))
        self.assertEqual(a['crs']['Look'], b['crs']['Look'])
        self.assertEqual(a['crs']['ToneCurvePV2012Red'], b['crs']['ToneCurvePV2012Red'])

    def test_creative_master_curve_is_never_replaced(self):
        base = linear_recipe().replace(b'<r:li>255, 255</r:li>', b'<r:li>128, 145</r:li><r:li>255, 255</r:li>', 1)
        with self.assertRaises(ValueError):
            refinement.recipe(base, [0, -40, 0, 240])
        unchanged = refinement.recipe(base, [.2, -40, 0, 255])
        self.assertEqual(xmp_evidence(base)['crs']['ToneCurvePV2012'],
                         xmp_evidence(unchanged)['crs']['ToneCurvePV2012'])

    def test_curve_endpoint_requires_safe_integer_range(self):
        for white in (219, 256, 240.5):
            with self.assertRaises(ValueError):
                refinement.recipe(linear_recipe(), [0, -40, 0, white])


class AcceptanceTests(unittest.TestCase):
    def runner(self, entries):
        runner = refinement.Refinement.__new__(refinement.Refinement)
        runner.target = np.zeros((1,6)); runner.reference_clips = {0:[0,0,0]}
        runner.history = lambda i: entries
        return runner

    def test_accepted_render_wins_over_lower_search_score_with_tail_violation(self):
        failed = {'brightness':[.02593,-.00469,-.04715,-.12845,-.02834,.02910],
                  'channel_clip_fraction':[0,0,0]}
        passed = {'brightness':[-.04090,-.03731,-.04815,-.11027,-.01378,.01313],
                  'channel_clip_fraction':[0,0,0]}
        r = self.runner([failed,passed])
        self.assertLess(r.score(0,failed),r.score(0,passed))
        self.assertIs(r.best(0),passed)
        self.assertIs(r.refinement_anchor(0),passed)

    def test_highlight_plateau_uses_measured_unclipped_branch_without_accepting_it(self):
        clipped = {'brightness':[.02113,.02329,.00414,.04450,-.13675,.01788],
                   'channel_clip_fraction':[.00145,0,0]}
        unclipped = {'brightness':[.002,-.082,-.101,-.010,-.206,-.074],
                     'channel_clip_fraction':[0,0,0]}
        r = self.runner([clipped,unclipped])
        self.assertIs(r.best(0),clipped)
        self.assertIs(r.refinement_anchor(0),unclipped)
        self.assertFalse(refinement.quality(unclipped,r.target[0],r.reference_clips[0])['accepted'])

    def test_original_clipping_budget_and_tail_threshold_are_independent(self):
        obs = {'brightness': [0, 0, 0, -.1, -.1, -.1], 'channel_clip_fraction': [.0116, 0, 0]}
        self.assertFalse(refinement.quality(obs, np.zeros(6), [.01, 0, 0])['accepted'])
        self.assertTrue(refinement.quality(obs, np.zeros(6), [.0108, 0, 0])['accepted'])
        obs['channel_clip_fraction'] = [.01, 0, 0]
        obs['brightness'][3] = .020001
        self.assertFalse(refinement.quality(obs, np.zeros(6), [.01, 0, 0])['accepted'])

    def test_sequence_uses_actual_adjacent_residuals_and_original_thresholds(self):
        target = np.array([[0.]*6, [2.]*6, [4.]*6])
        entries = {i:{'brightness': list(target[i]), 'channel_clip_fraction':[0,0,0]} for i in range(3)}
        entries[0]['brightness'][0] += .04
        entries[1]['brightness'][0] -= .04
        review = refinement.sequence_check(entries, target, [[0,0,0]]*3, [0,1,2])
        self.assertEqual(review['failed_frames'], [])
        self.assertFalse(review['accepted'])
        self.assertEqual(review['failed_adjacent_pairs'][0][:2], [1,2])
        self.assertTrue(refinement.sequence_check(entries,target,[[0,0,0]]*3,[0,2])['accepted'])
        self.assertEqual(review['thresholds'], {'max_tone_error_ev':.08, 'max_adjacent_tone_residual_ev':.06,
                                              'clip_fraction_increase':.001, 'max_channel_tail_error_ev':.02})


class GateTests(unittest.TestCase):
    def make_runner(self, folder):
        r = refinement.Refinement.__new__(refinement.Refinement)
        r.folder = folder; r.n = 810; r.chosen = {}; r.state = {'new_renders': 0}
        r.cancelled = lambda: False
        r.report = Mock(); r.save = Mock(); r.save_selection = Mock(); r.check_source = Mock()
        r.renderer = Mock(); r.export = Mock(return_value={'verified': True})
        r.optimize = Mock(side_effect=lambda i: {'frame': i})
        return r

    def test_each_failed_gate_blocks_downstream_work_and_export(self):
        centers = {i-1 for i in refinement.CENTERS}
        sample = {i for a,b in refinement.SAMPLE_RANGES for i in range(a-1,b)}
        cases = [('pilot', [{'accepted':False}], centers),
                 ('sample', [{'accepted':True},{'accepted':False}], sample),
                 ('sequence', [{'accepted':True},{'accepted':True},{'accepted':False}], set(range(810)))]
        for name, reviews, expected in cases:
            with self.subTest(gate=name), tempfile.TemporaryDirectory() as tmp:
                r = self.make_runner(Path(tmp)); r.check = Mock(side_effect=reviews)
                result = r.execute()
                self.assertFalse(result['verified']); self.assertEqual(r.state['state'], 'review-needed')
                self.assertEqual({c.args[0] for c in r.optimize.call_args_list}, expected)
                r.export.assert_not_called()

    def test_export_requires_all_three_measured_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.make_runner(Path(tmp)); r.check = Mock(return_value={'accepted':True})
            result = r.execute()
            self.assertTrue(result['verified']); self.assertEqual(r.check.call_count,3)
            r.export.assert_called_once_with({'accepted':True})

    def test_cancel_before_preflight_never_opens_adobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.make_runner(Path(tmp))
            r.report = refinement.Refinement.report.__get__(r)
            r.cancelled = lambda: True
            with self.assertRaises(Cancelled): r.execute()
            r.renderer.preflight.assert_not_called(); r.optimize.assert_not_called(); r.export.assert_not_called()
            self.assertEqual(r.state['state'], 'stopped')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'source'; self.source.mkdir()
        self.output = self.source/'Time-lapse工作'; self.output.mkdir()
        self.old_folder = self.output/'自动调色-fixture'; self.old_folder.mkdir()
        initial = self.old_folder/'initial'
        for sub in ('raw','jpg','receipts'): (initial/sub).mkdir(parents=True)
        for i in range(5):
            xmp = linear_recipe(i)
            (self.source/f'F{i:03}.ARW').write_bytes(b'SYNTHETIC-TEST-BYTES-'+str(i).encode())
            (self.source/f'F{i:03}.xmp').write_bytes(xmp)
            (self.source/f'F{i:03}.acr').write_bytes(b'aux-'+str(i).encode())
            (initial/'raw'/f'F{i:03}.xmp').write_bytes(xmp)
            (initial/'jpg'/f'F{i:03}.jpg').write_bytes(render(i,xmp))
        prepared = joint.prepare(initial/'jpg',self.old_folder/'packages',
                                 mid_roi=[0,0,.3,1],high_roi=[.34,0,1,1],window=5)
        package = Path(prepared['path'])
        self.old = {'id':'fixture','path':str(self.old_folder),'raw_dir':str(self.source),'state':'done',
                    'renderer':{'mock':'no-adobe'},'snapshot':automatic.inputs(self.source)[1],
                    'phases':{'第 1 轮准备校准':{'result':prepared,'sha256':digest((package/'manifest.json').read_bytes())}}}
        (self.old_folder/'run.json').write_text(json.dumps(self.old))
        self.renderer = Mock(identity=self.old['renderer'])
        def fake_render(raw, jpg, edge):
            self.assertNotEqual(raw.parent, self.source)
            self.assertNotEqual(raw.stat().st_ino, (self.source/raw.name).stat().st_ino)
            self.assertEqual((raw.parent/(raw.stem+'.acr')).read_bytes(),(self.source/(raw.stem+'.acr')).read_bytes())
            xmp = (raw.parent/(raw.stem+'.xmp')).read_bytes()
            jpg.write_bytes(render(int(raw.stem[1:]),xmp))
        self.renderer.render.side_effect = fake_render
        self.loader = patch.object(automatic,'load_latest',return_value=self.old); self.loader.start()
        self.factory = Mock(return_value=self.renderer)
        self.r = refinement.Refinement(self.output,renderer_factory=self.factory)
        for i in range(5):
            xmp = (initial/'raw'/f'F{i:03}.xmp').read_bytes()
            jpg = (initial/'jpg'/f'F{i:03}.jpg').read_bytes()
            automatic.atomic_json(initial/'receipts'/f'F{i:03}.json',{'key':self.r.cache_key(i,xmp),'sha256':digest(jpg)})
        self.protected = {p:p.read_bytes() for p in self.source.iterdir() if p.is_file()}
        self.protected.update({p:p.read_bytes() for p in self.old_folder.rglob('*')
                               if p.is_file() and self.r.folder not in p.parents})

    def tearDown(self):
        self.loader.stop(); self.tmp.cleanup()

    def test_completed_trial_reused_after_new_runner_without_touching_sources_or_old_run(self):
        first = self.r.render(0,[.1,-40,0,255])
        self.assertEqual(self.renderer.render.call_count,1)
        restored = refinement.Refinement(self.output,renderer_factory=self.factory)
        again = restored.render(0,[.1,-40,0,255])
        self.assertEqual(first['sha256'],again['sha256'])
        self.assertEqual(self.renderer.render.call_count,1)
        for path,data in self.protected.items(): self.assertEqual(path.read_bytes(),data,path)

    def test_accepted_saved_selection_is_remeasured_and_reused_without_new_trials(self):
        entry = self.r.history(0)[0]
        self.r.target[0] = entry['brightness']
        self.r.saved_selection = {'0':{'sha256':entry['sha256'],'recipe_sha256':entry['recipe_sha256']}}
        selected = self.r.optimize(0)
        self.assertEqual(selected['sha256'],entry['sha256'])
        self.renderer.render.assert_not_called()

    def test_old_receipt_with_wrong_source_is_rejected_before_render(self):
        rec = self.old_folder/'initial/receipts/F000.json'
        record = json.loads(rec.read_text()); record['key'] = 'wrong-source'
        rec.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'来源|配方'):
            self.r.render(0,[.1,-40,0,255])
        self.renderer.render.assert_not_called()

    def test_tampered_new_receipt_is_rejected_after_resume(self):
        first = self.r.render(0,[.1,-40,0,255])
        rec = Path(first['jpg']).parent/'receipt.json'
        value = json.loads(rec.read_text()); value['recipe_sha256'] = 'wrong-recipe'
        rec.write_text(json.dumps(value))
        restored = refinement.Refinement(self.output,renderer_factory=self.factory)
        with self.assertRaisesRegex(ValueError,'缓存校验'):
            restored.history(0)
        self.assertEqual(self.renderer.render.call_count,1)

    def test_first_reference_clip_does_not_inherit_previous_round_measurements(self):
        self.r.base['observations'][0]['channel_clip_fraction'] = [.5,.5,.5]
        self.r.context(0)
        self.assertEqual(self.r.reference_clips[0],[0.,0.,0.])

    def test_reference_hash_mismatch_and_changed_source_stop_before_render(self):
        frame = self.r.base['frames'][0]
        Path(frame['reference_preview']).write_bytes(b'changed')
        self.r.frames.clear()
        with self.assertRaisesRegex(ValueError,'参考图'):
            self.r.render(0,[.1,-40,0,255])
        self.renderer.render.assert_not_called()
        (self.source/'F000.xmp').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'原素材'):
            refinement.Refinement(self.output,renderer_factory=self.factory)

    def test_cumulative_exposure_limit_applies_before_adobe(self):
        with self.assertRaisesRegex(ValueError,'累计'):
            self.r.render(0,[2.01,-40,0,255])
        self.renderer.render.assert_not_called()

    def test_full_export_checks_native_dimensions_and_reuses_verified_output(self):
        self.r.n = 1
        entry = self.r.render(0,[.1,-40,0,255])
        self.r.chosen = {0:entry}
        old = self.old_folder/'final'
        (old/'jpg').mkdir(parents=True); (old/'receipts').mkdir()
        data = (self.old_folder/'initial/jpg/F000.jpg').read_bytes()
        (old/'jpg/F000.jpg').write_bytes(data)
        automatic.atomic_json(old/'receipts/F000.json',{'key':'original-recipe','sha256':digest(data)})
        result = self.r.export({'accepted':True})
        self.assertTrue(result['verified'])
        calls = self.renderer.render.call_count
        self.r.export({'accepted':True})
        self.assertEqual(self.renderer.render.call_count,calls)
        for path,data in self.protected.items(): self.assertEqual(path.read_bytes(),data,path)

    def test_wrong_full_dimensions_do_not_write_a_success_receipt(self):
        from io import BytesIO
        from PIL import Image
        self.r.n = 1
        entry = self.r.render(0,[.1,-40,0,255]); self.r.chosen = {0:entry}
        old = self.old_folder/'final'
        (old/'jpg').mkdir(parents=True); (old/'receipts').mkdir()
        buf = BytesIO(); Image.new('RGB',(500,300)).save(buf,format='JPEG')
        data = buf.getvalue(); (old/'jpg/F000.jpg').write_bytes(data)
        automatic.atomic_json(old/'receipts/F000.json',{'key':'original-recipe','sha256':digest(data)})
        with self.assertRaisesRegex(ValueError,'尺寸'):
            self.r.export({'accepted':True})
        self.assertFalse((self.r.folder/'结果/JPG/F000.json').exists())

    def test_interrupted_trial_is_not_reused_without_receipt_and_can_resume(self):
        normal_render = self.renderer.render.side_effect
        def interrupted(raw, jpg, edge):
            normal_render(raw,jpg,edge)
            raise Cancelled('test interruption after file write')
        self.renderer.render.side_effect = interrupted
        with self.assertRaises(Cancelled):
            self.r.render(0,[.1,-40,0,255])
        self.assertEqual(list((self.r.folder/'frames').glob('*/*/receipt.json')), [])
        self.renderer.render.side_effect = normal_render
        restored = refinement.Refinement(self.output,renderer_factory=self.factory)
        result = restored.render(0,[.1,-40,0,255])
        self.assertEqual(self.renderer.render.call_count,2)
        self.assertEqual(len(list(Path(result['jpg']).parent.glob('incomplete-*.jpg'))),1)
        self.assertTrue((Path(result['jpg']).parent/'receipt.json').is_file())
        for path,data in self.protected.items(): self.assertEqual(path.read_bytes(),data,path)

    def test_existing_automatic_lock_blocks_constructor_and_renderer(self):
        with (self.output/'.automatic.lock').open('a+') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(refinement,'Refinement') as constructor:
                with self.assertRaisesRegex(ValueError,'另一个显影任务'):
                    refinement.run(self.output,renderer_factory=self.factory)
                constructor.assert_not_called()
        self.renderer.render.assert_not_called()
