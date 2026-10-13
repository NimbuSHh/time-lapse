"""Sample-gated, measured refinement of an existing automatic run.

Separate from the manual workflows. Frozen targets, source media and the old run
remain immutable. Every trial uses Adobe on a private RAW copy; predictions only
choose trials and never satisfy the quality gate.
"""
from contextlib import contextmanager
from io import BytesIO
import fcntl
import json
from pathlib import Path
import shutil
import uuid
import xml.etree.ElementTree as ET

import numpy as np
from PIL.JpegImagePlugin import JpegImageFile

from . import automatic, joint_feedback as joint, joint_model, tone_xmp, refinement_model
from .feedback import target_hash
from .photoshop import PhotoshopRenderer, Cancelled
from .project import digest, safe_read
from .provenance import xmp_evidence
from .workspace import output_path

VERSION = 'staged-measured-v2'
THRESHOLDS = {'max_tone_error_ev': .08, 'max_adjacent_tone_residual_ev': .06,
              'clip_fraction_increase': .001, 'max_channel_tail_error_ev': .02}
# One-based inclusive intervals; targets continue to come from the full sequence.
SAMPLE_RANGES = ((141, 157), (331, 347), (501, 517), (652, 668))
CENTERS = (149, 339, 509, 660)
CRS = 'http://ns.adobe.com/camera-raw-settings/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'


def load_latest(output):
    output = output_path(output)
    try:
        index = json.loads(safe_read(output/'refinement-latest.json', 1024*1024))
        folder = Path(index['path']).resolve()
        if output not in folder.parents or not folder.name.startswith('分层修正-'):
            raise ValueError('新流程记录路径不符')
        state = json.loads(safe_read(folder/'state.json', 8*1024*1024))
        if state['path'] != str(folder):
            raise ValueError('新流程状态路径不符')
        return state
    except FileNotFoundError:
        return None


def recipe(base, controls):
    """Optional master-curve white endpoint; other develop fields preserved."""
    if len(controls) != 4 or not 220 <= controls[3] <= 255 or controls[3] != round(controls[3]):
        raise ValueError('曲线白端必须为220–255的整数')
    result = tone_xmp.update(base, controls[:3])
    if controls[3] == 255:
        return result
    root = ET.fromstring(result)
    rdf = root if root.tag == '{'+RDF+'}RDF' else root.find('{'+RDF+'}RDF')
    descs = [] if rdf is None else rdf.findall('{'+RDF+'}Description')
    if len(descs) != 1:
        raise ValueError('曲线试验需要唯一顶层设置')
    desc = descs[0]
    curve = desc.find('{'+CRS+'}ToneCurvePV2012')
    if curve is None or [x.text.strip() for x in curve.findall('.//{'+RDF+'}li')] != ['0, 0', '255, 255']:
        raise ValueError('当前曲线不是线性曲线，不能替换已有创作曲线')
    points = curve.findall('.//{'+RDF+'}li')
    points[-1].text = '255, '+str(int(controls[3]))
    # ACR materializes this default when a custom point curve is first used.
    # Record it explicitly so the embedded rendered recipe must still match.
    saturation = '{'+CRS+'}CurveRefineSaturation'
    if saturation not in desc.attrib and desc.find(saturation) is None:
        desc.set(saturation, '100')
    key = '{'+CRS+'}ToneCurveName2012'
    if key in desc.attrib:
        desc.set(key, 'Custom')
    else:
        child = desc.find(key)
        if child is None:
            raise ValueError('缺少原曲线名称')
        child.text = 'Custom'
    changed = ET.tostring(root, encoding='utf-8')
    before, after = xmp_evidence(result)['crs'], xmp_evidence(changed)['crs']
    allowed = {'ToneCurvePV2012', 'ToneCurveName2012'}
    if 'CurveRefineSaturation' not in before:
        allowed.add('CurveRefineSaturation')
    if {k:v for k,v in before.items() if k not in allowed} != {k:v for k,v in after.items() if k not in allowed}:
        raise ValueError('曲线试验改变了其他显影设置')
    return changed


