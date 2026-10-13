"""Inspectable estimates, never a substitute for an ACR re-render."""
from datetime import datetime
from io import BytesIO
import math
import warnings
import numpy as np
from PIL import Image, ImageOps, ImageCms

Image.MAX_IMAGE_PIXELS = 24_000_000

def numeric(value):
    try:
        if isinstance(value, str) and '/' in value:
            a, b = value.split('/')
            v = float(a) / float(b)
        else:
            v = float(value)
        return v if math.isfinite(v) and v > 0 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def measure(data, roi):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            if source.format != 'JPEG' or source.width * source.height > 24_000_000 or max(source.size) > 12000:
                raise ValueError('预览必须是最多 2400 万像素的 JPEG；建议长边 1600 像素')
            exif = dict(source.getexif())
            try:
                exif.update(source.getexif().get_ifd(34665))
            except (KeyError, TypeError):
                pass
            profile = source.info.get('icc_profile')
            if profile:
                try:
                    name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(BytesIO(profile)))
                except Exception as e:
                    raise ValueError('无法识别 JPEG 色彩配置') from e
                if 'srgb' not in name.lower():
                    raise ValueError('请从 ACR 导出 sRGB 小图；当前 JPEG 不是 sRGB')
            elif exif.get(40961) not in (None, 1):
                raise ValueError('JPEG EXIF 标示非 sRGB；请重新导出 sRGB')
            rgb = ImageOps.exif_transpose(source).convert('RGB')
            dimensions = rgb.size
            left, top, right, bottom = roi
            box = (round(left * rgb.width), round(top * rgb.height), round(right * rgb.width), round(bottom * rgb.height))
            rgb = rgb.crop(box)
            if min(rgb.size) < 32:
                raise ValueError('参考区域过小，至少需要 32×32 像素')
            rgb.thumbnail((512, 512))
            arr = np.asarray(rgb, dtype=np.float64) / 255
    linear = np.where(arr <= .04045, arr / 12.92, ((arr + .055) / 1.055) ** 2.4)
    y = linear @ np.array([.2126, .7152, .0722])
    clipped = (arr.max(axis=2) >= .98)
    dark = (arr.max(axis=2) <= .02)
    good = ~(clipped | dark)
    logs, valid = [], []
    for row in np.array_split(np.arange(y.shape[0]), 8):
        for col in np.array_split(np.arange(y.shape[1]), 8):
            ix = np.ix_(row, col)
            logs.append(float(np.log2(max(float(np.median(y[ix])), 1e-8))))
            valid.append(float(good[ix].mean()) >= .9)
    return {'tiles': logs, 'valid': valid, 'clipped': float(clipped.mean()), 'dark': float(dark.mean()), 'dimensions': dimensions,
            'metadata': {'ExposureTime': exif.get(33434), 'FNumber': exif.get(33437), 'ISOSpeedRatings': exif.get(34855), 'DateTimeOriginal': exif.get(36867)},
            'untagged': not profile and exif.get(40961) is None}


def camera_ev(metadata):
    vals = [numeric(metadata.get(k)) for k in ('ExposureTime', 'ISOSpeedRatings', 'FNumber')]
    if any(v is None for v in vals):
        return None
    t, iso, aperture = vals
    return math.log2(t * iso / aperture ** 2)


def capture_time(metadata):
    value = metadata.get('DateTimeOriginal')
    if not value:
        return None
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(str(value)[:19], fmt).timestamp()
        except ValueError:
            pass
    return None


def robust_line(x, y, at):
    x = np.asarray(x, float) - at
    y = np.asarray(y, float)
    slopes = [(y[j] - y[i]) / (x[j] - x[i]) for i in range(len(x)) for j in range(i + 1, len(x)) if x[j] != x[i]]
    slope = float(np.median(slopes)) if slopes else 0.
    offset = float(np.median(y - slope * x))
    residual = float(np.median(np.abs(y - slope * x - offset)))
    return offset, residual


