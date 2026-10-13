"""Preview boundary and immutable-cache behavior; no Adobe or real media."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from timelapse import trajectory
from timelapse.project import digest
from timelapse.photoshop import Cancelled


class PreviewBoundaryTests(unittest.TestCase):
    def make_runner(self,folder):
        r=trajectory.Trajectory.__new__(trajectory.Trajectory)
        r.folder=folder;r.n=5;r.chosen={};r.plan={'plan_sha256':'frozen-plan'}
        r.state={'state':'ready','preview_fps':25,'full_export_authorized':False}
        r.cancelled=lambda:False;r.progress=None;r.renderer=Mock();r.check_source=Mock();r.report=Mock()
        r.uniform_tone_policy=Mock(return_value=240)
        r.select_exposure=Mock(side_effect=lambda i,c:{'jpg':str(folder/f'{i}.jpg'),'sha256':str(i),'recipe_sha256':'recipe-'+str(i)})
        r.protected=Mock(return_value=True)
        return r

    def video(self,folder):
        p=folder/'preview.mp4';p.write_bytes(b'isolated-encoder-result')
        return {'video':str(p),'sha256':digest(p.read_bytes()),'fps':25,'frame_count':5,'encoded_verified':True}

    def test_complete_sequence_stops_for_user_even_when_numeric_checks_pass(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);r=self.make_runner(p)
            with patch('timelapse.trajectory_model.review_sequence',return_value={'numeric_conditions_met':True}), patch('timelapse.preview_video.encode_preview',return_value=self.video(p)) as encode, patch.object(r,'export',side_effect=AssertionError('must never export')) as export:
                result=r.execute()
                self.assertEqual(r.select_exposure.call_count,5)
                self.assertEqual(len(encode.call_args.args[0]),5)
                self.assertEqual(result['state'],'awaiting-user-review')
                self.assertFalse(result['verified']);self.assertFalse(result['full_export_authorized'])
                self.assertFalse(result['visual_playback_verified']);export.assert_not_called()
                saved=json.loads((p/'state.json').read_text());self.assertEqual(saved['state'],'awaiting-user-review')
                prior_calls=r.renderer.preflight.call_count
                again=r.execute()
                self.assertEqual(again,result);self.assertEqual(r.renderer.preflight.call_count,prior_calls)
                self.assertEqual(encode.call_count,1)

    def test_numerical_failure_is_visible_and_cannot_trigger_export(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);r=self.make_runner(p)
            with patch('timelapse.trajectory_model.review_sequence',return_value={'numeric_conditions_met':False}),patch('timelapse.preview_video.encode_preview',return_value=self.video(p)):
                result=r.execute()
            self.assertEqual(result['state'],'awaiting-user-review');self.assertFalse(result['numeric_conditions_met']);self.assertFalse(result['verified'])
            with self.assertRaisesRegex(ValueError,'用户确认'):r.export({'accepted':True})

    def test_changed_waiting_video_is_rejected_without_adobe(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);r=self.make_runner(p);r.state.update(state='awaiting-user-review',result=self.video(p))
            Path(r.state['result']['video']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'视频已改变'):r.execute()
            r.renderer.preflight.assert_not_called()

    def test_highlight_failure_preserves_selection_and_stops_before_encoding(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);r=self.make_runner(p);r.protected=Mock(return_value=False)
            with patch('timelapse.preview_video.encode_preview') as encode:
                with self.assertRaisesRegex(ValueError,'高光保护'):r.execute()
                encode.assert_not_called()
            self.assertEqual(r.state['state'],'error')
            self.assertIn('0',json.loads((p/'selection.json').read_text()))

    def test_cancelled_preflight_keeps_stopped_state(self):
        with tempfile.TemporaryDirectory() as t:
            r=self.make_runner(Path(t));r.report=Mock(side_effect=Cancelled('stop'))
            with self.assertRaises(Cancelled):r.execute()
            self.assertEqual(r.state['state'],'stopped');r.renderer.preflight.assert_not_called()


if __name__=='__main__':unittest.main()

class CrossRunCacheTests(unittest.TestCase):
    def test_old_render_is_remeasured_for_new_target_and_tampering_is_rejected(self):
        from tests.test_refinement import RecoveryTests
        from timelapse import refinement
        fixture=RecoveryTests(methodName='test_completed_trial_reused_after_new_runner_without_touching_sources_or_old_run')
        fixture.setUp()
        try:
            first=fixture.r.render(0,[.1,-40,0,255])
            old_folder=fixture.r.folder
            fresh=fixture.output/'new-independent-run';fresh.mkdir()
            fixture.r.folder=fresh;fixture.r.history_roots=[old_folder,fresh];fixture.r.histories.clear()
            again=fixture.r.render(0,[.1,-40,0,255])
            self.assertEqual(again['sha256'],first['sha256']);self.assertTrue(again['reused'])
            self.assertEqual(fixture.renderer.render.call_count,1)
            Path(first['jpg']).write_bytes(b'tampered')
            fixture.r.histories.clear()
            with self.assertRaisesRegex(ValueError,'缓存校验'):fixture.r.history(0)
            self.assertEqual(fixture.renderer.render.call_count,1)
            for path,data in fixture.protected.items():self.assertEqual(path.read_bytes(),data)
        finally:fixture.tearDown()

class UniformToneFeasibilityTests(unittest.TestCase):
    def runner(self,path):
        r=trajectory.Trajectory.__new__(trajectory.Trajectory);r.folder=path;r.state={};r.plan={'camera_events':[],'regional_targets':{'sky':[0.]}}
        r.pilot_indices=lambda:[0];r.report=Mock();r.save=Mock();r.protected=Mock(return_value=True)
        return r

    def test_specific_exposure_failure_can_try_another_uniform_curve(self):
        with tempfile.TemporaryDirectory() as t:
            r=self.runner(Path(t));r.select_exposure=Mock(side_effect=[trajectory.TargetUnreachable('range'),{'regions':{'sky':0.}}])
            self.assertEqual(r.uniform_tone_policy(),248)
            self.assertEqual([c.args[1] for c in r.select_exposure.call_args_list],[255,248])
            report=json.loads((Path(t)/'tone-policy-review.json').read_text())
            self.assertEqual(report['attempts'][0]['unreachable'][0]['frame'],1)

    def test_source_or_recipe_error_is_never_retried_as_a_curve_choice(self):
        with tempfile.TemporaryDirectory() as t:
            r=self.runner(Path(t));r.select_exposure=Mock(side_effect=ValueError('source changed'))
            with self.assertRaisesRegex(ValueError,'source changed'):r.uniform_tone_policy()
            self.assertEqual(r.select_exposure.call_count,1)

class RegionalSolverTests(unittest.TestCase):
    def runner(self):
        import numpy as np
        r=trajectory.Trajectory.__new__(trajectory.Trajectory)
        r.n=1;r.target=np.array([[.7,0,0,0,0,0]])
        r.plan={'regional_targets':{'sky':[.45]}}
        r.chosen={};r.report=Mock();r.protected=Mock(return_value=True)
        r.context=lambda i:{'frame':{'original_controls':[0,-100,0]}}
        history=[]
        r.history=lambda i:history
        def render(i,c):
            for entry in history:
                if entry['controls']==c:return entry
            e,w=c[0],c[2]
            entry={'controls':list(c),'brightness':[1.3*e+.002*w+.04*e*e,0,0,0,0,0],
                   'regions':{'sky':1.1*e+.009*w+.015*e*e}}
            history.append(entry);return entry
        r.render=Mock(side_effect=render)
        render(0,[0,-100,0,240])
        return r

    def test_two_regions_with_different_responses_converge_on_actual_observations(self):
        r=self.runner();entry=r.select_regional(0,240)
        self.assertLessEqual(r.regional_score(0,entry),1)
        self.assertEqual(entry['controls'][1],-100)
        self.assertEqual(entry['controls'][3],240)
        before=r.render.call_count
        self.assertEqual(r.select_regional(0,240),entry)
        self.assertEqual(r.render.call_count,before)

    def test_impossible_regional_target_stops_with_bounded_trials(self):
        r=self.runner();r.plan['regional_targets']['sky']=[5.]
        with self.assertRaises(trajectory.TargetUnreachable):r.select_regional(0,240)
        for call in r.render.call_args_list:
            self.assertLessEqual(abs(call.args[1][0]),2)
            self.assertLessEqual(abs(call.args[1][2]),100)
        self.assertLessEqual(r.render.call_count,50)

    def test_measured_fit_without_highlight_protection_cannot_pass(self):
        r=self.runner();r.protected=Mock(return_value=False)
        with self.assertRaises(trajectory.TargetUnreachable):r.select_regional(0,240)

class EffectivePolicyTests(unittest.TestCase):
    def test_highlights_axis_and_constant_offset_are_measured_with_whites_fixed(self):
        import numpy as np
        r=trajectory.Trajectory.__new__(trajectory.Trajectory)
        r.n=1;r.target=np.array([[.7,0,0,0,0,0]])
        r.plan={'regional_targets':{'sky':[.45]}};r.chosen={}
        r.report=Mock();r.protected=Mock(return_value=True)
        r.context=lambda i:{'frame':{'original_controls':[0,-100,0]}}
        history=[];r.history=lambda i:history
        def render(i,c):
            for old in history:
                if old['controls']==c:return old
            e,h=c[0],c[1]+100
            item={'controls':list(c),'brightness':[1.2*e+.002*h,0,0,0,0,0],
                  'regions':{'sky':e+.006*h}}
            history.append(item);return item
        r.render=Mock(side_effect=render)
        entry=r.select_regional(0,240,axis=1,sky_offset=.15)
        self.assertLessEqual(abs(entry['brightness'][0]-.7),.012)
        self.assertLessEqual(abs(entry['regions']['sky']-.6),.02)
        self.assertEqual(entry['controls'][2],0)
        self.assertTrue(all(c.args[1][2]==0 for c in r.render.call_args_list))

    def test_effective_target_policy_cannot_change_after_freezing(self):
        with tempfile.TemporaryDirectory() as t:
            r=trajectory.Trajectory.__new__(trajectory.Trajectory)
            r.folder=Path(t);r.plan={'plan_sha256':'target'}
            r.state={'tone_policy':'frozen-mid-sky-measured-two-controls','regional_axis':1,'sky_offset_ev':.15}
            first=r.freeze_tone_policy(240);old_hash=r.state['effective_policy_sha256']
            self.assertEqual(r.freeze_tone_policy(240),first)
            self.assertEqual(r.state['effective_policy_sha256'],old_hash)
            r.state['sky_offset_ev']=.2
            with self.assertRaisesRegex(ValueError,'有效显影目标发生改变'):r.freeze_tone_policy(240)
