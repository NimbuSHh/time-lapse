"""Read-only adaptation of verified v1 previews for explicit native export.

Original preview state, targets, selections and recipes are never migrated or
rewritten. Export progress lives in a separate record bound to the originals.
"""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np

from . import automatic, joint_feedback as joint, refinement, tone_xmp, trajectory, trajectory_model
from .photoshop import PhotoshopRenderer
from .project import digest, safe_read
from .provenance import xmp_evidence
from .workspace import output_path

STATE_FILE = 'approved-export-state.json'
KIND = 'legacy-preview-approved-export-v1'
_FROZEN_STATE = ('id', 'path', 'version', 'source_run', 'target_file_sha256',
                 'target_sha256', 'measurement_sha256', 'effective_policy_sha256')
_FROZEN_RESULT = ('video', 'sha256', 'manifest', 'manifest_sha256', 'selection_sha256',
                  'target_sha256', 'effective_policy_sha256', 'effective_tone_policy',
                  'numeric_conditions_met')


def _json(path, limit=32*1024*1024):
    return json.loads(safe_read(path, limit))


def _records(output):
    output = output_path(output)
    original_state = trajectory.load_latest(output)
    if original_state is None:
        return None
    if original_state.get('version') != trajectory.VERSION:
        raise ValueError('该记录不是旧版完整预览')
    original_run = automatic.load_latest(output)
    if (not original_run or original_run.get('state') != 'done'
            or original_state.get('source_run') != original_run.get('id')):
        raise ValueError('旧预览缺少相符的完整原自动任务，不能推断来源')
    if any(key not in original_run for key in ('raw_dir', 'snapshot', 'renderer', 'phases')):
        raise ValueError('原自动任务缺少素材快照、显影器或准备记录')
    folder = Path(original_state['path']).resolve()
    old_folder = Path(original_run['path']).resolve()
    if old_folder not in folder.parents:
        raise ValueError('旧预览不属于原自动任务目录')
    if any(not original_state.get(k) for k in _FROZEN_STATE):
        raise ValueError('旧预览冻结记录不完整，不能推断缺失字段')
    result = original_state.get('result')
    if not isinstance(result, dict) or any(k not in result for k in _FROZEN_RESULT):
        raise ValueError('旧预览缺少视频、配方或目标校验记录')
    if result.get('numeric_conditions_met') is not True:
        raise ValueError('旧预览数值检查未通过，不能直接导出')
    if result.get('run_id') not in (None, original_state['id']):
        raise ValueError('旧预览任务身份不一致')
    state_sha = automatic.file_hash(folder/'state.json')
    original_sha = automatic.file_hash(old_folder/'run.json')
    state = deepcopy(original_state)
    state['result']['run_id'] = state['id']
    state.setdefault('raw_dir', original_run['raw_dir'])
    record = folder/STATE_FILE
    if record.is_symlink():
        raise ValueError('独立导出记录不能使用符号链接')
    if record.exists():
        saved = _json(record)
        if (saved.get('kind') != KIND or saved.get('legacy_state_sha256') != state_sha
                or saved.get('original_run_sha256') != original_sha):
            raise ValueError('独立导出记录与旧预览不符，原记录或来源已改变')
        overlay = saved.get('state', {})
        if (any(overlay.get(k) != state.get(k) for k in _FROZEN_STATE)
                or any(overlay.get('result', {}).get(k) != state['result'].get(k) for k in _FROZEN_RESULT)
                or overlay.get('result', {}).get('run_id') != state['id']):
            raise ValueError('独立导出记录改变了旧预览的冻结版本')
        state = deepcopy(overlay)
    return state, original_run, state_sha, original_sha


def load(output):
    """Return display state with a run id; never write or connect to Adobe."""
    try:
        records = _records(output)
        if not records:
            return None
        state = records[0]
        state['result']['export_available'] = True
        return state
    except (ValueError, OSError, KeyError, TypeError) as exc:
        state = deepcopy(trajectory.load_latest(output))
        if state is None:
            return None
        result = state.setdefault('result', {})
        result.update(export_available=False, export_block_reason=str(exc))
        return state