def jump_at(y, index, boundaries):
    left = max([b for b in boundaries if b < index] + [0])
    right = min([b for b in boundaries if b > index] + [len(y)])
    a, b = max(left, index - 8), min(right, index + 8)
    if index - a < 3 or b - index < 3:
        return None
    l, lr = robust_line(range(a, index), y[a:index], index - .5)
    r, rr = robust_line(range(index, b), y[index:b], index - .5)
    return r - l, max(.01, 3 * (lr + rr)), a, b


def cluster_profile(y, indices, window):
    """Shared local trend plus a level per camera plateau, including one-frame ones.

    Only the stable windows before/after the burst determine the natural slope.
    Inner short plateaus provide their measured levels, not invented samples.
    """
    a, b = window
    first, last = indices[0], indices[-1]
    slopes, stable_residuals = [], []
    for lo, hi in ((a, first), (last, b)):
        x = np.arange(lo, hi)
        level, residual = robust_line(x, y[lo:hi], first)
        next_level, _ = robust_line(x, y[lo:hi], first + 1)
        slopes.append(next_level - level)
        stable_residuals.append(residual)
    slope = float(np.mean(slopes))
    detrended = np.asarray(y) - slope * (np.arange(len(y)) - first)
    cuts = [a] + list(indices) + [b]
    levels = [float(np.median(detrended[lo:hi])) for lo, hi in zip(cuts, cuts[1:])]
    return np.diff(levels), slope, max(.01, 3 * sum(stable_residuals)), abs(slopes[0] - slopes[1])


def assess_event(event, checks, success):
    """Evidence determines advice, not permission to use a measured estimate."""
    event['checks'] = [{'code': code, 'passed': bool(passed), 'message': message}
                       for code, passed, message in checks]
    failures = [c['message'] for c in event['checks'] if not c['passed']]
    event.update(selectable=True, recommended=not failures,
                 reason='；'.join(failures) if failures else success)


def response_reference(event):
    # Older frozen reports used selectable for both permission and confidence.
    return event.get('recommended', event.get('selectable', False))


def spatial_fraction(supported, common, weights):
    # Use the same fixed support as the brightness measurement. A nearly empty
    # tile must not have the same vote as a tile with thousands of samples.
    return float(np.average(supported, weights=np.asarray(weights)[common]))


