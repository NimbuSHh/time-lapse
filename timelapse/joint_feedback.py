"""Export-only joint ACR feedback: analyse -> probe -> solve -> verify.

Render metadata establishes recorded settings, not pixel authenticity. No function
opens RAW files for writing, changes source XMP, or claims to run Adobe Camera Raw.
"""
import argparse
from collections import Counter
import html
import json
from pathlib import Path
import shutil
import tempfile
import uuid

import numpy as np

from . import analysis, auto_regions, joint_model as model, tone_xmp
from .workspace import output_path, safe_child_output
from .feedback import target_hash
from .project import directory, digest, natural, safe_read, scan
from .provenance import jpeg_evidence, xmp_evidence, _identity_equal

PROBES = ('exposure', 'highlights', 'whites')
DEFAULT_MID = [0., 0., 1., 1.]
DEFAULT_HIGH = [0., 0., 1., 1.]


def measurement_hash(mid_roi, high_roi, selection):
    return digest(json.dumps([mid_roi, high_roi, selection], sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def discover_regions(files, inputs, progress=None):
    stats, dimensions = [], None
    for i, key in enumerate(sorted(files, key=natural)):
        linear, size = model.pixels(read(files[key], inputs))
        if dimensions is not None and size != dimensions:
            raise ValueError('自动测光需要同尺寸、同裁切的序列')
        dimensions = size
        stats.append(auto_regions.statistics(linear))
        if progress:
            progress(i+1, len(files))
    return auto_regions.choose(stats), dimensions


def suggest_regions(previews, progress=None):
    """Read-only preview of the deterministic selection used by prepare."""
    files = scan(directory(previews), {'.jpg', '.jpeg'})
    inputs = {}
    selection, dimensions = discover_regions(files, inputs, progress)
    for path, sha in inputs.items():
        if digest(safe_read(Path(path), 25*1024*1024)) != sha:
            raise ValueError('选择区域期间小图发生变化，请重新分析')
    return {'selection': selection, 'count': len(files), 'dimensions': list(dimensions)}


def read(path, inputs, limit=25*1024*1024, expected=None):
    data = safe_read(path, limit)
    sha = digest(data)
    if expected is not None and sha != expected:
        raise ValueError('文件与冻结快照不一致：' + Path(path).name)
    inputs[str(path)] = sha
    return data


def write_json(folder, name, value):
    (folder/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def match(expected_xmp, rendered, name, *, full_size=False):
    expected, actual = xmp_evidence(expected_xmp), jpeg_evidence(rendered, full_size=full_size)
    if actual['missing'] or actual['exposure'] is None:
        raise ValueError(name + '：缺少内嵌显影设置')
    if expected['crs'] != actual['crs'] or abs(expected['exposure']-actual['exposure']) > .0051:
        raise ValueError(name + '：显影参数与校准 XMP 不一致')
    required = ('DateTimeOriginal', 'ExposureTime', 'FNumber', 'ISOSpeedRatings')
    for key in required:
        if key not in expected['identity'] or key not in actual['identity']:
            raise ValueError(name + '：缺少拍摄身份 ' + key)
    for key, value in expected['identity'].items():
        if key not in actual['identity'] or not _identity_equal(key, value, actual['identity'][key]):
            raise ValueError(name + '：拍摄身份不符 ' + key)


def manifest(folder, inputs, kind):
    folder = directory(folder)
    data = json.loads(read(folder/'manifest.json', inputs, 64*1024*1024))
    if data.get('kind') != kind or data.get('schema') != 1:
        raise ValueError('请选择对应的联合反馈包')
    if data.get('target_sha256') != target_hash(data['target']):
        raise ValueError('联合反馈目标指纹不符')
    selection = data.get('measurement_selection')
    if data.get('region_mode') == 'auto' and not selection:
        raise ValueError('自动测光包缺少冻结网格')
    if selection:
        auto_regions.masks(selection, (240, 320, 3))
    if selection or 'measurement_sha256' in data:
        if data.get('measurement_sha256') != measurement_hash(data['mid_roi'], data['high_roi'], selection):
            raise ValueError('联合反馈测光区域指纹不符')
    values = np.asarray(data['target'], float)
    if values.shape != (len(data['frames']), 6) or not np.all(np.isfinite(values)):
        raise ValueError('联合目标维度或数值无效')
    names = []
    for f in data['frames']:
        for key, extension in (('xmp', '.xmp'), ('preview', '.jpg')):
            name = f[key]
            if Path(name).name != name or '\\' in name or Path(name).suffix.lower() not in (extension, '.jpeg' if key == 'preview' else extension):
                raise ValueError('联合清单文件名无效')
        if Path(f['xmp']).stem.casefold() != Path(f['preview']).stem.casefold():
            raise ValueError('联合清单帧名不一致')
        names.append(f['preview'].casefold())
    if len(set(names)) != len(names) or not 5 <= len(names) <= 5000:
        raise ValueError('联合清单重复或帧数无效')
    return folder, data


def staging(output, protected):
    root = output_path(output)
    if any(root == p or (p in root.parents and not safe_child_output(root, p)) for p in protected):
        raise ValueError('联合反馈输出不能位于输入目录内')
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix='.joint-', dir=root)), root/('joint-'+uuid.uuid4().hex)


def publish(stage, destination, inputs):
    for path, sha in inputs.items():
        if digest(safe_read(Path(path), 64*1024*1024)) != sha:
            raise ValueError('导出期间输入发生变化：' + Path(path).name)
    write_json(stage, 'source-audit.json', {'inputs': inputs, 'inputs_unchanged_after_export': True,
        'scope': 'recorded-metadata-hashes-and-image-measurements', 'pixel_provenance_authenticated': False})
    stage.rename(destination)


def prepare(previews, output, mid_roi=None, high_roi=None, window=31, source=None, progress=None, region_mode=None, fixed_selection=None):
    """Freeze multi-tone targets and export orthogonal, bounded calibration XMP."""
    inputs = {}
    previews = directory(previews)
    files = scan(previews, {'.jpg', '.jpeg'})
    if not 5 <= len(files) <= 5000:
        raise ValueError('联合分析需要 5–5000 帧')
    prior, source_folder, selection = None, None, fixed_selection
    if fixed_selection is not None:
        from .auto_regions import masks
        if source or region_mode != 'auto': raise ValueError('固定网格只能用于新自动参考')
        masks(fixed_selection,(96,128,3))
    protected = [previews]
    if source:
        source_folder, prior = manifest(source, inputs, 'joint-feedback-solution')
        if not prior['ready']:
            raise ValueError('只有完整联合修正包才能进入下一轮')
        protected.append(source_folder)
        protected.extend(Path(f['reference_preview']).resolve().parent for f in prior['frames'])
        if set(files) != {Path(f['preview']).stem.casefold() for f in prior['frames']}:
            raise ValueError('下一轮必须覆盖冻结序列的全部帧')
        mid_roi, high_roi, window = prior['mid_roi'], prior['high_roi'], prior['window']
        selection = prior.get('measurement_selection')
        region_mode = 'auto' if selection else 'manual'
    else:
        region_mode = region_mode or ('manual' if mid_roi is not None or high_roi is not None else 'auto')
        if region_mode not in ('auto', 'manual'):
            raise ValueError('测光模式只能选择自动或手动')
        mid_roi = DEFAULT_MID if mid_roi is None else list(mid_roi)
        high_roi = DEFAULT_HIGH if high_roi is None else list(high_roi)
    stage, dest = staging(output, protected)
    try:
        if region_mode == 'auto' and prior is None and selection is None:
            selection, _ = discover_regions(files, inputs,
                (lambda i, n: progress(i, n*2)) if progress else None)
            mid_roi, high_roi = DEFAULT_MID, DEFAULT_HIGH
        for name in ('baseline',) + tuple('probe-'+p for p in PROBES):
            (stage/name).mkdir()
            (stage/'jpg'/name).mkdir(parents=True)
        frames, observations, dimensions, times, cameras, signatures = [], [], None, [], [], []
        ordered = sorted(files, key=natural) if prior is None else [Path(f['preview']).stem.casefold() for f in prior['frames']]
        for i, key in enumerate(ordered):
            path = files[key]
            data = read(path, inputs, expected=inputs.get(str(path)))
            xmp, evidence = tone_xmp.recipe_from_jpeg(data)
            identity = evidence['identity']
            if identity.get('RawFileName') and Path(identity['RawFileName']).stem.casefold() != key:
                raise ValueError(path.name + '：内嵌 RAW 文件名不符')
            # Validate complete capture identity even during initial analysis.
            match(xmp, data, path.name)
            linear, size = model.pixels(data)
            if dimensions is not None and dimensions != size:
                raise ValueError('联合分析需要相同尺寸和裁切的序列')
            dimensions = size
            reference, reference_sha = str(path), digest(data)
            reference_pixels = linear
            original_controls = tone_xmp.values(xmp)
            if prior:
                pf = prior['frames'][i]
                expected = read(source_folder/'corrected'/pf['xmp'], inputs, expected=pf['corrected_sha256'])
                match(expected, data, path.name)
                reference, reference_sha = pf['reference_preview'], pf['reference_sha256']
                ref_data = read(Path(reference), inputs, expected=reference_sha)
                reference_pixels, ref_size = model.pixels(ref_data)
                if ref_size != size:
                    raise ValueError('本轮显影与首次基准尺寸不符')
                original_controls = pf['original_controls']
                if not np.allclose(original_controls, tone_xmp.values(tone_xmp.recipe_from_jpeg(ref_data)[0]), atol=1e-6, rtol=0):
                    raise ValueError('原始参数基线与首次显影记录不符')
            obs = model.measure(linear, mid_roi, high_roi, reference_pixels, selection)
            controls = tone_xmp.values(xmp)
            filename = path.stem+'.xmp'
            (stage/'baseline'/filename).write_bytes(xmp)
            variants = model.probes(controls)
            hashes = {}
            for label, values in zip(PROBES, variants):
                candidate = tone_xmp.update(xmp, values)
                (stage/('probe-'+label)/filename).write_bytes(candidate)
                hashes[label] = digest(candidate)
            frames.append({'frame': i+1, 'preview': path.name, 'xmp': filename, 'preview_sha256': digest(data),
                'baseline_sha256': digest(xmp), 'controls': controls, 'original_controls': original_controls,
                'probe_controls': variants, 'probe_sha256': hashes, 'reference_preview': reference,
                'reference_sha256': reference_sha, 'identity': identity})
            observations.append(obs)
            times.append(analysis.capture_time(identity)); cameras.append(analysis.camera_ev(identity))
            signatures.append({k: v for k, v in evidence['crs'].items() if k not in (*tone_xmp.FIELDS, 'RawFileName')})
            if progress:
                progress(i+1+(len(ordered) if region_mode == 'auto' and prior is None else 0),
                         len(ordered)*(2 if region_mode == 'auto' and prior is None else 1))
        # Preserve gaps and other recipe changes; already-compensated camera
        # changes remain in the smoothing window and are exposed in the report.
        intervals = [b-a for a, b in zip(times, times[1:]) if a is not None and b is not None and b > a]
        cadence = float(np.median(intervals)) if intervals else None
        boundaries = [i for i in range(1, len(frames)) if signatures[i] != signatures[i-1] or
            (times[i] is not None and times[i-1] is not None and (times[i] <= times[i-1] or (cadence and times[i]-times[i-1] > 1.8*cadence)))]
        camera_events = [i for i in range(1, len(frames)) if cameras[i] is not None and cameras[i-1] is not None and abs(cameras[i]-cameras[i-1]) > .1]
        observed = [o['brightness'] for o in observations]
        target = prior['target'] if prior else model.targets(observed, boundaries, window)
        selected = {0, len(frames)-1}
        # Full probe sets are always present. The sample is just an economical
        # first look; partial renders never imply complete sequence correction.
        residual = np.linalg.norm(np.asarray(observed)-target, axis=1)
        selected.update(np.argsort(residual)[-6:].tolist())
        selected.update(np.argsort([o['any_clip_fraction'] for o in observations])[-4:].tolist())
        ranked_events = sorted(camera_events, key=lambda j: abs(observed[j][1]-observed[j-1][1]), reverse=True)[:4]
        for j in ranked_events:
            selected.update(range(max(0, j-4), min(len(frames), j+4)))
        samples = sorted(selected)
        for name in ('baseline',) + tuple('probe-'+p for p in PROBES):
            (stage/('sample-'+name)).mkdir()
            for i in samples:
                shutil.copyfile(stage/name/frames[i]['xmp'], stage/('sample-'+name)/frames[i]['xmp'])
        result = {'schema': 1, 'kind': 'joint-feedback-calibration', 'package_id': dest.name,
            'baseline_origin': 'jpeg-embedded-recipe', 'baseline_note': '从 JPEG 内嵌设置重建独立 RAW 校准 XMP；不是原目录 XMP 的备份。',
            'previews': str(previews), 'dimensions': list(dimensions), 'mid_roi': mid_roi, 'high_roi': high_roi,
            'window': window, 'boundaries': boundaries, 'camera_events': camera_events, 'features': list(model.FEATURES),
            'region_mode': region_mode, 'measurement_selection': selection,
            'measurement_sha256': measurement_hash(mid_roi, high_roi, selection),
            'target': target, 'target_sha256': target_hash(target), 'observations': observations, 'frames': frames,
            'samples': samples, 'ready': False, 'acr_verified': False, 'round': 1 if prior is None else prior['round']+1,
            'status': 'awaiting-acr-calibration', 'source': str(source_folder) if prior else None}
        write_json(stage, 'manifest.json', result)
        write_json(stage, 'sample-plan.json', [{'frame': i+1, 'preview': frames[i]['preview'], 'controls': frames[i]['controls'],
            'probe_controls': frames[i]['probe_controls']} for i in samples])
        instructions(stage)
        preview_data = read(files[ordered[len(ordered)//2]], inputs, expected=frames[len(ordered)//2]['preview_sha256'])
        (stage/'region-preview.jpg').write_bytes(preview_data)
        report(stage, result)
        publish(stage, dest, inputs)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return summary(dest, result)


def calibrate(source, baseline_previews, exposure_previews, highlights_previews, whites_previews, output, progress=None):
    inputs = {}
    source, original = manifest(source, inputs, 'joint-feedback-calibration')
    dirs, mappings = [], []
    for label, path in zip(('基准', '曝光', '高光', '白色色阶'),
                           (baseline_previews, exposure_previews, highlights_previews, whites_previews)):
        try:
            folder = directory(path)
            mapping = scan(folder, {'.jpg', '.jpeg'})
        except (ValueError, OSError) as exc:
            raise ValueError(f'{label}小图无法读取：{path}。请先在 ACR 导出到此处，或展开“小图保存在其他位置？”修改目录。详情：{exc}') from exc
        if not mapping:
            raise ValueError(f'{label}小图尚未导出：{path}。请在 ACR 导出全序列 JPG 到此处，再重新计算。')
        dirs.append(folder)
        mappings.append(mapping)
    if not mappings[0] or any(set(m) != set(mappings[0]) for m in mappings):
        raise ValueError('基准和三个校准目录必须包含完全相同的帧，可先使用小样')
    known = {Path(f['preview']).stem.casefold() for f in original['frames']}
    if not set(mappings[0]) <= known:
        raise ValueError('校准目录包含冻结序列之外的文件')
    protected = [source, directory(original['previews']), *dirs,
                 *(Path(f['reference_preview']).resolve().parent for f in original['frames'])]
    stage, dest = staging(output, protected)
    try:
        solutions, frames, actuals, rendered_count = [], [], [], 0
        candidates = {}
        for i, f in enumerate(original['frames']):
            key = Path(f['preview']).stem.casefold()
            frame = dict(f)
            if key not in mappings[0]:
                solutions.append({'status': 'missing-calibration', 'blocked': True})
                frames.append(frame); actuals.append(original['observations'][i]); continue
            ref_data = read(Path(f['reference_preview']), inputs, expected=f['reference_sha256'])
            ref, size = model.pixels(ref_data)
            if not np.allclose(f['original_controls'], tone_xmp.values(tone_xmp.recipe_from_jpeg(ref_data)[0]), atol=1e-6, rtol=0):
                raise ValueError('原始参数基线与首次显影记录不符')
            if list(size) != original['dimensions']:
                raise ValueError('冻结参考尺寸不符')
            observed, variants, baseline_xmp = [], [], None
            for j, (mapping, folder) in enumerate(zip(mappings, ('baseline',)+tuple('probe-'+p for p in PROBES))):
                expected_sha = f['baseline_sha256'] if j == 0 else f['probe_sha256'][PROBES[j-1]]
                xmp = read(source/folder/f['xmp'], inputs, expected=expected_sha)
                data = read(mapping[key], inputs)
                match(xmp, data, mapping[key].name)
                linear, size = model.pixels(data)
                if list(size) != original['dimensions']:
                    raise ValueError('校准图尺寸不符')
                observation = model.measure(linear, original['mid_roi'], original['high_roi'], ref, original.get('measurement_selection'))
                observed.append(observation)
                variants.append(tone_xmp.values(xmp))
                if j == 0:
                    baseline_xmp = xmp
            # Reconstructed JPEG recipes must first reproduce the baseline. Do
            # not fit a slider model to an unrelated RAW import or stale render.
            if np.max(np.abs(np.asarray(observed[0]['brightness'])-original['observations'][i]['brightness'])) > .04:
                solution = {'status': 'baseline-render-not-reproduced', 'blocked': True}
            else:
                solution = model.solve(observed[0]['brightness'], original['target'][i], variants[0], variants[1:],
                    [v['brightness'] for v in observed[1:]], baseline=f['original_controls'])
            solution['probe_observations'] = observed[1:]
            if not solution['blocked']:
                candidate = tone_xmp.update(baseline_xmp, solution['final'])
                candidates[f['xmp']] = candidate
                frame['corrected_sha256'] = digest(candidate)
            frames.append(frame); solutions.append(solution); actuals.append(observed[0]); rendered_count += 1
            if progress:
                progress(rendered_count, len(mappings[0]))
        ready = all(not s['blocked'] for s in solutions)
        if candidates:
            folder = stage/('corrected' if ready else 'sample-corrected')
            folder.mkdir()
            for name, data in candidates.items():
                (folder/name).write_bytes(data)
        result = {**original, 'kind': 'joint-feedback-solution', 'package_id': dest.name,
            'source': str(source), 'ready': ready, 'acr_verified': False, 'frames': frames,
            'solutions': solutions, 'solver_version': model.SOLVER_VERSION, 'observations': actuals, 'rendered_calibration_count': rendered_count,
            'status': 'candidate-needs-acr-verification' if ready else 'partial-or-unsupported-calibration'}
        write_json(stage, 'manifest.json', result)
        instructions(stage)
        report(stage, result)
        publish(stage, dest, inputs)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return summary(dest, result)


def verify(source, previews, output, progress=None):
    inputs = {}
    source, original = manifest(source, inputs, 'joint-feedback-solution')
    if not original['ready']:
        raise ValueError('请先完成全序列校准，不能把小样当作全序列验收')
    previews = directory(previews)
    mapping = scan(previews, {'.jpg', '.jpeg'})
    if set(mapping) != {Path(f['preview']).stem.casefold() for f in original['frames']}:
        raise ValueError('复核必须覆盖全部冻结帧')
    stage, dest = staging(output, [source, previews, directory(original['previews']),
        *(Path(f['reference_preview']).resolve().parent for f in original['frames'])])
    try:
        observations, reference_observations = [], []
        for i, f in enumerate(original['frames']):
            data = read(mapping[Path(f['preview']).stem.casefold()], inputs)
            xmp = read(source/'corrected'/f['xmp'], inputs, expected=f['corrected_sha256'])
            match(xmp, data, f['preview'])
            linear, size = model.pixels(data)
            ref, ref_size = model.pixels(read(Path(f['reference_preview']), inputs, expected=f['reference_sha256']))
            if list(size) != original['dimensions'] or size != ref_size:
                raise ValueError('复核显影尺寸不符')
            observations.append(model.measure(linear, original['mid_roi'], original['high_roi'], ref, original.get('measurement_selection')))
            reference_observations.append(model.measure(ref, original['mid_roi'], original['high_roi'], ref, original.get('measurement_selection')))
            if progress:
                progress(i+1, len(original['frames']))
        error = np.asarray([o['brightness'] for o in observations])-original['target']
        # Each round keeps the same clipping budget against the frozen first
        # render; the preceding round is not a new allowance for more clipping.
        clip_before = np.array([o['channel_clip_fraction'] for o in reference_observations])
        clip_after = np.array([o['channel_clip_fraction'] for o in observations])
        # Published numeric acceptance is separate from subjective playback.
        metrics = {'rmse_ev_by_band': np.sqrt(np.mean(error**2, axis=0)).tolist(),
            'max_error_ev_by_band': np.max(abs(error), axis=0).tolist(),
            'max_adjacent_residual_ev': np.max(abs(np.diff(error, axis=0)), axis=0).tolist(),
            'channel_clipping_worsened_frames': np.flatnonzero(np.any(clip_after > clip_before+.001, axis=1)).tolist(),
            'thresholds': {'max_tone_error_ev': .08, 'max_adjacent_tone_residual_ev': .06,
                           'clip_fraction_increase': .001, 'max_channel_tail_error_ev': .02},
            'visual_playback_verified': False}
        accepted = bool(np.max(abs(error[:, :3])) <= .08 and np.max(abs(np.diff(error[:, :3], axis=0))) <= .06
                        and not metrics['channel_clipping_worsened_frames'] and np.max(error[:, 3:]) <= .02)
        result = {**original, 'kind': 'joint-feedback-verification', 'source': str(source), 'package_id': dest.name,
            'observations': observations, 'metrics': metrics, 'acr_verified': True, 'numerically_accepted': accepted,
            'status': 'render-measured-pass' if accepted else 'render-measured-residual-remains'}
        write_json(stage, 'manifest.json', result)
        write_json(stage, 'verification.json', metrics)
        report(stage, result)
        publish(stage, dest, inputs)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return summary(dest, result)


def summary(path, data):
    return {'path': str(path), 'count': len(data['frames']), 'ready': data['ready'], 'status': data['status'],
        'acr_verified': data['acr_verified'], 'target_sha256': data['target_sha256'],
        'sample_count': len(data['samples']), 'calibrated_count': data.get('rendered_calibration_count', 0),
        'measurement_selection': data.get('measurement_selection'),
        'issues': calibration_issues(data),
        'blocked_count': sum(s['blocked'] for s in data['solutions']) if 'solutions' in data else len(data['frames']),
        'held_count': sum(s.get('needs_review', False) for s in data.get('solutions', [])),
        'numerically_accepted': data.get('numerically_accepted')}


def calibration_issues(data):
    guidance = {
        'missing-calibration': ('缺少校准小图', '请补齐四组全序列 JPG，不要只导出 sample 小样。'),
        'baseline-render-not-reproduced': ('基准显影与原小图不一致', '请让 ACR 重新读取 baseline XMP，保持原裁切、尺寸和其他设置后重新导出。'),
        'insufficient-independent-response': ('三个滑块的效果无法可靠区分', '请返回准备，展开高级设置，检查或手动选择有细节的中间调和高光区域。'),
        'inconsistent-slider-response': ('滑块响应不一致', '请检查四组显影是否读取了对应 XMP，并保持其他设置一致。'),
        'cumulative-exposure-limit': ('累计曝光达到限制', '请检查亮度目标；需要时返回准备，清空上轮修正包并重新设定目标。'),
        'no-feasible-bounded-solution': ('当前范围内找不到可用修正', '请查看包内 review.html 中这些帧的亮度目标与参数。'),
        'predicted-channel-clipping-worsens': ('修正可能加重高光触顶', '请检查这些帧的高光，必要时先在 ACR 调整后重新开始。'),
        'no-supported-improvement': ('未测得可靠改善', '请查看包内 review.html 的逐帧诊断，确认当前效果是否已可接受。'),
    }
    counts = Counter(s['status'] for s in data.get('solutions', []) if s['blocked'])
    return [{'count': count, 'reason': guidance.get(key, (key, '请查看包内逐帧诊断。'))[0],
             'action': guidance.get(key, (key, '请查看包内逐帧诊断。'))[1]} for key, count in counts.items()]


def instructions(folder):
    (folder/'使用说明.txt').write_text('''联合反馈：曝光、高光、白色色阶
本包只导出；没有修改源 RAW、XMP 或 JPEG。baseline 从 JPEG 内嵌显影设置重建，不是原 XMP 备份。
1. 页面使用三步引导：准备 → 导入小图 → 检查效果。默认自动选择测光区域，包路径与进度自动记住。
2. 在独立 RAW 工作副本中，依次配对以下四组同名 XMP，并在 ACR 导出全序列 sRGB JPEG：
   baseline          → 本校准包/jpg/baseline
   probe-exposure    → 本校准包/jpg/probe-exposure
   probe-highlights  → 本校准包/jpg/probe-highlights
   probe-whites      → 本校准包/jpg/probe-whites
   每次关闭并重新打开 ACR 读取 XMP；保持文件名、完整元数据、尺寸、裁切与其他设置一致，不要批量同步曝光。
   上述 JPG 保存目录已在新校准包中建好，页面可直接复制路径。已有小图可展开“小图保存在其他位置？”分别选择。
3. 返回第 2 步，点击“已导出四组小图，自动计算”。目录自动填好；基准重显影须复现原小图到 0.04 亮度 EV 内。
4. 全部帧响应充分时生成完整修正包中的 corrected；第 3 步会自动显示它的目录。请将其中的 XMP 配对到工作副本，再用 ACR 导出一组小图并点击“检查效果”。
5. 检查通过后，播放确认连续性与色相，再在 ACR 导出最终成片。有残差可点击“继续改善剩余跳变”，自动沿用最新显影、原目标和曝光基线。
6. 高级用法：sample-* 只用于先试小样；仅有小样时只生成 sample-corrected，不推测未校准帧的响应。完整修正必须使用四组全部同名帧。
7. 曝光单轮最多 0.2 EV，高光/白色色阶最多 10，累计曝光相对本流程最初基线不超过 2 EV。预测不等于实际显影效果。
目录名含 sample 的数据不可用于宣称全片完成。记录设置核对不能独立鉴证像素生成过程。
此方法平滑测量趋势，不识别云层或推断真实光照。通道触顶只是 JPEG 输出证据，不证明 RAW 过曝或恢复能力。
''', encoding='utf-8')


def report(folder, data):
    actual = np.array([o['brightness'] for o in data['observations']])
    target = np.asarray(data['target'])
    charts = []
    for j, label in enumerate(('中间调参考区域', '高光参考区域', '高光区域最亮 5%')):
        series = [(actual[:, j], '#eead78', ''), (target[:, j], '#a2d6ac', '')]
        if data['kind'] == 'joint-feedback-solution':
            predicted = np.array([s.get('prediction', [np.nan]*6)[j] if not s['blocked'] else np.nan for s in data['solutions']])
            series.append((predicted, '#80bcf1', ' stroke-dasharray="5 4"'))
        finite = np.concatenate([v[np.isfinite(v)] for v, _, _ in series])
        lo, hi = float(finite.min()-.05), float(finite.max()+.05)
        lines = []
        for values, color, dash in series:
            segments, current = [], []
            for i, v in enumerate(values):
                if np.isfinite(v):
                    current.append(f'{35+i/(len(actual)-1)*980:.1f},{180-(v-lo)/(hi-lo)*155:.1f}')
                elif current:
                    segments.append(current); current = []
            if current:
                segments.append(current)
            lines.extend(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.5"{dash}/>' for points in segments)
        charts.append(f'<h2>{label}</h2><svg viewBox="0 0 1040 210"><text x="0" y="20">{hi:.2f}</text><text x="0" y="185">{lo:.2f}</text>{"".join(lines)}</svg>')
    worst = np.argsort(abs(np.diff(actual[:, 1])))[-16:][::-1]+1
    rows = ''.join(f'<tr><td>{i+1}</td><td>{html.escape(data["frames"][i]["preview"])}</td><td>{actual[i,0]-actual[i-1,0]:+.3f}</td><td>{actual[i,1]-actual[i-1,1]:+.3f}</td><td>{actual[i,2]-actual[i-1,2]:+.3f}</td><td>{"是" if i in data["camera_events"] else "否"}</td></tr>' for i in worst)
    clips = np.array([o['channel_clip_fraction'] for o in data['observations']])
    statuses = dict(Counter(s['status'] for s in data.get('solutions', [])))
    selection = data.get('measurement_selection')
    regions = f'手动中间调区域：{html.escape(str(data["mid_roi"]))}；高光区域：{html.escape(str(data["high_roi"]))}。坐标为左、上、右、下的画面比例。'
    overlay = ''
    if selection:
        regions = (f'自动分析 {selection["analysed_frames"]} 帧，固定使用 {len(selection["mid_tiles"])} 个中间调网格、'
            f'{len(selection["high_tiles"])} 个高光网格。选中网格的最低可测帧比例：中间调 {selection["mid_usable_fraction"]:.1%}，高光 {selection["high_usable_fraction"]:.1%}。')
        if (folder/'region-preview.jpg').exists():
            cells = ''.join(f'<rect x="{i%selection["cols"]}" y="{i//selection["cols"]}" width="1" height="1" fill="{color}" fill-opacity=".2" stroke="{color}" stroke-width=".04"/>'
                for key, color in (('mid_tiles', '#89efbd'), ('high_tiles', '#ffd568')) for i in selection[key])
            overlay = f'<p>绿色：中间调；黄色：高光。网格在全序列固定，不识别物体。</p><div style="position:relative"><img src="region-preview.jpg" alt="序列中段自动测光网格预览" style="width:100%;display:block"><svg viewBox="0 0 {selection["cols"]} {selection["rows"]}" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%;background:transparent">{cells}</svg></div>'
    labels = {'missing-calibration': '尚未校准', 'baseline-render-not-reproduced': '基准显影不一致',
        'insufficient-independent-response': '三个参数的响应无法可靠区分', 'inconsistent-slider-response': '响应方向不一致',
        'unchanged-at-discrete-optimum': '约束内无可靠改善，保留原设置并等待真实复核',
        'within-noise': '已在测量容差内', 'measured-joint-response': '已生成实测响应候选',
        'cumulative-exposure-limit': '累计曝光越界', 'no-feasible-bounded-solution': '约束内无可行解',
        'predicted-channel-clipping-worsens': '预测会加重通道触顶', 'no-supported-improvement': '没有可靠改善'}
    titles = {'awaiting-acr-calibration': '分析完成，等待 ACR 校准显影',
        'candidate-needs-acr-verification': '完整候选已生成，等待 ACR 重显影复核',
        'partial-or-unsupported-calibration': '校准不完整或响应不足，不能作为全序列修正',
        'render-measured-pass': '真实显影数值复核通过，仍须播放检查',
        'render-measured-residual-remains': '已测量真实显影，仍有残差'}
    note = ' · '.join(f'{labels.get(k,k)}: {v}' for k, v in statuses.items())
    candidates = ''
    if data.get('solutions'):
        candidate_rows = []
        for f, s in zip(data['frames'], data['solutions']):
            final = ' / '.join(f'{v:+.2f}' for v in s['final']) if not s['blocked'] else '—'
            delta = ' / '.join(f'{v:+.2f}' for v in s['delta']) if not s['blocked'] else '—'
            candidate_rows.append(f'<tr><td>{f["frame"]}</td><td>{html.escape(f["preview"])}</td><td>{final}</td><td>{delta}</td><td>{html.escape(labels.get(s["status"],s["status"]))}</td></tr>')
        candidates = '<details><summary>逐帧参数与未完成原因</summary><table><tr><th>帧</th><th>文件</th><th>曝光 / 高光 / 白色色阶</th><th>本轮增量</th><th>状态</th></tr>'+''.join(candidate_rows)+'</table></details>'
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>联合反馈检查</title>
<style>body{{background:#14201b;color:#e4ece6;font:16px system-ui;max-width:1080px;margin:40px auto;padding:0 24px;line-height:1.7}}h1{{font-size:28px}}h2{{font-size:20px}}svg{{width:100%;background:#1e3026}}svg text{{fill:#acc4b2;font-size:11px}}td,th{{padding:7px;border-bottom:1px solid #3b5042;text-align:left}}table{{width:100%;border-collapse:collapse}}code{{overflow-wrap:anywhere}}a{{color:#a2d6ac}}.notice{{padding:18px;background:#314333}}li{{margin:5px 0}}</style>
<h1>曝光 · 高光 · 白色色阶联合反馈</h1><p class="notice">{titles[data['status']]}</p>
<p>{len(actual)} 帧 · 首轮推荐小样 {len(data['samples'])} 帧 · 原素材只读</p>
<p>{regions}</p>{overlay}
<p>橙线：本次实际测量；绿线：冻结目标；蓝色虚线（如有）：候选预测，尚未显影，缺少校准的帧不连线。单位为线性亮度 EV，不能直接作为曝光滑块数值。</p>
<p>JPEG 通道接近顶格（≥254）的最大像素比例：R {clips[:,0].max():.2%} · G {clips[:,1].max():.2%} · B {clips[:,2].max():.2%}。不代表 RAW 剪切。</p>
<p>目标保留缓慢变化；遇到拍摄时间缺口或其他调色变化时分段。高光与白色色阶的真实响应只能由同帧独立重显影测量。</p>
{''.join(charts)}<h2>高光相邻变化最大的帧</h2><p>这里列的是变化，不自动断言都是闪烁；拍摄间隔与云层也可能产生变化。</p>
<table><tr><th>帧</th><th>文件</th><th>中间调变化</th><th>高光变化</th><th>极亮变化</th><th>相机调参</th></tr>{rows}</table>
<h2>校准与验收</h2><p>{html.escape(note) or '尚未取得三个参数的独立 ACR 响应；没有生成最终修正 XMP。'}</p>{candidates}
<p>目标指纹：<code>{data['target_sha256']}</code></p><p><a href="manifest.json">全部测量、参数、目标与来源</a> · <a href="source-audit.json">输入完整性核对</a> · <a href="使用说明.txt">操作步骤</a></p>
</html>'''
    (folder/'review.html').write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='曝光、高光、白色色阶联合反馈，仅导出')
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--previews', required=True); p.add_argument('--output', required=True)
    p.add_argument('--source'); p.add_argument('--window', type=int, default=31)
    p.add_argument('--mid-roi', nargs=4, type=float); p.add_argument('--high-roi', nargs=4, type=float)
    p.add_argument('--region-mode', choices=['auto', 'manual'], help='不传选区时默认自动；有选区时默认手动')
    p = sub.add_parser('calibrate')
    for key in ('source', 'baseline-previews', 'exposure-previews', 'highlights-previews', 'whites-previews', 'output'):
        p.add_argument('--'+key, required=True)
    p = sub.add_parser('verify')
    for key in ('source', 'previews', 'output'):
        p.add_argument('--'+key, required=True)
    args = vars(parser.parse_args()); action = args.pop('action')
    result = globals()[action](**args, progress=lambda i, n: print(f'{i}/{n}', flush=True) if i % 100 == 0 else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
