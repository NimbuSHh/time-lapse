"""Frozen brightness goals and export-only feedback from paired ACR renders.

The proposal is a bounded secant step, never an ACR-rendered result. No routine
in this module writes to the source, RAW, XMP or preview directories.
"""
import argparse
import base64
from collections import Counter
import csv
import html
import json
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np

from .workspace import output_path, safe_child_output
from . import analysis
from .project import directory, digest, safe_read, scan, RAW, MAX_FRAMES
from .provenance import audit_frame
from .xmp import parse


def target_hash(target):
    return digest(json.dumps(list(target), separators=(',', ':'), allow_nan=False).encode())


def propose(reference_brightness, actual_brightness, reference_exposure, current_exposure,
            target, baseline_exposure=None, damping=.8, max_step=.75, boundaries=(),
            reference_bounds=None, actual_bounds=None):
    """Use measured same-frame secants; nearby evidence only for tiny probes.

    A secant is not a derivative or a proven new render. Damping, a trust radius
    and bracket protection bound each experiment. Cumulative limits are always
    checked relative to the immutable original exposures, not the last round.
    """
    arrays = [np.asarray(a, dtype=float) for a in
              (reference_brightness, actual_brightness, reference_exposure, current_exposure, target)]
    if any(a.ndim != 1 for a in arrays) or len({a.size for a in arrays}) != 1 or not 1 <= arrays[0].size <= MAX_FRAMES:
        raise ValueError('显影配对数组必须是一一对应的非空序列')
    if not all(np.all(np.isfinite(a)) for a in arrays):
        raise ValueError('显影配对不能包含非有限值')
    y0, actual, e0, current, goal = arrays
    baseline = e0 if baseline_exposure is None else np.asarray(baseline_exposure, dtype=float)
    if baseline.shape != e0.shape or not np.all(np.isfinite(baseline)):
        raise ValueError('原始曝光基线必须逐帧对应且有限')
    if not np.isfinite(damping) or not 0 < damping <= 1 or not np.isfinite(max_step) or not 0 < max_step <= 1:
        raise ValueError('反馈强度须为 0–1，单轮步长须为 0–1 EV')
    n = len(goal)
    def intervals(values, provided):
        if provided is None:
            return np.column_stack((values, values))
        if len(provided) != n:
            raise ValueError('测光区间必须逐帧对应')
        result = np.full((n, 2), np.nan)
        for i, bounds in enumerate(provided):
            if bounds is None:
                continue
            bounds = np.asarray(bounds, dtype=float)
            if (bounds.shape != (2,) or not np.all(np.isfinite(bounds))
                    or not bounds[0] <= values[i] <= bounds[1]):
                raise ValueError('测光区间必须有限、有序且包含观测值')
            result[i] = bounds
        return result
    reference_interval = intervals(y0, reference_bounds)
    actual_interval = intervals(actual, actual_bounds)
    known = np.all(np.isfinite(reference_interval), axis=1) & np.all(np.isfinite(actual_interval), axis=1)
    if any(not isinstance(b, (int, np.integer)) or not 0 < b < n for b in boundaries):
        raise ValueError('反馈分段边界无效')
    cuts = [0] + sorted(set(boundaries)) + [n]
    dx, dy = current - e0, actual - y0
    ratios = np.full(n, np.nan)
    tested = np.abs(dx) >= .15
    np.divide(dy, dx, out=ratios, where=tested)
    ratio_intervals = np.full((n, 2), np.nan)
    from .measurement import response_bounds
    for i in np.flatnonzero(tested & known):
        ratio_intervals[i] = response_bounds(reference_interval[i], actual_interval[i], dx[i])
    reliable = (tested & known & (ratio_intervals[:, 0] * np.abs(dx) >= .08)
                & (ratio_intervals[:, 0] >= .25) & (ratio_intervals[:, 1] <= 4))
    gains, gain_bounds, statuses, limited, blocked = [], [], [], [], []
    final = current.copy()
    prediction = actual.copy()
    prediction_bounds = actual_interval.copy()
    for i in range(n):
        error = float(goal[i] - np.clip(goal[i], *actual_interval[i])) if known[i] else 0.
        gain, gain_interval, status = None, None, 'unsupported'
        if not known[i]:
            status = 'measurement-unknown'
            blocked.append(i)
        elif actual_interval[i, 0] < actual_interval[i, 1] and error == 0:
            status = 'within-measurement-interval'
        elif abs(error) <= .02:
            status = 'within-noise'
        elif tested[i] and not reliable[i]:
            status = 'inconsistent-response'
            blocked.append(i)
        elif reliable[i]:
            gain, status = float(ratios[i]), 'same-frame-secant'
            gain_interval = ratio_intervals[i].tolist()
        else:
            left = max(b for b in cuts if b <= i)
            right = min(b for b in cuts if b > i)
            lo, hi = max(left, i - 30), min(right, i + 31)
            neighbors = ratios[lo:hi][reliable[lo:hi]]
            neighbor_intervals = ratio_intervals[lo:hi][reliable[lo:hi]]
            if len(neighbors) >= 3:
                median = float(np.median(neighbors))
                median_interval = np.median(neighbor_intervals, axis=0)
                # Do not silently extrapolate a mixture of incompatible recipes.
                deviations = np.max(np.abs(neighbor_intervals - median), axis=1)
                if float(np.quantile(deviations, .9)) <= .25 * median_interval[0]:
                    gain, status = median, 'local-secant'
                    gain_interval = median_interval.tolist()
            if gain is None:
                blocked.append(i)
        if gain is not None:
            requested = damping * error / gain_interval[1]
            step = float(np.clip(requested, -max_step, max_step))
            if step != requested:
                limited.append(i)
            value = current[i] + step
            # When measured renders bracket the target, keep the next exposure
            # inside that measured interval; do not extrapolate unnecessarily.
            bracketed = (reference_interval[i, 1] <= goal[i] <= actual_interval[i, 0]
                         or actual_interval[i, 1] <= goal[i] <= reference_interval[i, 0])
            if bracketed:
                value = float(np.clip(value, min(e0[i], current[i]), max(e0[i], current[i])))
            low, high = current[i] - max_step, current[i] + max_step
            if bracketed:
                low, high = max(low, min(e0[i], current[i])), min(high, max(e0[i], current[i]))
            # With uncertain measurements, decimal rounding may not cross the
            # smallest supported residual or reverse an intended tiny step.
            if actual_interval[i, 0] < actual_interval[i, 1] or gain_interval[0] < gain_interval[1]:
                endpoint = current[i] + error / gain_interval[1]
                low, high = max(low, min(current[i], endpoint)), min(high, max(current[i], endpoint))
            # Quantize inside, rather than clamp after quantizing to a value
            # with unsupported decimal precision (e.g. a .247 EV step).
            low, high = np.ceil(low * 100 - 1e-9) / 100, np.floor(high * 100 + 1e-9) / 100
            value = float(np.clip(np.round(value, 2), low, high)) if low <= high else current[i]
            final[i] = value
            prediction[i] = actual[i] + gain * (value - current[i])
            shift = sorted(g * (value - current[i]) for g in gain_interval)
            prediction_bounds[i] = actual_interval[i] + shift
        gains.append(gain)
        gain_bounds.append(gain_interval)
        statuses.append(status)
    requested_final = final.copy()
    unsafe = np.flatnonzero((np.abs(final - baseline) > 2 + 1e-9) | (np.abs(final) > 5))
    blocked = sorted(set(blocked) | set(unsafe))
    for i in unsafe:
        final[i], prediction[i], statuses[i] = current[i], actual[i], 'exposure-limit'
        prediction_bounds[i] = actual_interval[i]
    return {'target': goal.tolist(), 'target_sha256': target_hash(goal.tolist()),
            'final': final.tolist(), 'requested_final': requested_final.tolist(), 'delta': (final - current).tolist(),
            'baseline_delta': (final - baseline).tolist(), 'gain': gains, 'gain_bounds': gain_bounds,
            'prediction_bounds_ev': [row.tolist() if np.all(np.isfinite(row)) else None for row in prediction_bounds],
            'reference_bounds_ev': [row.tolist() if np.all(np.isfinite(row)) else None for row in reference_interval],
            'actual_bounds_ev': [row.tolist() if np.all(np.isfinite(row)) else None for row in actual_interval],
            'prediction': prediction.tolist(), 'statuses': statuses, 'limited': limited,
            'blocked': blocked, 'damping': damping, 'max_step': max_step,
            'method': 'bounded-paired-render-secant-v1', 'rendered': False,
            'note': '候选曝光来自同帧前后显影配对；预测曲线不是 ACR 复核结果，须在工作副本重显影验证。'}