def dense_event(y, tiles, common, indices, boundaries, gaps, exposures, cameras, singles, weights):
    first, last = indices[0], indices[-1]
    event = {'index': first, 'end_index': last, 'indices': indices, 'method': 'dense-cluster',
             'camera_delta': cameras[last] - cameras[first - 1],
             'xmp_delta': exposures[last] - exposures[first - 1],
             'jump': None, 'selectable': False, 'recommended': False, 'reason': '连续调参组前后各需至少 5 帧稳定样本；请扩大序列或检查邻近断点'}
    a = max([v for v in boundaries if v < first] + [0, first - 8])
    b = min([v for v in boundaries if v > last] + [len(y), last + 8])
    # Do not extrapolate across long adjustment runs or missing frames.
    if last - first > 12:
        event['reason'] = '连续调参跨度超过 12 帧，当前联合估计方法不适用；请分段处理'
        return event
    if first - a < 5 or b - last < 5:
        return event
    if any(first <= g <= last for g in gaps):
        event['reason'] = '连续调参组跨越时间缺口，不能联合估计'
        return event
    if any(c is None for c in cameras[a:b]):
        event['reason'] = '连续调参组的支持窗口内缺失拍摄参数'
        return event
    if any(first < v < last and v not in indices for v in boundaries):
        event['reason'] = '连续调参组内有独立的 XMP 曝光断点，不能跨编辑联合估计；请分段处理'
        return event
    cuts = [a] + indices + [b]
    if any(np.ptp(cameras[lo:hi]) > .06 for lo, hi in zip(cuts, cuts[1:])):
        event['reason'] = '连续调参组的支持窗口内拍摄参数不稳定'
        return event
    jumps, slope, noise, slope_difference = cluster_profile(y, indices, (a, b))
    tile_profiles = [cluster_profile(tiles[:, j], indices, (a, b)) for j in np.flatnonzero(common)]
    tile_jumps = np.array([p[0] for p in tile_profiles])
    dispersion = np.median(np.abs(tile_jumps - jumps), axis=0)
    camera_jumps = np.array([cameras[i] - cameras[i - 1] for i in indices])
    net = float(sum(jumps))
    xmp_range = float(np.ptp(exposures[a:b]))
    event.update(jump=net, components=[{'index': i, 'jump': float(j), 'camera_delta': float(c),
                                       'xmp_delta': float(exposures[i] - exposures[i - 1]),
                                       'tile_dispersion': float(d)}
                                      for i, j, c, d in zip(indices, jumps, camera_jumps, dispersion)],
                 residual_range=noise, tile_dispersion=float(max(dispersion)), window=[a, b],
                 natural_slope=slope, xmp_range=xmp_range)
    if xmp_range > .06:
        # Existing edits invalidate a camera-only response, not the measured
        # plateau levels. Keep the observed residual available for confirmation;
        # never subtract XMP EV from JPEG EV or reuse this burst as calibration.
        event['response_gain'] = None
        assess_event(event, [
            ('xmp', False, '窗口内已有 XMP 曝光变化；仅估计当前图像中的剩余跳变，需人工确认，不作为曝光换算依据'),
            ('noise', noise < .15, f'稳定窗口残差 {noise:.3f} EV，需小于 0.150 EV'),
            ('trend', slope_difference * (last - first + 1) < max(.08, noise * 2), '前后自然亮度趋势差异过大'),
        ], '')
        return event
    # Calibrate the burst against a stable net change or independent isolated
    # events. Never assume that one JPEG brightness EV equals one camera EV.
    references = [e['jump'] / e['camera_delta'] for e in singles
                  if response_reference(e) and abs(e['xmp_delta']) < .06 and abs(e['camera_delta']) > .2]
    gain = net / event['camera_delta'] if abs(event['camera_delta']) > .2 else (float(np.median(references)) if references else None)
    expected = camera_jumps * gain if gain is not None else np.full(len(indices), np.nan)
    # A fixed tone curve can give shadows and highlights different gains. Test
    # each tile's plateau sequence against its OWN response, not the global EV.
    # The net step (or independent isolated steps for a zero-net burst) anchors
    # that response; fitting each component separately would hide transients.
    tile_supported = []
    for j, (local_jumps, _, local_noise, _) in zip(np.flatnonzero(common), tile_profiles):
        local_references = [jump_at(tiles[:, j], e['index'], boundaries)[0] / e['camera_delta']
                            for e in singles if response_reference(e) and abs(e['xmp_delta']) < .06
                            and abs(e['camera_delta']) > .2] if abs(event['camera_delta']) <= .2 else []
        local_gain = (float(sum(local_jumps)) / event['camera_delta'] if abs(event['camera_delta']) > .2
                      else float(np.median(local_references)) if local_references else None)
        local_expected = camera_jumps * local_gain if local_gain is not None else np.full(len(indices), np.nan)
        tile_supported.append(local_gain is not None and local_gain > 0
                              and np.all(local_jumps * camera_jumps > 0)
                              and np.all(np.abs(local_jumps - local_expected)
                                         < np.maximum(.15, np.abs(local_expected) * .25) + local_noise))
    support = spatial_fraction(tile_supported, common, weights)
    event.update(response_gain=gain, spatial_support=support)
    assess_event(event, [
        ('response', gain is not None and .4 <= gain <= 1.8, '整组响应缺少依据或超出可估计范围'),
        ('noise', noise < .15, f'稳定窗口残差 {noise:.3f} EV，需小于 0.150 EV'),
        ('trend', slope_difference * (last - first + 1) < max(.08, noise * 2), '前后自然亮度趋势差异过大'),
        ('direction', np.all(jumps * camera_jumps > 0), '部分调参的图像跳变与相机方向不符'),
        ('components', np.all(np.abs(jumps - expected) < np.maximum(.15, np.abs(expected) * .25) + noise),
         '短平台跳变与整组响应不一致'),
        ('spatial', support >= .75, f'连续调参方向与各段响应仅获 {support:.1%} 固定支持，需至少 75%'),
        ('reference', not references or (gain is not None and abs(gain - float(np.median(references))) < .35),
         '整组响应与独立调参事件不一致'),
    ], '待确认：稳定前后窗口、各短平台与空间网格共同支持；整组应用或撤销')
    return event


