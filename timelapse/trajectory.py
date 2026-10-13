"""Whole-sequence exposure trajectory with a mandatory video review boundary.

Source media, manual workflows and previous targets remain immutable. This
module stops for explicit approval of the exact preview before full export.
"""
import argparse
import json
from pathlib import Path
import uuid

import numpy as np

from . import automatic, joint_feedback as joint, joint_model, refinement, trajectory_model
from .photoshop import PhotoshopRenderer, Cancelled
from .project import digest, safe_read
from .workspace import output_path

VERSION = 'whole-sequence-preview-v1'
PRODUCT_VERSION = 'whole-sequence-product-v2'
REGIONS = {'sky': (.1,.1,.9,.3), 'sea': (.05,.6,.4,.85),
           'buildings': (.7,.55,.9,.8), 'sunrise': (.3,.4,.65,.46)}
WAITING = 'awaiting-user-review'


class TargetUnreachable(ValueError):
    """A measured exposure family exhausted its legal range or search budget."""


def regions(linear):
    y = linear @ np.array([.2126,.7152,.0722])
    h,w = y.shape
    return {name: float(np.log2(max(float(np.mean(y[joint_model.roi_box(roi,w,h)])),1e-10)))
            for name,roi in REGIONS.items()}


def load_latest(output):
    output = output_path(output)
    index = output/'trajectory-latest.json'
    if not index.exists(): return None
    folder = Path(json.loads(safe_read(index,1024*1024))['path']).resolve()
    if output not in folder.parents or not folder.name.startswith('轨迹修正-'):
        raise ValueError('整段预览任务路径无效')
    state = json.loads(safe_read(folder/'state.json',8*1024*1024))
    if state['path'] != str(folder): raise ValueError('整段预览任务记录不一致')
    return state