class LegacyRunner(trajectory.Trajectory):
    """Use legacy actual measurements while keeping all old artifacts immutable."""
    legacy_compatible = True
    product = False
    primary_region = 'sky'

    def __init__(self, output, progress=None, cancelled=None, renderer_factory=PhotoshopRenderer):
        self.output = output_path(output)
        self.progress = progress
        self.cancelled = cancelled or (lambda: False)
        records = _records(self.output)
        if records is None:
            raise ValueError('没有可确认的旧版完整预览')
        self.state, self.original, self.legacy_state_sha256, self.original_run_sha256 = records
        self.folder = Path(self.state['path']).resolve()
        self.old_folder = Path(self.original['path']).resolve()
        self.source = Path(self.original['raw_dir'])
        self.check_source()
        try:
            phase = self.original['phases']['第 1 轮准备校准']
            self.base_folder = Path(phase['result']['path']).resolve()
            if self.old_folder not in self.base_folder.parents:
                raise ValueError('首次参考路径越界')
            if automatic.file_hash(self.base_folder/'manifest.json') != phase['sha256']:
                raise ValueError('首次参考清单已改变')
            _, self.base = joint.manifest(self.base_folder, {}, 'joint-feedback-calibration')
            data = safe_read(self.folder/'trajectory-target.json', 16*1024*1024)
            if digest(data) != self.state['target_file_sha256']:
                raise ValueError('旧预览冻结目标已改变')
            self.plan = json.loads(data)
            if (self.plan.get('version') != trajectory_model.LEGACY_VERSION
                    or self.plan.get('plan_sha256') != self.state['target_sha256']
                    or self.plan.get('plan_sha256') != trajectory_model._hash(self.plan)):
                raise ValueError('旧预览目标版本或内容指纹不符')
            self.n = len(self.base['frames'])
            if self.n != len(self.original['snapshot']) or self.n != self.plan['frame_count']:
                raise ValueError('旧预览帧数与原素材不符')
            policy_data = safe_read(self.folder/'effective-tone-policy.json', 1024*1024)
            if digest(policy_data) != self.state['effective_policy_sha256']:
                raise ValueError('旧预览有效调色目标已改变')
            policy = json.loads(policy_data)
            if (policy != self.state['result']['effective_tone_policy']
                    or policy.get('target_sha256') != self.plan['plan_sha256']
                    or not np.isfinite(float(policy['sky_offset_ev']))):
                raise ValueError('旧预览有效调色目标不完整或不符')
            selection_data = safe_read(self.folder/'selection.json', 16*1024*1024)
            if digest(selection_data) != self.state['result']['selection_sha256']:
                raise ValueError('旧预览已选帧发生改变')
            self.saved_selection = json.loads(selection_data)
            if set(self.saved_selection) != {str(i) for i in range(self.n)}:
                raise ValueError('旧预览已选帧不完整')
        except (KeyError, TypeError) as exc:
            raise ValueError('旧预览或原自动任务缺少必要校验字段') from exc
        self.region_contract = self.plan['regional_measurement_contract']
        # The old method used these exact coordinates. Do not substitute new
        # automatic regions and thereby change the meaning of its measurements.
        expected = {k:list(v) for k,v in trajectory.REGIONS.items()}
        if (self.region_contract.get('rois') != expected
                or self.region_contract.get('per_frame_pixel_reselection') is not False):
            raise ValueError('旧预览测光定义与原算法不符')
        self.renderer = renderer_factory(self.folder, self.cancelled)
        compatible = getattr(self.renderer, 'compatible_identities', ())
        if (self.renderer.identity != self.original['renderer']
                and self.original['renderer'] not in compatible):
            raise ValueError('显影器与旧预览不兼容，不能把算法变更当作尺寸回执更新')
        self.target = np.zeros((self.n, 6))
        self.target[:,0] = self.plan['target_mid']
        self.frames, self.reference_clips, self.histories, self.chosen = {}, {}, {}, {}
        self.retry_indices = set()
        self.history_roots = []
        for folder in sorted(self.old_folder.glob('分层修正-*')):
            old = _json(folder/'state.json', 8*1024*1024)
            if (old.get('source_run') == self.original['id']
                    and old.get('measurement_sha256') == self.base['measurement_sha256']):
                if self.old_folder not in folder.resolve().parents:
                    raise ValueError('旧实测缓存路径越界')
                self.history_roots.append(folder)
        self.history_roots.append(self.folder)
        self.state.setdefault('reused_renders', 0)
        self.state.setdefault('new_renders', 0)

    def history(self, i):
        # Only the selected render is needed. Re-reading hundreds of rejected
        # trials per frame would make confirming a long preview impractical.
        if i in self.histories:
            return self.histories[i]
        context = self.context(i)
        chosen = self.saved_selection[str(i)]
        stem = Path(context['source']['raw']['name']).stem
        jpg_path, xmp_path = Path(chosen['jpg']), Path(chosen['xmp'])
        for path in (jpg_path, xmp_path):
            if not path.is_absolute() or path.is_symlink() or self.old_folder not in path.resolve().parents:
                raise ValueError('旧预览所选实测路径越界')
        jpg_path, xmp_path = jpg_path.resolve(), xmp_path.resolve()
        xmp = safe_read(xmp_path, 4*1024*1024)
        jpg = safe_read(jpg_path, 25*1024*1024)
        if jpg_path.name == 'preview.jpg':
            trial = jpg_path.parent
            if (xmp_path != trial/'recipe.xmp' or trial.parent.name != stem
                    or trial.parent.parent.name != 'frames'
                    or trial.parent.parent.parent not in self.history_roots):
                raise ValueError('旧预览试验目录或帧名不符')
            receipt = _json(trial/'receipt.json', 1024*1024)
            if (Path(receipt['jpg']).resolve() != jpg_path or Path(receipt['xmp']).resolve() != xmp_path
                    or receipt.get('recipe_sha256') != digest(xmp)
                    or digest(refinement.recipe(context['base'], receipt['controls'])) != digest(xmp)):
                raise ValueError('旧预览所选实测配方或路径不符')
            controls = receipt['controls']
        else:
            group = jpg_path.parent.parent
            allowed = [self.old_folder/'initial']
            allowed += [self.old_folder/f'round-{n}'/name for n in (1,2)
                        for name in ('baseline','probe-exposure','probe-highlights','probe-whites','corrected')]
            if (group not in allowed or jpg_path != group/'jpg'/(stem+'.jpg')
                    or xmp_path != group/'raw'/(stem+'.xmp')):
                raise ValueError('旧参考显影目录或帧名不符')
            receipt = _json(group/'receipts'/(stem+'.json'), 1024*1024)
            controls = tone_xmp.values(xmp)+[255.]
            if xmp_evidence(refinement.recipe(context['base'], controls))['crs'] != xmp_evidence(xmp)['crs']:
                raise ValueError('旧参考显影改变了非授权配方字段')
        if (receipt['key'] != self.cache_key(i, xmp) or receipt['sha256'] != digest(jpg)
                or chosen['sha256'] != digest(jpg) or chosen['recipe_sha256'] != digest(xmp)):
            raise ValueError('旧预览所选实测来源、图像或配方校验失败')
        joint.match(xmp, jpg, stem)
        entry = {**self.measure(i,jpg), 'controls':controls, 'jpg':str(jpg_path),
                 'xmp':str(xmp_path), 'sha256':digest(jpg), 'recipe_sha256':digest(xmp), 'reused':True}
        self.histories[i] = [entry]
        self.state['reused_renders'] += 1
        return self.histories[i]

    def cache_key(self, i, xmp, edge=1600):
        identity = self.original['renderer'] if edge == 1600 else self.renderer.identity
        return digest(json.dumps([self.context(i)['source'], digest(xmp), edge, identity], sort_keys=True).encode())

    def save(self):
        if (automatic.file_hash(self.folder/'state.json') != self.legacy_state_sha256
                or automatic.file_hash(self.old_folder/'run.json') != self.original_run_sha256):
            raise ValueError('旧预览或原自动任务在导出期间改变，已停止')
        record = self.folder/STATE_FILE
        if record.is_symlink():
            raise ValueError('独立导出记录不能使用符号链接')
        if record.exists():
            saved = _json(record)
            if (saved.get('kind') != KIND or saved.get('legacy_state_sha256') != self.legacy_state_sha256
                    or saved.get('original_run_sha256') != self.original_run_sha256):
                raise ValueError('已有独立导出记录不属于此预览，不能覆盖')
        automatic.atomic_json(record, {'kind':KIND, 'legacy_state_sha256':self.legacy_state_sha256,
                                      'original_run_sha256':self.original_run_sha256, 'state':self.state})

    def render(self, *args, **kwargs):
        raise ValueError('旧预览兼容流程不能重做预览或改写已确认配方')

    def save_selection(self):
        raise ValueError('旧预览兼容流程不能改写已选帧')

    def execute(self):
        return self.state['result']

    def review_entries(self, observations):
        review = trajectory_model.review_sequence(self.plan, observations)
        data = safe_read(self.folder/'effective-tone-policy.json', 1024*1024)
        if digest(data) != self.state['effective_policy_sha256']:
            raise ValueError('旧预览有效调色目标已改变')
        policy = json.loads(data)
        mid = np.array([e['brightness'][0] for e in observations])-self.target[:,0]
        sky = np.array([e['regions']['sky'] for e in observations])-np.array(self.plan['regional_targets']['sky'])-policy['sky_offset_ev']
        review['strict_failed_frames'] = [i+1 for i in range(self.n)
                                          if abs(mid[i]) > .012+1e-9 or abs(sky[i]) > .02+1e-9]
        review['primary_offset_adjusted_max_error_ev'] = float(max(abs(sky)))
        review['highlight_protection_passed'] = all(self.protected(i,e) for i,e in enumerate(observations))
        review['numeric_conditions_met'] = (review['numeric_conditions_met'] and not review['strict_failed_frames']
                                             and review['highlight_protection_passed'])
        review['effective_tone_policy'] = policy
        return review

    def export(self, confirmation):
        from .trajectory_export import export
        return export(self, confirmation)