def _name(value, extension=None):
    if not isinstance(value, str) or Path(value).name != value or value in ('', '.', '..') or '\\' in value:
        raise ValueError('来源清单包含不安全文件名')
    if extension and Path(value).suffix.lower() not in extension:
        raise ValueError('来源文件类型不符')
    return value


def _load_source(source):
    source = Path(source).expanduser().absolute()
    analysis_source = source.is_file()
    if analysis_source:
        directory(source.parent)
        mdata = safe_read(source, 128 * 1024 * 1024)
        saved = json.loads(mdata)
        v = saved['target']
        if saved['sha256'] != digest(json.dumps(v, sort_keys=True, allow_nan=False).encode()):
            raise ValueError('保存的分析目标校验失败')
        v = {**v, 'package_id': v['snapshot_id'], 'kind': 'timelapse-analysis'}
        frames = [{**f, 'xmp': f.get('xmp', Path(f['name']).stem + '.xmp'),
                   'baseline_xmp_sha256': f.get('source_xmp_sha256')} for f in v['frames']]
        baseline_folder, vdata = Path(v['raw_dir']), mdata
    else:
        source = directory(source)
        mdata = safe_read(source / 'manifest.json', 64 * 1024 * 1024)
        m = json.loads(mdata)
    if not analysis_source and (source / 'verification.json').exists():
        # Durable writeback bundle from the original UI, including schema 1.
        vdata = safe_read(source / 'verification.json', 64 * 1024 * 1024)
        v = json.loads(vdata)
        if m.get('state') != 'applied' or m.get('package_id') != v.get('package_id'):
            raise ValueError('写回记录与冻结复核目标不一致或写回尚未完成')
        bm = {_name(f['xmp'], {'.xmp'}).casefold(): f for f in m['frames']}
        if len(bm) != len(m['frames']):
            raise ValueError('写回清单存在重复文件')
        frames = []
        for f in v['frames']:
            name = _name(f['name'], RAW)
            key = (Path(name).stem + '.xmp').casefold()
            entry = bm.get(key)
            if entry is None:
                raise ValueError('写回清单与冻结目标未逐帧对应')
            frames.append({**f, 'xmp': entry['xmp'], 'baseline_xmp_sha256': entry['original'],
                           'corrected_xmp_sha256': entry['corrected']})
        if len(frames) != len(bm):
            raise ValueError('写回清单与冻结目标长度不同')
        baseline_folder = source / 'originals'
    elif not analysis_source:
        v, vdata = m, mdata
        frames = [dict(f) for f in v['frames']]
        baseline_folder = source / ('baseline' if v.get('kind') == 'timelapse-feedback' else 'originals')
        for f in frames:
            f['baseline_xmp_sha256'] = f.get('baseline_xmp_sha256', f.get('source_xmp_sha256'))
    n = len(frames)
    if not 5 <= n <= MAX_FRAMES or len({Path(_name(f['name'], RAW)).stem.casefold() for f in frames}) != n:
        raise ValueError('冻结目标帧清单无效或重复')
    for key, value in (('target', v['result']['target']), ('exposure', v['result']['final']), ('measured', v['report']['measured'])):
        if len(value) != n or not np.all(np.isfinite(value)):
            raise ValueError('冻结目标或曝光数据无效: ' + key)
    for f in frames:
        _name(f['xmp'], {'.xmp'})
        if Path(f['xmp']).stem.casefold() != Path(f['name']).stem.casefold():
            raise ValueError('XMP 与帧名不对应')
    target_id = target_hash(v['result']['target'])
    if v.get('target_sha256', target_id) != target_id:
        raise ValueError('冻结亮度目标的哈希不匹配')
    return source, v, frames, baseline_folder, {'manifest_sha256': digest(mdata),
            'verification_sha256': digest(vdata), 'package_id': v['package_id'], 'target_sha256': target_id,
            'analysis_source': analysis_source}