def control_key(controls):
    return tuple(round(float(x), 2 if i == 0 else 0) for i,x in enumerate(controls))


def quality(observation, target, reference_clip):
    assessed = refinement_model.assess(observation, target, reference_clip)
    error = np.asarray(observation['brightness'])-target
    excess = np.asarray(observation['channel_clip_fraction'])-reference_clip
    # Stricter search margins make adjacent residuals attainable; published
    # acceptance remains exactly the frozen original thresholds.
    search = np.r_[abs(error[:3])/.025, np.maximum(error[3:], 0)/.015,
                   np.maximum(excess, 0)/.0008]
    accepted = assessed['accepted']
    return {'accepted': accepted, 'error': error.tolist(), 'clip_increase': excess.tolist(),
            'score': float(max(search)+.02*np.linalg.norm(error[:3])/.025)}


def sequence_check(entries, target, reference_clips, indices):
    indices = sorted(indices)
    residuals = {i: np.asarray(entries[i]['brightness'])-target[i] for i in indices}
    bad = [i+1 for i in indices if not quality(entries[i], target[i], reference_clips[i])['accepted']]
    pairs = [(i-1,i) for i in indices if i-1 in residuals]
    adjacent = [(a+1,b+1,float(max(abs(residuals[b][:3]-residuals[a][:3])))) for a,b in pairs]
    bad_pairs = [list(p) for p in adjacent if p[2] > .06]
    return {'accepted': not bad and not bad_pairs, 'frame_count': len(indices),
            'failed_frames': bad, 'failed_adjacent_pairs': bad_pairs,
            'max_tone_error_ev': float(max((max(abs(v[:3])) for v in residuals.values()), default=0)),
            'max_adjacent_tone_residual_ev': max((p[2] for p in adjacent), default=0),
            'thresholds': dict(THRESHOLDS), 'visual_playback_verified': False}


