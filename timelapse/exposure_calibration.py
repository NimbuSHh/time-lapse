"""Pair ACR exposure renders with the current preview's fixed measurement.

Calibration changes the exposure conversion, never the chosen brightness target.
All source media are read-only; generated XMP files belong to a separate package.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np

from .workspace import output_path, safe_child_output
from . import analysis
from .measurement import brightness, brightness_bounds, response_bounds
from .project import digest, directory, safe_read, scan
from .provenance import audit_frame
from .xmp import parse


def _validate(project, revision):
    if getattr(project, 'written', False):
        raise ValueError('XMP 已写回或写回中断，请重新显影小图并重新分析')
    if not project.result or project.result['revision'] != revision:
        raise ValueError('当前预览已失效，请更新预览后重新生成曝光校准包')
    project.unchanged()


def apply_response(result, exposures, response):
    """Re-use measured per-frame gains only within the same source snapshot."""
    result = deepcopy(result)
    result.pop('revision', None)
    values = np.asarray(exposures, dtype=float)
    target = np.asarray(result['target'])
    bounds = np.asarray(response.get('baseline_bounds_ev',
        [[v, v] for v in response['baseline_measured']]), dtype=float)
    gains = np.asarray(response.get('gain_bounds',
        [[v, v] for v in response['gains']]), dtype=float)
    # Correct only the error shared by every supported baseline measurement.
    # The upper response bound gives the smallest defensible exposure step.
    delta = (target - np.clip(target, bounds[:, 0], bounds[:, 1])) / gains[:, 1]
    final = values + delta
    result.update(delta=delta.tolist(), final=final.tolist(), calibration_required=False,
                  blocked=np.flatnonzero((np.abs(delta) > 2) | (np.abs(final) > 5)).tolist(),
                  response=deepcopy(response))
    return result


def prepare(project, output_root, revision):
    _validate(project, revision)
    root = output_path(output_root)
    for source in (project.raw_dir, project.preview_dir):
        if root == source or (source in root.parents and not safe_child_output(root, source)):
            raise ValueError('输出目录不能位于素材目录内')
    root.mkdir(parents=True, exist_ok=True)
    root = directory(root)
    calibration_id = uuid.uuid4().hex
    staging = Path(tempfile.mkdtemp(prefix='.exposure-staging-', dir=root))
    target = root / ('exposure-' + calibration_id)
    manifest = {'schema': 1, 'kind': 'timelapse-exposure-calibration',
                'calibration_id': calibration_id, 'snapshot_id': project.id, 'revision': revision,
                'roi': project.roi, 'report': project.report, 'result': project.result, 'frames': []}
    try:
        (staging / 'baseline').mkdir()
        (staging / 'probe-exposure').mkdir()
        for i, frame in enumerate(project.frames):
            step = -.2 if project.result['brightness_delta'][i] < 0 else .2
            if not -5 <= frame.source.exposure + step <= 5:
                step = -step
            probe = frame.source.updated(frame.source.exposure + step)
            (staging / 'baseline' / frame.xmp.name).write_bytes(frame.source.data)
            (staging / 'probe-exposure' / frame.xmp.name).write_bytes(probe)
            manifest['frames'].append({'name': frame.name, 'xmp': frame.xmp.name,
                'preview_sha256': frame.jpeg_hash, 'baseline_sha256': digest(frame.source.data),
                'probe_sha256': digest(probe), 'probe_exposure': parse(probe).exposure})
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        (staging / '使用说明.txt').write_text(
            '此包仅用于曝光校准，不是最终修正。当前亮度目标和测光区域保持不变。\n'
            '1. 复制 RAW 到独立工作目录，关闭 ACR 中的序列。\n'
            '2. 应用 baseline 中的同名 XMP，重新打开 RAW，导出全序列同名、同尺寸、同裁切的 sRGB JPEG，保留全部元数据。\n'
            '3. 关闭序列，换用 probe-exposure 中的 XMP，重新打开并导出到另一个目录，保留全部元数据。\n'
            '   每帧只扰动曝光 0.2 EV，方向逐帧记录；不要批量同步设置覆盖它们。\n'
            '4. 在当前页面的曝光校准入口选择这两个小图目录，点击校准，再导出修正包。\n'
            '5. 最终 corrected 仍是候选，须在工作副本中重新显影复核。\n'
            '同一分析的目标参数调整后可继续使用此校准包；重新分析或更换素材、测光区域后需重新生成。原素材不会被此操作改写。\n', encoding='utf-8')
        project.unchanged()
        target.mkdir()
        staging.rename(target / 'package')
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if target.exists() and not any(target.iterdir()):
            target.rmdir()
        raise
    folder = target / 'package'
    project.exposure_calibrations[calibration_id] = {'path': folder, 'manifest': deepcopy(manifest),
        'manifest_sha256': digest(safe_read(folder / 'manifest.json', 64 * 1024 * 1024))}
    return {'calibration_id': calibration_id, 'path': str(folder), 'count': len(project.frames)}


def calibrate(project, revision, calibration_id, baseline_previews, exposure_previews, progress=None):
    _validate(project, revision)
    plan = project.exposure_calibrations.get(calibration_id)
    if (plan is None or plan['manifest']['snapshot_id'] != project.id
            or plan['manifest']['roi'] != project.roi or plan['manifest']['report'] != project.report):
        raise ValueError('曝光校准包与当前素材快照或冻结测光规则不对应，请重新生成校准包')
    folder = directory(plan['path'])
    if digest(safe_read(folder / 'manifest.json', 64 * 1024 * 1024)) != plan['manifest_sha256']:
        raise ValueError('曝光校准包清单已改变，请重新生成')
    baseline = scan(directory(baseline_previews), {'.jpg', '.jpeg'})
    probes = scan(directory(exposure_previews), {'.jpg', '.jpeg'})
    keys = {Path(frame.name).stem.casefold() for frame in project.frames}
    if set(baseline) != keys or set(probes) != keys:
        raise ValueError('基准与曝光校准 JPEG 必须和完整原序列同名、一一对应')
    gains, measured, receipts, baseline_bounds, gain_intervals = [], [], [], [], []
    for i, (frame, entry) in enumerate(zip(project.frames, plan['manifest']['frames'])):
        original = safe_read(folder / 'baseline' / frame.xmp.name, 4 * 1024 * 1024)
        probe = safe_read(folder / 'probe-exposure' / frame.xmp.name, 4 * 1024 * 1024)
        if digest(original) != entry['baseline_sha256'] or digest(probe) != entry['probe_sha256']:
            raise ValueError(f'{frame.name}: 校准 XMP 已改变，请使用本包生成的设置')
        key = Path(frame.name).stem.casefold()
        before = safe_read(baseline[key], 25 * 1024 * 1024)
        after = safe_read(probes[key], 25 * 1024 * 1024)
        audit = audit_frame(original, probe, current_xmp=probe, original_jpeg=before, corrected_jpeg=after)
        if audit['status'] != 'verified':
            issue = (audit['mismatches'] or audit['missing'])[0]
            raise ValueError(f"{frame.name}: 校准显影设置或帧身份核对未通过（{issue.get('field', 'metadata')}）。请使用对应 XMP 显影并保留全部元数据")
        pair = [analysis.measure_for_report(data, project.roi, project.report) for data in (before, after)]
        for m in pair:
            if m['dimensions'] != frame.measure['dimensions']:
                raise ValueError(f'{frame.name}: 校准小图尺寸与原预览不一致')
        intervals = [brightness_bounds(m, project.report) for m in pair]
        if any(bounds is None for bounds in intervals):
            raise ValueError(f'{frame.name}: 冻结参考像素失效且旧版测光规则不能给出有效区间；无法确定校准响应，请重新显影或重新分析建立测光目标')
        y0, y1 = [brightness(m, project.report) for m in pair]
        if max(abs(v - project.report['measured'][i]) for v in intervals[0]) > .04:
            raise ValueError(f'{frame.name}: 基准测光区间未能复现原预览（可能偏差超过 0.04 EV），请核对原 XMP、高光/暗部和导出设置')
        dx = entry['probe_exposure'] - frame.source.exposure
        gain = (y1 - y0) / dx
        gain_interval = response_bounds(*intervals, dx)
        if not .25 <= gain_interval[0] <= gain_interval[1] <= 4 or gain_interval[0] * abs(dx) < .04:
            raise ValueError(f'{frame.name}: 曝光校准响应区间 [{gain_interval[0]:.4f}, {gain_interval[1]:.4f}] 无法完整支持正向响应 0.25–4 或至少 0.04 EV 的测光变化；请确认校准 XMP，并检查高光、暗部和导出设置后重新显影')
        measured.append(y0)
        gains.append(float(gain))
        baseline_bounds.append(intervals[0])
        gain_intervals.append(gain_interval)
        receipts.append({'name': frame.name, 'status': audit['status'],
            'baseline_xmp_sha256': digest(original), 'probe_xmp_sha256': digest(probe),
            'baseline_preview_sha256': digest(before), 'probe_preview_sha256': digest(after),
            'baseline_measured': y0, 'probe_measured': y1, 'exposure_delta': dx,
            'baseline_bounds_ev': intervals[0], 'probe_bounds_ev': intervals[1], 'gain_bounds': gain_interval})
        if progress:
            progress(i + 1, len(project.frames), 'calibration')
    response = {'kind': 'measured-exposure', 'calibrated': True, 'calibration_id': calibration_id,
                'gain': float(np.median(gains)), 'gains': gains, 'support': len(gains),
                'baseline_measured': measured, 'baseline_bounds_ev': baseline_bounds,
                'gain_bounds': gain_intervals, 'evidence': receipts,
                'note': '同帧配对曝光的局部响应；测光区间传播至响应区间，按可确定的残差与响应上界保守换算。元数据已核对，候选仍须 ACR 重新显影复核。'}
    result = apply_response(project.result, project.exposures, response)
    result['revision'] = digest(json.dumps(result, sort_keys=True).encode())
    # Publish only after every frame and source snapshot passed. A failed attempt
    # cannot partially update gains or unlock the current export.
    _validate(project, revision)
    project.exposure_response = response
    project.result = result
    return result