def build(source, original_previews, current_xmps, previews, output_root, damping=.8, max_step=.75, progress=None):
    """Audit every input and publish an independent review bundle, never apply.

    An incomplete audit publishes evidence only, without candidate XMP files.
    Accepts the original backup/export or a previous feedback package so later
    rounds retain the same goal, baseline and event/component definitions.
    """
    source, frozen, frames, baseline_folder, receipt = _load_source(source)
    original_previews, current_xmps, previews = map(directory, (original_previews, current_xmps, previews))
    root = output_path(output_root)
    protected = [source, original_previews, current_xmps, previews, Path(frozen['raw_dir']).resolve()]
    if any(root == p or (p in root.parents and not safe_child_output(root, p)) for p in protected):
        raise ValueError('反馈输出不能位于素材、显影或来源包目录内')
    originals = scan(original_previews, {'.jpg', '.jpeg'})
    renders = scan(previews, {'.jpg', '.jpeg'})
    xmps = scan(current_xmps, {'.xmp'})
    keys = {Path(f['name']).stem.casefold() for f in frames}
    if any(set(items) != keys for items in (originals, renders, xmps)):
        raise ValueError('原图、复核图与当前 XMP 必须和冻结清单按同名一一对应')
    checks, observations, inputs, baseline_bytes, current_bytes = [], [], [], [], []
    reference_bounds, actual_bounds = [], []
    y0, actual, original_e, current_e = [], [], [], []
    common, roi = frozen['report']['common_tiles'], frozen['roi']
    minimum_cells = 1 if 'measurement_contract' in frozen['report'] else 8
    if len(common) < minimum_cells or len(set(common)) != len(common) or any(not isinstance(i, int) or not 0 <= i < 64 for i in common):
        raise ValueError('冻结参考区域网格无效')
    if len(roi) != 4 or not all(np.isfinite(roi)) or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
        raise ValueError('冻结参考区域无效')
    for i, f in enumerate(frames):
        key = Path(f['name']).stem.casefold()
        paths = {'current_xmp': xmps[key], 'original_jpeg': originals[key], 'corrected_jpeg': renders[key]}
        checkpoint = frozen.get('checkpoint') if receipt['analysis_source'] else None
        if not checkpoint:
            paths['original_xmp'] = baseline_folder / f['xmp']
        if not receipt['analysis_source']:
            paths['corrected_xmp'] = source / 'corrected' / f['xmp']
        data = {k: safe_read(p, (25 if k.endswith('jpeg') else 4) * 1024 * 1024) for k, p in paths.items()}
        if checkpoint:
            if checkpoint.get('version') != 1 or len(checkpoint['frames']) != len(frames):
                raise ValueError('保存的分析来源记录不完整')
            data['original_xmp'] = base64.b64decode(checkpoint['frames'][i]['xmp'], validate=True)
        if receipt['analysis_source']:
            data['corrected_xmp'] = data['current_xmp']
        expected = {'original_jpeg': f['preview_sha256']}
        if f.get('baseline_xmp_sha256'):
            expected['original_xmp'] = f['baseline_xmp_sha256']
        if not receipt['analysis_source']:
            expected['corrected_xmp'] = f['corrected_xmp_sha256']
        check = audit_frame(**data, expected_sha256=expected)
        parsed = {k: parse(data[k]) for k in ('original_xmp', 'corrected_xmp', 'current_xmp')}
        if receipt['analysis_source']:
            original_value = frozen['public']['frames'][i]['exposure']
            if abs(parsed['original_xmp'].exposure - original_value) > 1e-5:
                check['mismatches'].append({'check': 'original_exposure', 'expected': original_value,
                                             'actual': parsed['original_xmp'].exposure})
        elif abs(parsed['corrected_xmp'].exposure - frozen['result']['final'][i]) > 1e-5:
            check['mismatches'].append({'check': 'frozen_exposure', 'expected': frozen['result']['final'][i], 'actual': parsed['corrected_xmp'].exposure})
        # All source files are hashed again immediately before publishing.
        inputs.extend((str(path), digest(data[k])) for k, path in paths.items())
        from .measurement import brightness as measured_brightness, brightness_bounds
        measurements = [analysis.measure_for_report(data[k], roi, frozen['report']) for k in ('original_jpeg', 'corrected_jpeg')]
        brightness = [measured_brightness(m, frozen['report']) for m in measurements]
        for label, m in zip(('original', 'actual'), measurements):
            if list(m['dimensions']) != frozen['dimensions']:
                check['mismatches'].append({'check': 'measurement_dimensions', 'render': label})
        bounds = [brightness_bounds(m, frozen['report']) for m in measurements]
        reference_bounds.append(bounds[0]); actual_bounds.append(bounds[1])
        if abs(brightness[0] - frozen['report']['measured'][i]) > 1e-9:
            check['mismatches'].append({'check': 'original_measurement', 'expected': frozen['report']['measured'][i], 'actual': brightness[0]})
        status = 'mismatch' if check['mismatches'] else 'incomplete' if check['missing'] else 'verified'
        checks.append({'frame': i + 1, 'name': f['name'], 'status': status, 'mismatches': check['mismatches'],
                       'missing': check['missing'], 'verified_checks': len(check['verified']),
                       'sha256': {k: digest(d) for k, d in data.items()},
                       'embedded_exposure': {k: check['evidence'].get(k, {}).get('exposure') for k in ('original_jpeg', 'corrected_jpeg')},
                       'ignored_fields': {k: check['evidence'].get(k, {}).get('ignored_fields', {}) for k in data}})
        y0.append(brightness[0]); actual.append(brightness[1])
        original_e.append(parsed['original_xmp'].exposure); current_e.append(parsed['current_xmp'].exposure)
        baseline_bytes.append(data['original_xmp']); current_bytes.append(data['current_xmp'])
        observations.append({'frame': i + 1, 'reference_brightness': brightness[0], 'actual_brightness': brightness[1],
                             'reference_exposure': original_e[-1], 'current_exposure': current_e[-1],
                             'reference_bounds_ev': bounds[0], 'actual_bounds_ev': bounds[1],
                             'roi_clipped_fraction': measurements[1]['clipped']})
        if progress:
            progress(i + 1, len(frames))
    counts = dict(Counter(c['status'] for c in checks))
    audit = {'scope': 'recorded-settings-identities-hashes-and-measurements', 'source': str(source), 'receipt': receipt,
             'counts': counts, 'frames': checks,
             'note': '来源核对覆盖文件哈希、记录的显影设置与拍摄身份；元数据不能单独鉴证像素的生成过程。'}
    result = None
    if counts.get('verified') == len(frames):
        # Gaps and original protected boundaries bound fallback evidence. Direct
        # same-frame feedback never needs to rediscover dense camera events.
        result = propose(y0, actual, original_e, current_e, frozen['result']['target'], original_e,
                         damping=damping, max_step=max_step, boundaries=frozen['report']['boundaries'],
                         reference_bounds=reference_bounds, actual_bounds=actual_bounds)
        if result['target_sha256'] != receipt['target_sha256']:
            raise ValueError('反馈不得改变冻结亮度目标')
    changed = [] if result is None else [i for i, delta in enumerate(result['delta']) if abs(delta) > 1e-9]
    ready = bool(changed)
    issues = _issues(checks, result)
    held = len(frames) - len(changed)
    partial = ready and bool(result['blocked'] or any(s == 'within-measurement-interval' for s in result['statuses']))
    actual_array = np.asarray(actual)
    error = actual_array - frozen['result']['target']
    current_metrics = {'rmse_ev': float(np.sqrt(np.mean(error ** 2))), 'max_abs_error_ev': float(max(abs(error))),
                       'events': [analysis.event_residual(actual, e, frozen['report']['boundaries'])
                                  for e in frozen['report']['events'] if e['index'] in frozen['result']['settings']['selected']]}
    root.mkdir(parents=True, exist_ok=True)
    package_id = uuid.uuid4().hex
    staging = Path(tempfile.mkdtemp(prefix='.feedback-', dir=root))
    destination = root / ('feedback-' + package_id)
    try:
        def write_json(name, value):
            (staging / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        write_json('source-audit.json', audit)
        write_json('observations.json', observations)
        write_json('current-verification.json', current_metrics)
        write_json('frozen-source.json', frozen)
        manifest = {**{k: v for k, v in frozen.items() if k != 'checkpoint'},
                    'schema': 1, 'kind': 'timelapse-feedback', 'package_id': package_id,
                    'source_receipt': receipt, 'target_sha256': receipt['target_sha256'],
                    'source_preview_dir': str(original_previews), 'current_xmp_dir': str(current_xmps),
                    'feedback_preview_dir': str(previews), 'round': frozen.get('round', 0) + 1,
                    'ready': ready, 'partial': partial, 'changed_count': len(changed), 'held_count': held,
                    'issues': issues, 'acr_verified': False, 'frames': []}
        if result is not None:
            result['settings'] = frozen['result']['settings']
            manifest['result'] = result
            write_json('proposal.json', result)
        if ready:
            for folder in ('baseline', 'originals', 'corrected', 'sample-corrected'):
                (staging / folder).mkdir()
            samples = {0, len(frames) - 1, int(np.argmin(y0)), int(np.argmax(y0)),
                       int(np.argmax(np.abs(result['delta'])))}
            for e in frozen['report']['events']:
                if e['index'] in frozen['result']['settings']['selected']:
                    # Keep the full evidence window, so a representative render
                    # can measure the step rather than only eyeball two frames.
                    a, b = e.get('window', [max(0, e['index'] - 8), min(len(frames), e['index'] + 8)])
                    samples.update(range(a, b))
            for i, f in enumerate(frames):
                candidate = (parse(current_bytes[i]).updated(result['final'][i])
                             if i in changed else current_bytes[i])
                for folder, data in (('baseline', baseline_bytes[i]), ('originals', current_bytes[i]), ('corrected', candidate)):
                    (staging / folder / f['xmp']).write_bytes(data)
                if i in samples:
                    (staging / 'sample-corrected' / f['xmp']).write_bytes(candidate)
                manifest['frames'].append({**f, 'baseline_exposure': original_e[i], 'original_exposure': current_e[i],
                    'final_exposure': result['final'][i], 'source_xmp_sha256': digest(current_bytes[i]),
                    'baseline_xmp_sha256': digest(baseline_bytes[i]), 'corrected_xmp_sha256': digest(candidate)})
            manifest['sample_frames'] = [i + 1 for i in sorted(samples)]
            write_json('sample-plan.json', [{'frame': i + 1, 'name': frames[i]['name'], 'xmp_exposure': result['final'][i],
                                             'target_brightness': result['target'][i]} for i in sorted(samples)])
        else:
            manifest['frames'] = frames
        write_json('manifest.json', manifest)
        with (staging / 'curves.csv').open('w', newline='', encoding='utf-8-sig') as out:
            writer = csv.writer(out)
            writer.writerow(['frame', 'name', 'original_brightness_ev', 'frozen_target_ev', 'actual_brightness_ev',
                             'current_xmp_ev', 'candidate_xmp_ev', 'increment_xmp_ev', 'total_from_baseline_ev', 'response_gain', 'status'])
            for i, f in enumerate(frames):
                writer.writerow([i + 1, f['name'], y0[i], frozen['result']['target'][i], actual[i], current_e[i]] +
                                ([result['final'][i], result['delta'][i], result['baseline_delta'][i], result['gain'][i], result['statuses'][i]] if result else [''] * 5))
        _report(staging, manifest, audit, current_metrics, y0, actual, result)
        (staging / '使用说明.txt').write_text(
            '本包仅导出，没有覆盖原素材。亮度目标与原备份完全相同。\n'
            'corrected 为候选绝对曝光 XMP；不要把它作为增量叠加。originals 为本轮输入 XMP，baseline 为首次修正前 XMP。\n'
            '先将 sample-plan.json 中的 RAW 复制到独立工作目录，配对 sample-corrected 中的 XMP 后在 ACR 导出 sRGB 小图。\n'
            '完成代表帧检查后再在完整工作副本验证全序列，不得仅凭预测曲线验收。\n'
            '下一轮使用本包作为来源包、原始小图作为原始参考、工作副本 XMP 和新显影图做残差反馈；不要重新设计目标。\n'
            '来源缺失或设置冲突时只有诊断；个别帧测量或响应不足时保留其当前 XMP，详见逐帧问题清单。\n', encoding='utf-8')
        for path, expected in inputs:
            if digest(safe_read(Path(path), 25 * 1024 * 1024)) != expected:
                raise ValueError('核对期间源文件发生变化，停止发布: ' + Path(path).name)
        # Protect the target source itself from concurrent edits as well.
        source_file = source if receipt['analysis_source'] else source / 'manifest.json'
        if digest(safe_read(source_file, 128 * 1024 * 1024)) != receipt['manifest_sha256']:
            raise ValueError('来源清单在核对期间发生变化')
        verification_file = (source if receipt['analysis_source'] else
            source / ('verification.json' if (source / 'verification.json').exists() else 'manifest.json'))
        if digest(safe_read(verification_file, 128 * 1024 * 1024)) != receipt['verification_sha256']:
            raise ValueError('冻结目标在核对期间发生变化')
        audit['inputs_unchanged_after_export'] = True
        write_json('source-audit.json', audit)
        destination.mkdir()
        staging.rename(destination / 'package')
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise
    return {'path': str(destination / 'package'), 'ready': ready, 'count': len(frames), 'audit_counts': counts,
            'candidate_generated': ready, 'partial': partial, 'changed_count': len(changed), 'held_count': held,
            'issues': issues, 'reason_counts': dict(Counter(item['code'] for item in issues)),
            'status': 'partial' if partial else 'candidate' if ready else 'no-changes' if result is not None and not issues else 'needs-review',
            'blocked_count': len(result['blocked']) if result else None, 'target_sha256': receipt['target_sha256'],
            'rmse_before_ev': current_metrics['rmse_ev'], 'acr_verified': False}


def _issues(checks, result):
    reasons = {
        'unsupported': ('缺少足够的曝光响应依据，保留当前值', '补充同帧曝光校准显影；邻帧响应需至少三个且一致'),
        'measurement-unknown': ('冻结测光规则无法给出有效区间，保留当前值', '检查暗部、高光及导出设置；必要时重新显影'),
        'inconsistent-response': ('实测曝光响应不一致或区间过宽，保留当前值', '核对同帧 XMP 和显影设置，补充曝光校准'),
        'exposure-limit': ('候选超过累计 2 EV 或绝对 5 EV，保留当前值', '检查基线和目标，缩小目标修正后再试'),
        'within-measurement-interval': ('目标位于测量不确定区间，保留当前值', '当前依据不足以确定修正方向；可缩小测光不确定性后复核'),
    }
    issues = []
    for i, check in enumerate(checks):
        if check['status'] != 'verified':
            details = check['mismatches'] or check['missing']
            first = details[0]
            reason = '显影设置或帧身份记录不一致' if check['mismatches'] else '缺少显影设置或帧身份记录'
            action = '使用对应 XMP 显影同名 JPEG，保留全部元数据，并核对原始参考目录'
            if first.get('field') == 'exposure' and 'expected' in first:
                reason = f'曝光记录不一致：XMP 为 {first["expected"]:+.6f} EV，JPEG 为 {first["actual"]:+.2f} EV'
                action = '选择生成本批 JPEG 时实际使用的 XMP 文件夹；或用当前 XMP 重新显影'
            elif first.get('field') == 'sha256':
                reason = '原始文件与冻结记录的文件指纹不一致'
            else:
                reason += '（' + str(first.get('field', first.get('check', 'metadata'))) + '）'
            if len(details) > 1:
                reason += f'，共 {len(details)} 处问题'
            issues.append({'frame': i + 1, 'name': check['name'],
                'code': first.get('check', '') + ':' + first.get('field', 'source'),
                'reason': reason, 'action': action})
        elif result and result['statuses'][i] in reasons:
            reason, action = reasons[result['statuses'][i]]
            issues.append({'frame': i + 1, 'name': check['name'], 'code': result['statuses'][i],
                           'reason': reason, 'action': action})
    return issues


def _report(folder, manifest, audit, metrics, original, actual, result):
    goal = manifest['result']['target']
    series = [('原始测量', original, '#85958a'), ('冻结亮度目标', goal, '#d6e6a5'), ('本次实际复核', actual, '#e2ac79')]
    if result:
        series.append(('下一轮预测（未显影）', result['prediction'], '#79c4e6'))
    values = [v for _, a, _ in series for v in a]
    low, high = min(values) - .15, max(values) + .15
    paths = []
    for label, data, color in series:
        points = ' '.join(f'{45+i/(len(data)-1)*1000:.2f},{260-(v-low)/(high-low)*220:.2f}' for i, v in enumerate(data))
        dash = ' stroke-dasharray="5 4"' if '预测' in label else ''
        paths.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.5"{dash}/>')
    ticks = ''.join(f'<text x="0" y="{260-i*55:.0f}">{low+i*(high-low)/4:.2f}</text>' for i in range(5))
    legend = '　'.join(f'<span style="color:{c}">● {l}</span>' for l, _, c in series)
    rows = []
    if result:
        for i in manifest.get('sample_frames', []):
            j = i - 1
            rows.append(f'<tr><td>{i}</td><td>{html.escape(manifest["frames"][j]["name"])}</td><td>{result["final"][j]:+.2f}</td><td>{result["delta"][j]:+.3f}</td><td>{result["target"][j]:.3f}</td></tr>')
    status = (f'候选修正包已生成：{manifest["changed_count"]} 帧更新，{manifest["held_count"]} 帧保留当前值；尚未经过 ACR 重显影验收'
              if manifest['ready'] else '没有可执行的候选变更；请查看逐帧诊断或当前残差')
    issues = [f'第 {f["frame"]} 帧 {html.escape(f["name"])}：{html.escape(f["reason"])}。{html.escape(f["action"])}'
              for f in manifest['issues']]
    text = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>冻结目标 · 曝光反馈检查</title>
<style>body{{background:#121d17;color:#e1e7e0;font:16px system-ui;margin:36px auto;max-width:1120px;padding:0 24px;line-height:1.7}}h1{{font-size:28px}}svg{{width:100%;background:#1b2821;margin-top:18px}}svg text{{fill:#b5c4b8;font-size:12px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:8px;border-bottom:1px solid #344438;text-align:left}}code{{overflow-wrap:anywhere}}a{{color:#d6e6a5}}.notice{{padding:16px;background:#29392a;border-radius:8px}}details{{margin:18px 0}}</style>
<h1>冻结目标 · 曝光反馈检查</h1><p class="notice">{status}</p>
<p>{len(original)} 帧 · 来源核对：{html.escape(str(audit['counts']))} · 原素材保持只读</p>
<p>本次实际目标误差 RMSE：<b>{metrics['rmse_ev']:.3f} EV</b>。这是修正前的实测值；蓝色虚线仅表示下一轮模型预测。</p>
<div>{legend}</div><svg viewBox="0 0 1070 290" role="img" aria-label="原始、冻结目标、本次复核与下一轮预测曲线">{ticks}{''.join(paths)}</svg>
<p>目标指纹：<code>{manifest['target_sha256']}</code>。每轮保留同一目标，不重新检测或重选已确认台阶。</p>
<p>先在独立 RAW 工作副本检查代表帧，再验证完整序列。曝光值是绝对值。输入配对数据已核对元数据与哈希；它不是对像素生成过程的独立鉴证。</p>
<p><a href="curves.csv">完整逐帧数据</a> · <a href="source-audit.json">来源核对</a> · <a href="manifest.json">冻结目标与候选清单</a> · <a href="current-verification.json">当前真实残差</a></p>
<details><summary>逐帧问题与处理建议（{len(issues)} 帧）</summary><p>{'<br>'.join(issues) or '未发现记录冲突或缺失。'}</p></details>
<h2>代表帧检查清单</h2><table><thead><tr><th>帧</th><th>文件</th><th>候选 XMP 曝光</th><th>本轮增量</th><th>冻结亮度目标</th></tr></thead><tbody>{''.join(rows)}</tbody></table></html>'''
    (folder / 'review.html').write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='来源核对、冻结目标残差反馈，仅生成独立修正包')
    parser.add_argument('--source', required=True, help='原写回备份、导出包或上一轮反馈 package')
    parser.add_argument('--original-previews', required=True)
    parser.add_argument('--current-xmps', required=True)
    parser.add_argument('--previews', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.original_previews, args.current_xmps, args.previews, args.output,
                           progress=lambda i, n: print(f'核对 {i}/{n}', flush=True) if i % 100 == 0 else None), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