def event_residual(y, event, boundaries):
    """Report every short plateau transition; a net sum can hide a deep dip."""
    if event.get('method') == 'dense-cluster' and 'window' in event:
        jumps, _, _, _ = cluster_profile(y, event['indices'], event['window'])
        return {'index': event['index'], 'end_index': event['end_index'],
                'remaining_jump': float(sum(jumps)), 'max_component_jump': float(max(abs(jumps))),
                'components': [{'index': i, 'remaining_jump': float(v)} for i, v in zip(event['indices'], jumps)]}
    estimated = jump_at(y, event['index'], boundaries)
    return {'index': event['index'], 'remaining_jump': estimated[0] if estimated else None}


def measure_for_report(data, roi, report):
    # Unversioned durable packages retain the exact historical sampling rule.
    if 'measurement_contract' in report:
        from .measurement import measure_frozen
        return measure_frozen(data, roi, report['measurement_contract'])
    return measure(data, roi)


def infer(measures, exposures, cameras, times, mode, measurement=None, contract=None):
    tiles = np.asarray([m['tiles'] for m in measures])
    common = np.all(np.asarray([m['valid'] for m in measures]), axis=0)
    if contract is None and common.sum() < 8:
        raise ValueError(f'全序列共同有效区域仅 {int(common.sum())}/64，至少需要 8。请避开太阳/深黑区域、更换 ROI 或选取连续子序列；不会自动放宽测量门槛。')
    if contract is not None:
        from .measurement import brightness
        if not measurement or not measurement['passed'] or not common.any():
            raise ValueError('测量诊断未通过，不能生成目标')
        y = np.asarray([brightness(m, {'common_tiles': np.flatnonzero(common).tolist(), 'measurement_contract': contract}) for m in measures])
    else:
        y = np.median(tiles[:, common], axis=1)
    weights = measures[0]['weights'] if contract is not None else np.ones(tiles.shape[1])
    gaps, notes = [], []
    if all(t is not None for t in times):
        diff = np.diff(times)
        if np.any(diff <= 0):
            notes.append('拍摄时间非严格递增（可能跨编号或同秒连拍），请仔细确认文件顺序。')
        positive = diff[diff > 0]
        if len(positive):
            gaps = (np.flatnonzero(diff > 2.5 * np.median(positive)) + 1).tolist()
    else:
        notes.append('拍摄时间不完整：无法可靠检测漏帧或时间缺口，请确认序列连续。')
    candidates = []
    if mode == 'manual':
        for i in range(1, len(y)):
            if cameras[i] is not None and cameras[i - 1] is not None and abs(cameras[i] - cameras[i - 1]) > .15:
                candidates.append(i)
        if any(c is None for c in cameras):
            notes.append('部分拍摄参数缺失：这些位置不能定位调参台阶；可从 ACR 导出保留 EXIF 的 JPEG。')
    else:
        notes.append('视觉模式只做短时平滑，不能可靠区分自然变化与持续曝光台阶。')
    # Existing creative exposure discontinuities must not be silently flattened.
    edit_boundaries = (np.flatnonzero(np.abs(np.diff(exposures)) > .15) + 1).tolist()
    boundaries = sorted(set(candidates + gaps + edit_boundaries))
    events = []
    for i in candidates:
        est = None if i in gaps else jump_at(y, i, boundaries)
        event = {'index': i, 'camera_delta': cameras[i] - cameras[i - 1], 'xmp_delta': exposures[i] - exposures[i - 1], 'jump': None, 'selectable': False, 'recommended': False,
                 'reason': '断点位于时间缺口，当前方法未生成估计；请检查缺帧或分段处理' if i in gaps else '断点前后各需至少 3 帧样本；请扩大序列或检查邻近断点'}
        if est:
            jump, noise, a, b = est
            tile_jumps = [jump_at(tiles[:, j], i, boundaries)[0] for j in np.flatnonzero(common)]
            dispersion = float(np.median(np.abs(np.asarray(tile_jumps) - jump)))
            expected = event['camera_delta'] + event['xmp_delta']
            support = spatial_fraction(np.asarray(tile_jumps) * expected > 0, common, weights)
            event.update(jump=float(jump), residual_range=noise, tile_dispersion=dispersion,
                         spatial_support=support, window=[a, b])
            assess_event(event, [
                ('signal', abs(jump) > max(.06, noise * 2), f'跳变未超过残差门槛 {max(.06, noise * 2):.3f} EV，可能已补偿'),
                ('direction', jump * expected > 0, '图像跳变与相机及已有 XMP 合计变化方向不符'),
                ('response', abs(jump - expected) < max(.35, abs(expected) * .6), '图像跳变幅度与拍摄参数变化不匹配'),
                ('spatial', support >= .75, f'同向跳变仅获 {support:.1%} 固定支持，需至少 75%'),
            ], '待确认：图像和参数方向支持，但不能排除自然光变化')
        events.append(event)
    # Adjacent short plateaus are one inference unit, not independent jumps.
    groups = []
    for i in candidates:
        if groups and i - groups[-1][-1] < 3:
            groups[-1].append(i)
        else:
            groups.append([i])
    singles = events
    by_index = {e['index']: e for e in singles}
    events = [by_index[g[0]] if len(g) == 1 else
              dense_event(y, tiles, common, g, boundaries, gaps, exposures, cameras, singles, weights) for g in groups]
    report = {'measured': y.tolist(), 'common_tiles': np.flatnonzero(common).tolist(), 'events': events, 'boundaries': boundaries, 'gaps': gaps, 'notes': notes}
    if contract is not None:
        report.update(measurement=measurement, measurement_contract=contract)
    return report