class Trajectory(refinement.Refinement):
    def __init__(self, output, progress=None, cancelled=None, renderer_factory=PhotoshopRenderer, original=None):
        self.output = output_path(output)
        self.progress = progress
        self.cancelled = cancelled or (lambda: False)
        self.original = original or automatic.load_latest(self.output)
        self.product = self.original is not None and self.original.get('workflow') == PRODUCT_VERSION
        version = PRODUCT_VERSION if self.product else VERSION
        if self.product:
            from .trajectory_regions import validate
            validate(self.original.get('region_contract'))
        if not self.original or self.original['state'] not in (('prepared',) if self.product else ('done',)):
            raise ValueError('需要完整的原自动任务，才能复用首次参考与曝光响应')
        self.old_folder = Path(self.original['path']).resolve()
        self.source = Path(self.original['raw_dir'])
        self.check_source()
        phase = self.original['phases']['第 1 轮准备校准']
        self.base_folder = Path(phase['result']['path']).resolve()
        if self.old_folder not in self.base_folder.parents: raise ValueError('首次参考路径越界')
        data = safe_read(self.base_folder/'manifest.json',64*1024*1024)
        if digest(data) != phase['sha256']: raise ValueError('首次参考清单改变')
        _, self.base = joint.manifest(self.base_folder,{},'joint-feedback-calibration')
        self.n = len(self.base['frames'])
        if self.n != len(self.original['snapshot']): raise ValueError('原素材与首次参考数量不符')
        existing = load_latest(self.output)
        if existing and existing['source_run'] == self.original['id']:
            self.state = existing
            self.folder = Path(existing['path'])
            # Changing code or clicking start cannot revoke the review boundary.
            if existing['version'] != version: raise ValueError('已有预览版本不同，请保留该版本独立处理')
            data = safe_read(self.folder/'trajectory-target.json',16*1024*1024)
            if digest(data) != self.state['target_file_sha256']: raise ValueError('冻结轨迹已改变')
            self.plan = json.loads(data)
        else:
            contract = self.original.get('region_contract') if self.product else None
            baseline_regions = []
            for i,f in enumerate(self.base['frames']):
                if self.cancelled(): raise Cancelled('已停止目标准备')
                image = safe_read(Path(f['reference_preview']),25*1024*1024)
                if digest(image) != f['reference_sha256']: raise ValueError('首次参考图改变')
                linear,_ = joint_model.pixels(image)
                if contract:
                    from .trajectory_regions import measure
                    baseline_regions.append(measure(linear,contract))
                else:
                    baseline_regions.append(regions(linear))
                if progress: progress(i+1,self.n,'核对首次参考与固定区域')
            self.plan = trajectory_model.build_plan(self.base,baseline_regions,region_contract=contract) if contract else trajectory_model.build_plan(self.base,baseline_regions)
            self.folder = self.old_folder/('轨迹修正-'+uuid.uuid4().hex[:12])
            self.folder.mkdir()
            automatic.atomic_json(self.folder/'trajectory-target.json',self.plan)
            self.state = {'schema':1,'id':self.folder.name,'path':str(self.folder),
                'source_run':self.original['id'],'version':version,'raw_dir':str(self.source),
                'target_file_sha256':automatic.file_hash(self.folder/'trajectory-target.json'),
                'target_sha256':self.plan['plan_sha256'],'measurement_sha256':self.base['measurement_sha256'],
                'state':'ready','phase':'整段目标已冻结','count':self.n,'done':0,'total':self.n,
                'new_renders':0,'reused_renders':0,'error':None,'preview_fps':25,
                'full_export_authorized':False,'visual_playback_verified':False}
            automatic.atomic_json(self.output/'trajectory-latest.json',{'path':str(self.folder)})
        self.region_contract = self.plan.get('regional_measurement_contract') if self.product else None
        self.primary_region = 'primary' if self.product else 'sky'
        self.renderer = renderer_factory(self.folder,self.cancelled)
        if self.renderer.identity != self.original['renderer']:
            raise ValueError('Adobe版本或显影脚本改变，不能混用旧实测')
        self.target = np.zeros((self.n,6));self.target[:,0] = self.plan['target_mid']
        self.frames = {}; self.reference_clips = {}; self.histories = {}; self.chosen = {}
        self.retry_indices = set()
        self.history_roots = []
        for folder in sorted(self.old_folder.glob('分层修正-*')):
            old = json.loads(safe_read(folder/'state.json',8*1024*1024))
            if old['source_run'] == self.original['id'] and old['measurement_sha256'] == self.base['measurement_sha256']:
                if self.old_folder not in folder.resolve().parents: raise ValueError('缓存目录越界')
                self.history_roots.append(folder)
        self.history_roots.append(self.folder)
        selection = self.folder/'selection.json'
        self.saved_selection = json.loads(safe_read(selection,16*1024*1024)) if selection.exists() else {}
        self.save()

    def history(self,i):
        if not getattr(self,'product',False): return super().history(i)
        if i in self.histories: return self.histories[i]
        ctx=self.context(i)
        frame=ctx['frame']
        reference=safe_read(Path(frame['reference_preview']),25*1024*1024)
        base=ctx['base']
        entry={**self.measure(i,reference),'controls':frame['original_controls']+[255.],
               'jpg':frame['reference_preview'],'xmp':str(self.base_folder/'baseline'/frame['xmp']),
               'sha256':digest(reference),'recipe_sha256':digest(base),'reused':True}
        # Existing trial receipts are verified by the inherited implementation.
        # Give it a baseline in memory, then explicitly load only this run's trials.
        entries=[entry]
        stem=Path(ctx['source']['raw']['name']).stem
        for receipt in sorted((self.folder/'frames'/stem).glob('*/receipt.json')):
            item=json.loads(safe_read(receipt,1024*1024))
            xmp_path,jpg_path=receipt.parent/'recipe.xmp',receipt.parent/'preview.jpg'
            xmp=safe_read(xmp_path,4*1024*1024);jpg=safe_read(jpg_path,25*1024*1024)
            if (item['key']!=self.cache_key(i,xmp) or digest(jpg)!=item['sha256']
                    or digest(xmp)!=item['recipe_sha256'] or digest(refinement.recipe(base,item['controls']))!=digest(xmp)):
                raise ValueError('新显影缓存校验失败')
            if Path(item['xmp']).resolve()!=xmp_path.resolve() or Path(item['jpg']).resolve()!=jpg_path.resolve():
                raise ValueError('新显影缓存路径不符')
            joint.match(xmp,jpg,stem)
            entries.append({**item,**self.measure(i,jpg),'reused':True})
        self.histories[i]=entries
        self.state['reused_renders']+=len(entries)
        return entries

    def measure(self,i,data):
        ctx = self.context(i)
        linear,size = joint_model.pixels(data)
        if size != ctx['size']: raise ValueError('实际预览尺寸改变')
        obs = joint_model.measure(linear,self.base['mid_roi'],self.base['high_roi'],ctx['reference'],self.base.get('measurement_selection'))
        if getattr(self,'product',False):
            from .trajectory_regions import measure
            regional = measure(linear,self.region_contract)
        else:
            regional = regions(linear)
        return {**obs,'regions':regional}

    def score(self,i,entry):
        return abs(entry['brightness'][0]-self.target[i,0])

    def protected(self,i,entry):
        # A global display-white ceiling, not the old per-frame six-band target.
        return (max(np.asarray(entry['channel_clip_fraction'])-self.plan['reference_clips'][i]) <= .001
                and max(entry['brightness'][3:]) <= joint_model.CEILING+.02)

    def select_exposure(self,i,ceiling):
        self.report(f'整段曝光实测 · 第{i+1}帧',i,self.n)
        origin = self.context(i)['frame']['original_controls']+[float(ceiling)]
        family = [e for e in self.history(i) if refinement.control_key(e['controls'])[1:] == refinement.control_key(origin)[1:]]
        if family:
            nearest = min(family,key=lambda e:self.score(i,e))
            origin = nearest['controls']
        result = self.exposure(i,origin,budget=10)
        if self.score(i,result) > .04:
            raise TargetUnreachable(f'第{i+1}帧的整段目标在当前曝光范围内尚未实现：实测误差{self.score(i,result):.4f}EV')
        return result

    def pilot_indices(self):
        delta = np.asarray(self.plan['target_mid'])-np.asarray([o['brightness'][0] for o in self.base['observations']])
        result = {0,self.n-1,int(np.argmin(delta)),int(np.argmax(delta)),int(np.argmin(self.target[:,0])),int(np.argmax(self.target[:,0]))}
        for i in self.plan['camera_events']+self.plan['gaps']:
            result.update(j for j in (i-1,i,i+1) if 0<=j<self.n)
        return sorted(result)

    def regional_error(self,i,entry):
        return np.array([entry['brightness'][0]-self.target[i,0],
                         entry['regions'][getattr(self,'primary_region','sky')]-self.plan['regional_targets'][getattr(self,'primary_region','sky')][i]-getattr(self,'_regional_sky_offset',0.)])

    def regional_score(self,i,entry):
        return float(max(abs(self.regional_error(i,entry))/[.012,.02]))

    def select_regional(self,i,ceiling,axis=2,sky_offset=0.):
        """Two measured outputs, two bounded controls; never smooth sliders.

        A response from a previous frame only proposes a trial. Every accepted
        frame must independently satisfy the frozen image-space targets.
        """
        self._regional_sky_offset=float(sky_offset)
        fixed_axis=3-axis
        axes=[0,axis]
        self.report(f'主体与亮部联合实测 · 第{i+1}帧',i,self.n)
        origin = self.context(i)['frame']['original_controls']+[float(ceiling)]
        lower = np.array([max(-5,origin[0]-2),-100.])
        upper = np.array([min(5,origin[0]+2),100.])
        def family():
            return [e for e in self.history(i) if e['controls'][fixed_axis]==origin[fixed_axis] and e['controls'][3]==ceiling]
        def best():
            return min(family(),key=lambda e:(not self.protected(i,e),self.regional_score(i,e)))
        def trial(anchor, ew):
            ew=np.clip(ew,lower,upper)
            controls=origin.copy();controls[0]=round(float(ew[0]),2);controls[axis]=round(float(ew[1]))
            return self.render(i,controls)
        if not family():
            # Transfer only a nearby starting point. Camera changes may require
            # a discontinuous correction, which the measured solver permits.
            neighbors = [j for j,e in self.chosen.items() if e['controls'][3]==ceiling and e['controls'][fixed_axis]==origin[fixed_axis]]
            if neighbors:
                prior=self.chosen[min(neighbors,key=lambda j:abs(i-j))]
                trial(prior,[prior['controls'][0],prior['controls'][axis]])
            else:
                self.render(i,origin)
        responses=getattr(self,'regional_jacobians',{});self.regional_jacobians=responses
        response_key=(axis,ceiling);jac=responses.get(response_key)
        for iteration in range(10):
            anchor=best()
            if self.protected(i,anchor) and self.regional_score(i,anchor)<=1:
                return anchor
            pos=np.array(anchor['controls'],dtype=float)[axes]
            error=self.regional_error(i,anchor)
            # Refresh a local response if a transferred response failed or if
            # no response exists. Probes are retained as reusable real renders.
            if jac is None or iteration in (2,5,8):
                columns=[]
                for coordinate,step in ((0,.12),(1,12.)):
                    change=step if pos[coordinate]+step<=upper[coordinate] else -step
                    probe=pos.copy();probe[coordinate]+=change
                    actual=trial(anchor,probe)
                    delta=actual['controls'][axes[coordinate]]-pos[coordinate]
                    if abs(delta)<1e-8: raise TargetUnreachable('区域响应探针达到边界')
                    columns.append((self.regional_error(i,actual)-error)/delta)
                jac=np.column_stack(columns)
                if not np.all(np.isfinite(jac)) or np.linalg.matrix_rank(jac)<2:
                    raise TargetUnreachable(f'第{i+1}帧的所选控制量的响应不足以区分主体与亮部')
                responses[response_key]=jac
                anchor=best();pos=np.array(anchor['controls'],dtype=float)[axes];error=self.regional_error(i,anchor)
            delta=np.linalg.lstsq(jac,-error,rcond=None)[0]
            delta/=max(1.,abs(delta[0])/.8,abs(delta[1])/40.)
            proposed=np.clip(pos+delta,lower,upper)
            controls=origin.copy();controls[0]=proposed[0];controls[axis]=proposed[1]
            key=refinement.control_key(controls)
            if any(refinement.control_key(e['controls'])==key for e in family()):
                jac=None
                continue
            actual=trial(anchor,proposed)
            if not self.protected(i,actual) or self.regional_score(i,actual)>=self.regional_score(i,anchor):
                # A nonlinear or clipping boundary can invalidate the full
                # Newton step without making the target itself unreachable.
                for fraction in (.5,.25):
                    candidate=trial(anchor,pos+fraction*(proposed-pos))
                    if self.protected(i,candidate) and self.regional_score(i,candidate)<self.regional_score(i,anchor):
                        break
                jac=None
        result=best()
        if self.protected(i,result) and self.regional_score(i,result)<=1:
            return result
        raise TargetUnreachable(f'第{i+1}帧主体/亮部联合目标未实现：残差{self.regional_error(i,result).round(4).tolist()}EV，参数{result["controls"]}')

    def regional_tone_policy(self):
        path=self.folder/'regional-tone-policy-review.json'
        checks=json.loads(safe_read(path,32*1024*1024)).get('attempts',[]) if path.exists() else []
        # A constant sky offset changes global contrast, never the natural time
        # trajectory. It is frozen with the effective policy before full output.
        for axis,offset,ceiling in self.regional_candidates():
            prior=next((c for c in checks if c['curve_white']==ceiling
                        and c.get('axis',2)==axis and c.get('sky_offset_ev',0.)==offset),None)
            if prior is not None and prior['failed_frames']: continue
            entries={};failures=[]
            pilots=self.pilot_indices()
            # Rank by actual required correction, never by scene-specific frame IDs.
            order=sorted(pilots,key=lambda i:abs(self.target[i,0]-self.base['observations'][i]['brightness'][0]),reverse=True)
            for i in order:
                try:
                    entries[i]=self.select_regional(i,ceiling,axis,offset)
                except TargetUnreachable as exc:
                    failures.append({'frame':i+1,'reason':str(exc)})
            checks.append({'curve_white':ceiling,'axis':axis,'sky_offset_ev':offset,
                           'failed_frames':failures,'entries':{str(i):e for i,e in entries.items()}})
            automatic.atomic_json(path,{'attempts':checks})
            if not failures:
                self.state.update(curve_white=ceiling,tone_policy='frozen-mid-sky-measured-two-controls',
                                  regional_axis=axis,sky_offset_ev=offset)
                self.save()
                return ceiling
        raise ValueError('主体与亮部联合实测仍有未解决帧，保留全部证据；未执行全量')

    def curve_candidates(self):
        if not getattr(self,'product',False): return (255,248,240,232)
        if hasattr(self,'_curve_candidates'): return self._curve_candidates
        # Keep user-created point curves. Only linear originals allow a common ceiling.
        self._curve_candidates=(255,248,240,232)
        for i in range(self.n):
            ctx=self.context(i)
            try: refinement.recipe(ctx['base'],ctx['frame']['original_controls']+[248.])
            except ValueError:
                self._curve_candidates=(255,);break
        return self._curve_candidates

    def regional_candidates(self):
        candidates=[]
        key=getattr(self,'primary_region','sky')
        for ceiling in self.curve_candidates():
            offsets=[0.]
            if getattr(self,'product',False):
                # A scene-wide contrast offset is estimated from measured pilot
                # renders, not an inherited sea/sky setting. Never refit time variation.
                residual=[]
                for i in self.pilot_indices():
                    feasible=[e for e in self.history(i) if e['controls'][3]==ceiling
                              and self.protected(i,e) and self.score(i,e)<=.04]
                    if feasible:
                        e=min(feasible,key=lambda e:self.score(i,e))
                        residual.append(e['regions'][key]-self.plan['regional_targets'][key][i])
                if residual:
                    offset=round(float(np.clip(np.median(residual),-.25,.25)),2)
                    if abs(offset)>=.02: offsets.append(offset)
            for offset in offsets:
                candidates.extend((axis,offset,ceiling) for axis in (1,2))
        return candidates

    def freeze_tone_policy(self,ceiling):
        policy={'schema':1,'target_sha256':self.plan['plan_sha256'],
                'curve_white':ceiling,'tone_policy':self.state.get('tone_policy','original-highlights-whites-with-uniform-master-curve'),
                'regional_axis':self.state.get('regional_axis'),
                'sky_offset_ev':self.state.get('sky_offset_ev',0.),
                'interpretation':'constant whole-sequence display contrast; no temporal target refitting'}
        path=self.folder/'effective-tone-policy.json'
        if path.exists():
            if json.loads(safe_read(path,1024*1024))!=policy:
                raise ValueError('已冻结的有效显影目标发生改变；不能覆盖原视频来源')
        else:
            automatic.atomic_json(path,policy)
        self.state['effective_policy_sha256']=automatic.file_hash(path)
        self.save()
        return policy

    def uniform_tone_policy(self):
        if 'curve_white' in self.state: return self.state['curve_white']
        prior=self.folder/'tone-policy-review.json'
        if prior.exists():
            attempts=json.loads(safe_read(prior,4*1024*1024)).get('attempts',[])
            if [a['curve_white'] for a in attempts]==[255,248,240,232]:
                return self.regional_tone_policy()
        pilots = self.pilot_indices()
        checks = []
        for ceiling in self.curve_candidates():
            entries = {}; unreachable = []; self.report(f'实测整段统一高光方案 · 白端{ceiling}',0,len(pilots))
            for i in pilots:
                try:
                    entries[i] = self.select_exposure(i,ceiling)
                except TargetUnreachable as exc:
                    unreachable.append({'frame':i+1,'reason':str(exc)})
            bad = [i+1 for i,e in entries.items() if not self.protected(i,e)]
            sky_steps = []
            key=getattr(self,'primary_region','sky')
            sky_target = self.plan['regional_targets'][key]
            for i in self.plan['camera_events']:
                if i-1 in entries and i in entries:
                    residual_step = ((entries[i]['regions'][key]-sky_target[i])
                                     -(entries[i-1]['regions'][key]-sky_target[i-1]))
                    sky_steps.append({'frame':i+1,'residual_step_ev':float(residual_step)})
            bad_sky = [v['frame'] for v in sky_steps if abs(v['residual_step_ev']) > .08]
            checks.append({'curve_white':ceiling,'frames':[i+1 for i in pilots],
                           'unreachable':unreachable,'failed_highlight_frames':bad,
                           'sky_event_steps':sky_steps,'failed_sky_event_frames':bad_sky})
            automatic.atomic_json(self.folder/'tone-policy-review.json',{'attempts':checks})
            if getattr(self,'product',False):
                # Match the regional trajectory across ALL pilot times, not just camera edges.
                bad_sky += [i+1 for i,e in entries.items() if abs(e['regions'][key]-sky_target[i])>.02]
            if not bad and not unreachable and not bad_sky:
                self.state['curve_white'] = ceiling
                if getattr(self,'product',False):
                    # Every subsequent frame also matches the regional target.
                    self.state.update(tone_policy='frozen-mid-sky-measured-two-controls',regional_axis=1,sky_offset_ev=0.)
                else:
                    self.state['tone_policy'] = 'original-highlights-whites-with-uniform-master-curve'
                self.save()
                return ceiling
        return self.regional_tone_policy()

    def export(self,confirmation):
        if not getattr(self,'product',False):
            raise ValueError('此旧版预览只读保留；全尺寸导出需要用户确认新版流程的具体视频')
        from .trajectory_export import export
        return export(self,confirmation)

    def review_entries(self,observations):
        review=trajectory_model.review_sequence(self.plan,observations)
        policy=json.loads(safe_read(self.folder/'effective-tone-policy.json',1024*1024))
        review['effective_tone_policy']=policy
        review['uniform_curve_white']=policy['curve_white']
        if getattr(self,'product',False):
            residual=np.array([e['regions'][self.primary_region] for e in observations])-np.array(self.plan['regional_targets'][self.primary_region])-policy['sky_offset_ev']
            mid=np.array([e['brightness'][0] for e in observations])-self.target[:,0]
            review['primary_offset_adjusted_max_error_ev']=float(max(abs(residual)))
            review['primary_max_adjacent_residual_ev']=float(max(abs(np.diff(residual)),default=0))
            review['strict_failed_frames']=[i+1 for i in range(self.n) if abs(mid[i])>.012+1e-9 or abs(residual[i])>.02+1e-9]
            review['numeric_conditions_met']=review['numeric_conditions_met'] and not review['strict_failed_frames']
        review['highlight_protection_passed']=all(self.protected(i,e) for i,e in enumerate(observations))
        review['numeric_conditions_met']=review['numeric_conditions_met'] and review['highlight_protection_passed']
        return review

    def execute(self):
        if self.state['state'] in (WAITING,'done') or self.state.get('result',{}).get('video'):
            result = self.state['result']
            if automatic.file_hash(result['video']) != result['sha256']:
                raise ValueError('待确认视频已改变，不能复用原预览记录')
            if (automatic.file_hash(self.folder/'selection.json') != result['selection_sha256']
                    or result['target_sha256'] != self.plan['plan_sha256']):
                raise ValueError('待确认视频对应的帧选择或目标已改变')
            if automatic.file_hash(self.folder/'effective-tone-policy.json') != result['effective_policy_sha256']:
                raise ValueError('待确认视频的有效显影目标已改变')
            return result
        try:
            self.report('连接Adobe · 准备整段曝光实测')
            self.renderer.preflight()
            ceiling = self.uniform_tone_policy()
            policy = self.freeze_tone_policy(ceiling)
            for i in range(self.n):
                if self.state.get('tone_policy')=='frozen-mid-sky-measured-two-controls':
                    entry=self.select_regional(i,ceiling,self.state['regional_axis'],self.state['sky_offset_ev'])
                else:
                    entry=self.select_exposure(i,ceiling)
                if not self.protected(i,entry):
                    self.chosen[i] = entry; self.save_selection()
                    raise ValueError(f'第{i+1}帧未满足高光保护限制，需重新评估统一方案')
                self.chosen[i] = entry
                self.save_selection()
            self.check_source()
            observations = [self.chosen[i] for i in range(self.n)]
            review = self.review_entries(observations) if getattr(self,'product',False) else trajectory_model.review_sequence(self.plan,observations)
            review['uniform_curve_white'] = ceiling
            review['effective_tone_policy']=policy
            key=getattr(self,'primary_region','sky')
            sky_residual=np.array([e['regions'][key] for e in observations])-np.array(self.plan['regional_targets'][key]) if 'regional_targets' in self.plan else None
            if sky_residual is not None:
                review['sky_absolute_residual_range_ev']=[float(min(sky_residual)),float(max(sky_residual))]
                review['sky_offset_adjusted_max_error_ev']=float(max(abs(sky_residual-policy['sky_offset_ev'])))
            review['highlight_protection_passed'] = all(self.protected(i,e) for i,e in self.chosen.items())
            automatic.atomic_json(self.folder/'sequence-review.json',review)
            # Even a numerical failure may be useful to inspect, but is never
            # called successful. The entire actual sequence is encoded first.
            from .preview_video import encode_preview
            self.report('生成完整低分辨率视频',0,self.n)
            video = encode_preview(observations,self.folder/'预览视频',fps=self.state['preview_fps'],
                                   cancelled=self.cancelled,progress=self.progress)
            self.check_source()
            result = {**video,'run_id':self.state.get('id'),'region_contract':getattr(self,'region_contract',None),'raw_dir':str(self.source) if hasattr(self,'source') else None,'state':WAITING,'verified':False,'numeric_conditions_met':review['numeric_conditions_met'],
                      'effective_policy_sha256':self.state['effective_policy_sha256'],'effective_tone_policy':policy,
                      'target_sha256':self.plan['plan_sha256'],'selection_sha256':automatic.file_hash(self.folder/'selection.json'),
                      'count':self.n,'review':review,'visual_playback_verified':False,'full_export_authorized':False,
                      'message':'完整低分辨率视频已生成，等待用户播放确认；尚未授权全尺寸导出'}
            self.state.update(state=WAITING,phase='等待完整视频确认',done=self.n,total=self.n,result=result,error=None)
            self.save()
            return result
        except Exception as exc:
            self.state.update(state='stopped' if isinstance(exc,Cancelled) else 'error',error=str(exc))
            self.save()
            raise


def run(output,progress=None,cancelled=None,renderer_factory=PhotoshopRenderer):
    output = output_path(output)
    with automatic.render_lock(output):
        return Trajectory(output,progress,cancelled,renderer_factory).execute()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='整段轨迹低分辨率视频；完成后等待确认')
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    result = run(args.output,progress=lambda done,total,phase: print(json.dumps({'done':done,'total':total,'phase':phase},ensure_ascii=False),flush=True))
    print(json.dumps(result,ensure_ascii=False),flush=True)