class Refinement:
    def __init__(self, output, progress=None, cancelled=None, renderer_factory=PhotoshopRenderer):
        self.output = output_path(output)
        self.progress = progress
        self.cancelled = cancelled or (lambda: False)
        old = automatic.load_latest(self.output)
        if not old or old['state'] not in ('done', 'error', 'stopped'):
            raise ValueError('需要已停止且有完整校准记录的原任务；不会中断正在运行的任务')
        self.original = old
        self.source = Path(old['raw_dir'])
        self.old_folder = Path(old['path'])
        self.check_source()
        self.packages = {}
        for label, phase in old['phases'].items():
            p = Path(phase['result']['path']).resolve()
            if self.old_folder not in p.parents:
                raise ValueError('原任务校准记录越界')
            data = safe_read(p/'manifest.json', 64*1024*1024)
            if digest(data) != phase['sha256']:
                raise ValueError('原任务中间记录已改变')
            self.packages[label] = (p, json.loads(data))
        key = '第 1 轮准备校准'
        if key not in self.packages:
            raise ValueError('缺少首次冻结目标')
        self.base_folder, self.base = self.packages[key]
        joint.manifest(self.base_folder, {}, 'joint-feedback-calibration')
        self.target = np.asarray(self.base['target'])
        self.n = len(self.base['frames'])
        if len(old['snapshot']) != self.n:
            raise ValueError('原素材与目标帧数不符')
        state = load_latest(self.output)
        if state and state.get('source_run') == old['id']:
            if state['target_sha256'] != self.base['target_sha256']:
                raise ValueError('新流程目标发生改变')
            self.state = state
            if state['version'] != VERSION:
                self.state.setdefault('previous_versions', []).append(state['version'])
                self.state['version'] = VERSION
                self.state['state'] = 'ready'
                self.state.pop('result', None)
        else:
            folder = self.old_folder/('分层修正-'+uuid.uuid4().hex[:12])
            folder.mkdir()
            self.state = {'schema': 1, 'id': folder.name, 'path': str(folder), 'source_run': old['id'],
                          'version': VERSION, 'target_sha256': self.base['target_sha256'],
                          'measurement_sha256': self.base['measurement_sha256'], 'state': 'ready',
                          'phase': '准备小样', 'count': self.n, 'done': 0, 'total': self.n,
                          'new_renders': 0, 'reused_renders': 0, 'sample_ranges': SAMPLE_RANGES}
        self.folder = Path(self.state['path'])
        self.renderer = renderer_factory(self.folder, self.cancelled)
        if old['renderer'] != self.renderer.identity:
            raise ValueError('显影器版本已改变，不能混用旧校准与新显影')
        self.frames = {}
        self.reference_clips = {}
        self.histories = {}
        self.chosen = {}
        selection = self.folder/'selection.json'
        self.saved_selection = json.loads(safe_read(selection,16*1024*1024)) if selection.exists() else {}
        previous_review = self.folder/'sequence-review.json'
        self.retry_indices = set()
        if previous_review.exists():
            review = json.loads(safe_read(previous_review,4*1024*1024))
            self.retry_indices.update(n-1 for n in review.get('failed_frames',[]))
            for pair in review.get('failed_adjacent_pairs',[]):
                self.retry_indices.update(n-1 for n in pair[:2])
        self.save()
        automatic.atomic_json(self.output/'refinement-latest.json', {'path': str(self.folder)})

    def save(self):
        automatic.atomic_json(self.folder/'state.json', self.state)

    def report(self, text, done=0, total=None):
        if self.cancelled():
            raise Cancelled('已停止，新显影与旧结果均保留，可继续')
        self.state.update(state='running', phase=text, done=done, total=total or self.n, error=None)
        self.save()
        if self.progress:
            self.progress(done, total or self.n, text)

    def check_source(self):
        if automatic.inputs(self.source)[1] != self.original['snapshot']:
            raise ValueError('原素材与冻结快照不一致，已停止')

    def context(self, i):
        if i in self.frames:
            return self.frames[i]
        frame = self.base['frames'][i]
        name = self.original['snapshot'][i]['raw']['name']
        if Path(name).stem.casefold() != Path(frame['preview']).stem.casefold():
            raise ValueError('素材与目标顺序不一致')
        reference = safe_read(Path(frame['reference_preview']), 25*1024*1024)
        if digest(reference) != frame['reference_sha256']:
            raise ValueError('首次参考图发生改变')
        ref, size = joint_model.pixels(reference)
        base = safe_read(self.base_folder/'baseline'/frame['xmp'], 4*1024*1024)
        if digest(base) != frame['baseline_sha256']:
            raise ValueError('首次配方发生改变')
        joint.match(base, reference, name)
        obs = joint_model.measure(ref, self.base['mid_roi'], self.base['high_roi'], ref, self.base.get('measurement_selection'))
        self.reference_clips[i] = obs['channel_clip_fraction']
        value = {'frame': frame, 'reference': ref, 'size': size, 'base': base, 'source': self.original['snapshot'][i]}
        self.frames[i] = value
        if len(self.frames) > 4:
            del self.frames[next(iter(self.frames))]
        return value

    def measure(self, i, data):
        ctx = self.context(i)
        linear, size = joint_model.pixels(data)
        if size != ctx['size']:
            raise ValueError('显影预览尺寸与原基准不符')
        return joint_model.measure(linear, self.base['mid_roi'], self.base['high_roi'], ctx['reference'], self.base.get('measurement_selection'))

    def cache_key(self, i, xmp, edge=1600):
        return digest(json.dumps([self.context(i)['source'], digest(xmp), edge, self.renderer.identity], sort_keys=True).encode())

    def history(self, i):
        if i in self.histories:
            return self.histories[i]
        ctx = self.context(i)
        stem = Path(ctx['source']['raw']['name']).stem
        entries = []
        groups = [self.old_folder/'initial']
        for round_no in (1,2):
            groups += [self.old_folder/f'round-{round_no}'/n for n in ('baseline','probe-exposure','probe-highlights','probe-whites','corrected')]
        seen = set()
        for group in groups:
            rec = group/'receipts'/(stem+'.json')
            if not rec.exists():
                continue
            xmp_path, jpg_path = group/'raw'/(stem+'.xmp'), group/'jpg'/(stem+'.jpg')
            xmp = safe_read(xmp_path, 4*1024*1024)
            controls = tone_xmp.values(xmp)+[255.]
            k = control_key(controls)
            if k in seen:
                continue
            # Only inherit trials that changed these three scalars from the
            # frozen recipe. Other edits cannot silently become local evidence.
            expected = recipe(ctx['base'], controls)
            if xmp_evidence(expected)['crs'] != xmp_evidence(xmp)['crs']:
                continue
            receipt = json.loads(safe_read(rec, 1024*1024))
            if receipt['key'] != self.cache_key(i, xmp):
                raise ValueError('旧显影的来源或配方指纹不符')
            data = safe_read(jpg_path, 25*1024*1024)
            if digest(data) != receipt['sha256']:
                raise ValueError('旧显影文件校验失败')
            joint.match(xmp, data, stem)
            obs = self.measure(i, data)
            entries.append({**obs, 'controls': controls, 'jpg': str(jpg_path), 'xmp': str(xmp_path),
                            'sha256': receipt['sha256'], 'recipe_sha256': digest(xmp), 'reused': True})
            seen.add(k)
        # A new trajectory may reuse verified renders, but never an old target
        # or acceptance decision. The default preserves the original workflow.
        for history_root in getattr(self, 'history_roots', [self.folder]):
            frame_dir = history_root/'frames'/stem
            if not frame_dir.exists():
                continue
            for receipt_path in sorted(frame_dir.glob('*/receipt.json')):
                item = json.loads(safe_read(receipt_path, 1024*1024))
                xmp = safe_read(receipt_path.parent/'recipe.xmp', 4*1024*1024)
                jpg = safe_read(receipt_path.parent/'preview.jpg', 25*1024*1024)
                if (item['key'] != self.cache_key(i,xmp) or digest(jpg) != item['sha256']
                        or digest(xmp) != item['recipe_sha256']):
                    raise ValueError('新显影缓存校验失败')
                if digest(recipe(ctx['base'], item['controls'])) != digest(xmp):
                    raise ValueError('试验参数与配方不符')
                joint.match(xmp, jpg, stem)
                obs = self.measure(i, jpg)
                entries.append({**item, **obs, 'reused': history_root != self.folder or item.get('reused', False)})
        if not entries:
            raise ValueError('缺少可核验的原始显影')
        self.histories[i] = entries
        self.state['reused_renders'] += len([e for e in entries if e.get('reused')])
        return entries

    def render(self, i, controls):
        ctx = self.context(i)
        controls = list(control_key(controls))
        baseline = ctx['frame']['original_controls'][0]
        if abs(controls[0]-baseline) > 2+1e-8:
            raise ValueError('曝光累计修正超过原有±2 EV限制')
        hist = self.history(i)
        for item in hist:
            if control_key(item['controls']) == control_key(controls):
                return item
        self.check_source()
        xmp = recipe(ctx['base'], controls)
        key = self.cache_key(i, xmp)
        stem = Path(ctx['source']['raw']['name']).stem
        trial = self.folder/'frames'/stem/key[:20]
        trial.mkdir(parents=True, exist_ok=True)
        raw_dir = trial/'raw'
        raw_dir.mkdir(exist_ok=True)
        raw = raw_dir/ctx['source']['raw']['name']
        automatic.copy_raw(self.source/raw.name, raw)
        (raw_dir/(stem+'.xmp')).write_bytes(xmp)
        if 'acr' in ctx['source']:
            name = ctx['source']['acr']['name']
            shutil.copyfile(self.source/name, raw_dir/name)
        (trial/'recipe.xmp').write_bytes(xmp)
        jpg = trial/'preview.jpg'
        if jpg.exists():
            jpg.rename(trial/('incomplete-'+uuid.uuid4().hex+'.jpg'))
        self.report(f'小样/序列实测 · 第{i+1}帧 · 已新增{self.state["new_renders"]}张', i, self.n)
        self.renderer.render(raw, jpg, 1600)
        data = safe_read(jpg, 25*1024*1024)
        joint.match(xmp, data, stem)
        self.check_source()
        obs = self.measure(i, data)
        item = {**obs, 'controls': controls, 'key': key, 'sha256': digest(data),
                'recipe_sha256': digest(xmp), 'jpg': str(jpg), 'xmp': str(trial/'recipe.xmp'), 'reused': False}
        automatic.atomic_json(trial/'receipt.json', item)
        hist.append(item)
        self.state['new_renders'] += 1
        self.save()
        return item

    def score(self, i, entry):
        return quality(entry, self.target[i], self.reference_clips[i])['score']

    def best(self, i):
        # A lower search score is not permission to replace a feasible render
        # with one that violates a published limit.
        return min(self.history(i), key=lambda e:(
            not quality(e,self.target[i],self.reference_clips[i])['accepted'],
            self.score(i,e)))

    def refinement_anchor(self, i):
        best = self.best(i)
        q = quality(best,self.target[i],self.reference_clips[i])
        if q['accepted']:
            return best
        if max(q['error'][3:]) > .02 or max(q['clip_increase']) > .001:
            # Clipped channels can have a flat local derivative. An already
            # measured unclipped curve branch may initially have worse tone
            # error, but provides the response needed to leave that plateau.
            feasible = []
            for entry in self.history(i):
                check = quality(entry,self.target[i],self.reference_clips[i])
                if max(check['error'][3:]) <= .02 and max(check['clip_increase']) <= .001:
                    feasible.append(entry)
            if feasible:
                return min(feasible,key=lambda e:self.score(i,e))
        return best

    def exposure(self, i, controls, budget=6):
        # This stage measures the exposure-only root even if highlights fail.
        # It never declares acceptance; all trials retain full strict metrics.
        current = self.render(i, controls)
        for step in range(budget):
            family = [h for h in self.history(i) if control_key(h['controls'])[1:] == control_key(controls)[1:]]
            anchor = min(family,key=lambda h:abs(h['brightness'][0]-self.target[i,0]))
            error = anchor['brightness'][0]-self.target[i,0]
            if abs(error) <= .01:
                break
            ordered = sorted(family,key=lambda h:h['controls'][0])
            bracket = [(a,b) for a,b in zip(ordered,ordered[1:])
                       if a['brightness'][0] <= self.target[i,0] <= b['brightness'][0]
                       and b['brightness'][0] > a['brightness'][0]]
            if bracket:
                a,b = min(bracket,key=lambda pair:pair[1]['controls'][0]-pair[0]['controls'][0])
                e = a['controls'][0]+(self.target[i,0]-a['brightness'][0])*(b['controls'][0]-a['controls'][0])/(b['brightness'][0]-a['brightness'][0])
                if any(abs(e-h['controls'][0])<.005 for h in family):
                    e = (a['controls'][0]+b['controls'][0])/2
            else:
                other = [h for h in family if abs(h['controls'][0]-anchor['controls'][0]) >= .05]
                if other:
                    h = min(other,key=lambda h:abs(h['controls'][0]-anchor['controls'][0]))
                    gain = (h['brightness'][0]-anchor['brightness'][0])/(h['controls'][0]-anchor['controls'][0])
                    if not .1 < gain < 5:
                        break
                    e = anchor['controls'][0]+float(np.clip(-error/gain,-.75,.75))
                else:
                    e = anchor['controls'][0]+(.2 if error<0 else -.2)
            baseline = self.context(i)['frame']['original_controls'][0]
            e = round(max(max(-5,baseline-2),min(min(5,baseline+2),e)),2)
            candidate = [e,*controls[1:]]
            if any(control_key(h['controls']) == control_key(candidate) for h in family):
                break
            current = self.render(i,candidate)
        return min((h for h in self.history(i) if control_key(h['controls'])[1:] == control_key(controls)[1:]),key=lambda h:self.score(i,h))

    def optimize(self, i, budget=36):
        self.report(f'分析实测响应 · 第{i+1}帧', i, self.n)
        hist = self.history(i)
        saved = self.saved_selection.get(str(i))
        if saved and i not in self.retry_indices:
            # The persisted selection is only a lookup hint. Reuse requires
            # history() to verify and remeasure its actual JPEG and recipe.
            for entry in hist:
                if (entry['sha256'] == saved['sha256'] and entry['recipe_sha256'] == saved['recipe_sha256']
                        and quality(entry,self.target[i],self.reference_clips[i])['accepted']):
                    return entry
        start_count = len(hist)
        best = self.best(i)
        if self.score(i,best) <= 1:
            return best
        seeded = False
        # Nearby accepted real frames seed a trial, not an acceptance decision.
        # Keep the original per-frame recipe and transfer only the four allowed
        # corrections, then solve this frame's exposure with actual renders.
        neighbors = [j for j in self.chosen if j != i]
        if neighbors:
            j = min(neighbors, key=lambda j:abs(i-j))
            neighbor = self.chosen[j]
            if quality(neighbor,self.target[j],self.reference_clips[j])['accepted']:
                origin = self.context(i)['frame']['original_controls']+[255.]
                previous = self.context(j)['frame']['original_controls']+[255.]
                seed = np.asarray(origin)+np.asarray(neighbor['controls'])-previous
                seed = refinement_model.quantize_controls(seed,origin[0])
                self.exposure(i,seed,3)
                best = self.best(i)
                if self.score(i,best) <= 1:
                    return best
                seeded = quality(best,self.target[i],self.reference_clips[i])['accepted']
        # Separate global luminance from tone-shape controls. First try exposure
        # with the inherited creative recipe; do not linearly extrapolate H/W.
        original = self.context(i)['frame']['original_controls']+[255.]
        if not seeded:
            self.exposure(i, original)
        best = self.best(i)
        if self.score(i,best) <= 1:
            return best
        # Only after measured exposure-only failure, test a smooth master curve.
        # It is applied on independent copies of an originally linear curve.
        for ceiling in (() if seeded else (250., 240.)):
            c = list(best['controls']); c[3] = ceiling
            self.exposure(i,c,4)
            best = self.best(i)
            if self.score(i,best) <= 1:
                return best
        # Local, measured multi-control refinement. Finite differences are
        # refreshed after each accepted move. Non-improving trials are retained
        # for diagnosis but never overwrite the best actual render.
        stagnant = 0
        radii = np.array([.15,10.,10.,3.])
        while len(hist)-start_count < budget and stagnant < 3:
            best = self.refinement_anchor(i)
            if self.score(i,best) <= 1:
                break
            c = np.asarray(best['controls'])
            y = np.r_[best['brightness'],best['channel_clip_fraction']]
            columns = []
            for axis in range(4):
                p = c.copy()
                low = [max(-5.,self.context(i)['frame']['original_controls'][0]-2),-100.,-100.,220.][axis]
                high = [min(5.,self.context(i)['frame']['original_controls'][0]+2),100.,100.,255.][axis]
                delta = radii[axis] if c[axis]+radii[axis] <= high else -radii[axis]
                p[axis] = max(low,min(high,p[axis]+delta))
                item = self.render(i,p)
                columns.append((np.r_[item['brightness'],item['channel_clip_fraction']]-y)/(p[axis]-c[axis]))
            jac = np.asarray(columns).T
            axes = [np.arange(-10,11)*.02, np.arange(-2,3)*5., np.arange(-2,3)*5., np.arange(-3,4)]
            if stagnant:
                axes = [a/(2**stagnant) for a in axes]
            delta = np.array(np.meshgrid(*axes,indexing='ij')).reshape(4,-1).T
            candidates = c+delta
            candidates[:,0] = np.round(candidates[:,0],2)
            candidates[:,1:] = np.round(candidates[:,1:])
            lo = np.array([max(-5.,self.context(i)['frame']['original_controls'][0]-2),-100,-100,220])
            hi = np.array([min(5.,self.context(i)['frame']['original_controls'][0]+2),100,100,255])
            candidates = candidates[np.all((candidates>=lo)&(candidates<=hi),axis=1)]
            tried = {control_key(h['controls']) for h in hist}
            candidates = np.array([p for p in candidates if control_key(p) not in tried])
            if not len(candidates):
                break
            pred = y+(candidates-c)@jac.T
            err = pred[:,:6]-self.target[i]
            clip = pred[:,6:]-self.reference_clips[i]
            objective = np.max(np.column_stack((abs(err[:,:3])/.025,np.maximum(err[:,3:],0)/.015,np.maximum(clip,0)/.0008)),axis=1)
            objective += .02*np.linalg.norm(err[:,:3],axis=1)/.025
            chosen = candidates[int(np.argmin(objective))]
            actual = self.render(i,chosen)
            gain = self.score(i,best)-self.score(i,actual)
            stagnant = 0 if gain > .02 else stagnant+1
        result = self.best(i)
        return result

    def check(self, indices):
        entries = {i:self.chosen[i] for i in indices}
        return sequence_check(entries,self.target,self.reference_clips,indices)

    def save_selection(self):
        automatic.atomic_json(self.folder/'selection.json', {str(i):e for i,e in self.chosen.items()})

    def execute(self):
        try:
            self.report('连接 Photoshop · 准备四段小样')
            self.renderer.preflight()
            centers = [n-1 for n in CENTERS if n <= self.n]
            sample = sorted({i for a,b in SAMPLE_RANGES for i in range(a-1,min(b,self.n))})
            if not centers or not sample:
                raise ValueError('当前任务不足以覆盖冻结的小样范围')
            # Pilot failures stop before the other frames, not after 810 renders.
            for i in centers:
                self.chosen[i] = self.optimize(i)
                self.save_selection()
            pilot = self.check(centers)
            automatic.atomic_json(self.folder/'pilot-review.json',pilot)
            if not pilot['accepted']:
                return self.review_needed('代表帧仍未通过原标准，保留试验结果供诊断，未启动全量处理',pilot)
            for j,i in enumerate(sample):
                self.report('四段连续小样验证',j,len(sample))
                self.chosen[i] = self.optimize(i)
                self.save_selection()
            check = self.check(sample)
            automatic.atomic_json(self.folder/'sample-review.json',check)
            if not check['accepted']:
                return self.review_needed('小样连续性未通过原标准，未启动全量处理',check)
            self.state['sample_accepted'] = True
            self.save()
            for i in range(self.n):
                self.report('复用旧显影 · 全序列分层修正',i,self.n)
                if i not in self.chosen:
                    self.chosen[i] = self.optimize(i)
                self.save_selection()
            full = self.check(range(self.n))
            automatic.atomic_json(self.folder/'sequence-review.json',full)
            if not full['accepted']:
                return self.review_needed('全序列仍有未通过项，保留候选预览，未导出全尺寸',full)
            return self.export(full)
        except Exception as exc:
            self.state.update(state='stopped' if isinstance(exc,Cancelled) else 'error',error=str(exc))
            self.save()
            raise

    def review_needed(self,message,review):
        self.check_source()
        result = {'verified':False,'message':message,'path':str(self.folder),'review':review,
                  'count':len(self.chosen),'new_renders':self.state['new_renders']}
        self.state.update(state='review-needed',phase=message,result=result,error=None)
        self.save()
        return result

    def export(self,review):
        results = self.folder/'结果'
        xmps,jpgs = results/'修正XMP',results/'JPG'
        xmps.mkdir(parents=True,exist_ok=True);jpgs.mkdir(parents=True,exist_ok=True)
        audit = {}
        for i in range(self.n):
            self.report('验收通过 · 导出全尺寸',i,self.n)
            ctx,entry = self.context(i),self.chosen[i]
            stem = Path(ctx['source']['raw']['name']).stem
            xmp = safe_read(Path(entry['xmp']),4*1024*1024)
            if digest(xmp)!=entry['recipe_sha256']:
                raise ValueError('已选配方被改变')
            dest = jpgs/(stem+'.jpg')
            receipt = jpgs/(stem+'.json')
            key = self.cache_key(i,xmp,0)
            original_receipt = self.old_folder/'final/receipts'/(stem+'.json')
            original_jpg = self.old_folder/'final/jpg'/(stem+'.jpg')
            original_saved = json.loads(safe_read(original_receipt,1024*1024))
            original_data = safe_read(original_jpg,128*1024*1024)
            if digest(original_data) != original_saved['sha256']:
                raise ValueError('原全尺寸参考校验失败')
            with JpegImageFile(BytesIO(original_data)) as im:
                expected_size = list(im.size)
            if receipt.exists():
                saved=json.loads(safe_read(receipt,1024*1024))
                if saved['key']!=key or automatic.file_hash(dest)!=saved['sha256']:
                    raise ValueError('全尺寸缓存被改变')
            else:
                old_receipt=self.old_folder/'final/receipts'/(stem+'.json')
                reused=False
                if old_receipt.exists():
                    saved=json.loads(safe_read(old_receipt,1024*1024))
                    old_jpg=self.old_folder/'final/jpg'/(stem+'.jpg')
                    if saved['key']==key:
                        if automatic.file_hash(old_jpg)!=saved['sha256']:
                            raise ValueError('旧全尺寸文件校验失败')
                        shutil.copyfile(old_jpg,dest);reused=True
                if not reused:
                    if dest.exists():dest.rename(jpgs/(stem+'.incomplete-'+uuid.uuid4().hex+'.jpg'))
                    rawdir=self.folder/'full-raw'/stem;rawdir.mkdir(parents=True,exist_ok=True)
                    raw=rawdir/ctx['source']['raw']['name']
                    automatic.copy_raw(self.source/raw.name,raw)
                    (rawdir/(stem+'.xmp')).write_bytes(xmp)
                    if 'acr' in ctx['source']:
                        name=ctx['source']['acr']['name'];shutil.copyfile(self.source/name,rawdir/name)
                    self.renderer.render(raw,dest,0)
                data=safe_read(dest,128*1024*1024)
                joint.match(xmp,data,stem,full_size=True)
                with JpegImageFile(BytesIO(data)) as im:size=list(im.size)
                if size != expected_size:
                    raise ValueError('输出尺寸与原全尺寸参考不符')
                automatic.atomic_json(receipt,{'key':key,'sha256':digest(data),'size':size,'reused':reused})
            with JpegImageFile(dest) as im:
                if list(im.size) != expected_size:
                    raise ValueError('全尺寸缓存的图像尺寸不符')
            (xmps/(stem+'.xmp')).write_bytes(xmp)
            audit[dest.name]={'jpg':automatic.file_hash(dest),'xmp':digest(xmp)}
        self.check_source()
        automatic.atomic_json(results/'交付核验.json',{'files':audit,'review':review,'sources_unchanged':True})
        result={'verified':True,'message':'810帧分层修正与数值复核通过；待播放确认视觉效果','path':str(results),'count':self.n,
                'jpgs':str(jpgs),'xmps':str(xmps),'review':review}
        self.state.update(state='done',phase='全序列数值复核与输出完成',done=self.n,total=self.n,result=result,error=None)
        self.save();return result


def run(output,progress=None,cancelled=None,renderer_factory=PhotoshopRenderer):
    output=output_path(output)
    with automatic.render_lock(output):
        return Refinement(output,progress,cancelled,renderer_factory).execute()