def smooth(y, window, boundaries):
    result = np.array(y, dtype=float)
    ends = [0] + sorted(set(boundaries)) + [len(y)]
    for a, b in zip(ends, ends[1:]):
        if b - a < 5:
            continue
        for i in range(a, b):
            width = min(window, b - a)
            lo = max(a, min(i - width // 2, b - width))
            hi = lo + width
            x = np.arange(lo, hi, dtype=float) - i
            design = np.column_stack([np.ones(len(x)), x])
            result[i] = np.linalg.lstsq(design, np.asarray(y[lo:hi]), rcond=None)[0][0]
    return result


def bridge_gaps(y, gaps, boundaries, window):
    """Join output brightness in frame order, without claiming missing scene data.

    Local Hermite spans preserve endpoint levels and limit slopes to avoid
    overshoot. Overlapping spans share one join rather than being rewritten.
    """
    source = np.asarray(y, dtype=float)
    target = source.copy()
    spans = []
    for i in sorted(set(gaps)):
        left = max([v for v in boundaries if v < i] + [0, i - window // 2])
        right = min([v - 1 for v in boundaries if v > i] + [len(y) - 1, i + window // 2 - 1])
        if i in boundaries or i - left < 2 or right - i < 1:
            continue
        if spans and left <= spans[-1]['end_index']:
            spans[-1]['end_index'] = right
            spans[-1]['indices'].append(i)
        else:
            spans.append({'start_index': left, 'end_index': right, 'indices': [i]})
    for span in spans:
        a, b = span['start_index'], span['end_index']
        first, last = span['indices'][0], span['indices'][-1]
        def slope(lo, hi):
            value, _ = robust_line(range(lo, hi), source[lo:hi], lo)
            next_value, _ = robust_line(range(lo, hi), source[lo:hi], lo + 1)
            return next_value - value
        secant = (source[b] - source[a]) / (b - a)
        if abs(secant) < 1e-12:
            m0 = m1 = 0.
        else:
            alpha = max(0., slope(a, min(first, a + 4)) / secant)
            beta = max(0., slope(max(last, b - 3), b + 1) / secant)
            scale = max(1., math.hypot(alpha, beta) / 3)
            m0, m1 = alpha / scale * secant, beta / scale * secant
        t = np.arange(b - a + 1) / (b - a)
        target[a:b + 1] = ((2*t**3 - 3*t**2 + 1) * source[a]
                           + (t**3 - 2*t**2 + t) * (b - a) * m0
                           + (-2*t**3 + 3*t**2) * source[b]
                           + (t**3 - t**2) * (b - a) * m1)
    return target, spans


def correct(report, exposures, selected, strength=.6, window=15, start=0., end=0., smooth_gaps=False):
    if not all(math.isfinite(float(v)) for v in (strength, start, end)) or not 0 <= strength <= 1 or window not in range(5, 62, 2) or abs(start) > 2 or abs(end) > 2:
        raise ValueError('平滑强度、窗口或首尾偏移无效')
    if not isinstance(smooth_gaps, bool):
        raise ValueError('拍摄间隔接缝选项必须是布尔值')
    selected = set(selected)
    allowed = {e['index']: e for e in report['events'] if e['selectable']}
    if not selected <= allowed.keys():
        raise ValueError('包含不可估计的曝光事件')
    y = np.array(report['measured'])
    steps = np.zeros(len(y))
    resolved = set()
    edited = set()
    for i in selected:
        event = allowed[i]
        for component in event.get('components', [event]):
            steps[component['index']:] -= component['jump']
            resolved.add(component['index'])
            if abs(component.get('xmp_delta', event.get('xmp_delta', 0))) > .15:
                edited.add(component['index'])
    stepped = y + steps
    # Once a camera step is confirmed and removed, smooth through its join.
    # Keep unconfirmed steps, time gaps and existing creative edits protected.
    protected = [i for i in report['boundaries'] if i not in resolved
                 or i in report.get('gaps', []) or i in edited]
    correction = steps + strength * (smooth(stepped, window, protected) - stepped)
    gap_joins = []
    if smooth_gaps:
        # A time-only boundary may be joined as an explicit output choice.
        # Camera events at a gap remain unconfirmed; XMP edits remain protected.
        camera_boundaries = {i for e in report['events'] for i in e.get('indices', [e['index']])}
        edit_boundaries = set((np.flatnonzero(np.abs(np.diff(exposures)) > .15) + 1).tolist())
        gaps = set(report.get('gaps', [])) - camera_boundaries - edit_boundaries
        joined, gap_joins = bridge_gaps(y + correction, gaps, sorted(set(protected) - gaps), window)
        correction = joined - y
    # De-stepping alone reconstructs the full scene-light rise and accumulates
    # every manual exposure reduction. Anchor the rendered endpoints instead:
    # a continuous ramp removes that accumulated drift without adding jumps.
    # This is an explicit output-brightness choice, not a physical-light estimate.
    drift = np.linspace(correction[0], correction[-1], len(y))
    brightness_delta = correction - drift + np.linspace(start, end, len(y))
    # Camera EV supplies an initial prior, not a measured XMP response. Keep
    # brightness-space goals separate from the exposure values we write.
    references = [e['jump'] / e['camera_delta'] for e in report['events']
                  if response_reference(e) and abs(e.get('xmp_delta', 0)) < .06
                  and abs(e['camera_delta']) > .2 and e['jump'] is not None]
    gain = float(np.median(references)) if references else 1.
    known = bool(references) and .25 <= gain <= 4
    if not known:
        gain = 1.
    delta = brightness_delta / gain
    final = np.asarray(exposures) + delta
    blocked = np.flatnonzero((np.abs(delta) > 2) | (np.abs(final) > 5)).tolist()
    return {'delta': delta.tolist(), 'final': final.tolist(), 'target': (y + brightness_delta).tolist(), 'blocked': blocked,
            'brightness_delta': brightness_delta.tolist(),
            'response': {'kind': 'camera-prior' if known else 'unavailable', 'gain': gain if known else None,
                         'calibrated': False, 'support': len(references)},
            'calibration_required': bool(not known and np.max(np.abs(brightness_delta)) > 1e-6),
            'event_warnings': [{'index': e['index'], 'reason': e['reason']} for e in report['events']
                               if e['index'] in selected and not response_reference(e)],
            'gap_joins': gap_joins,
            'trajectory': 'brightness-target-gap-joins-v3',
            'settings': {'selected': sorted(selected), 'strength': strength, 'window': window, 'start': start, 'end': end,
                         'smooth_gaps': smooth_gaps}}
