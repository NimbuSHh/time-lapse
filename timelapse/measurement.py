"""Versioned, fixed-pixel metering. Diagnostics never redesign a frozen goal."""
import base64
from io import BytesIO
import math
import tempfile
import warnings
import numpy as np
from PIL import Image, ImageOps, ImageCms

VERSION = 'roi-fixed-support-v3'
LEGACY_VERSION = 'roi-fixed-support-v2'
MIN_PIXELS = 1024   # Distinct JPEG samples; never enlarged/interpolated pixels.
MAX_LOCAL_CHANGE = .2  # EV relative to the common change, not absolute exposure.
MAX_LOCAL_SPAN = .5


class MeasurementError(ValueError):
    def __init__(self, message, measurement):
        super().__init__(message)
        self.measurement = measurement


def decode(data, roi):
    if len(roi) != 4 or not all(math.isfinite(v) for v in roi) or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
        raise ValueError('ROI 必须为 0–1 的左、上、右、下坐标')
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
            width, height = rgb.size
            box = tuple(round(v * size) for v, size in zip(roi, (width, height, width, height)))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError('参考区域没有可测量的 JPEG 像素')
            # Anchor sampling to the full image, not the ROI. A nested ROI uses
            # exactly the same source pixels; resizing cannot create evidence.
            stride = max(1, math.ceil(max(width, height) / 512))
            xs = np.arange(math.ceil(box[0] / stride) * stride, box[2], stride)
            ys = np.arange(math.ceil(box[1] / stride) * stride, box[3], stride)
            arr = np.asarray(rgb.crop(box))[np.ix_(ys - box[1], xs - box[0])].astype(float) / 255
    ids = (np.minimum(7, ((ys-box[1])*8//(box[3]-box[1])))[:, None] * 8
           + np.minimum(7, ((xs-box[0])*8//(box[2]-box[0])))).astype(int)
    linear = np.where(arr <= .04045, arr / 12.92, ((arr+.055)/1.055)**2.4)
    y = linear @ np.array([.2126, .7152, .0722])
    clipped = arr.max(axis=2) >= .98
    dark = arr.max(axis=2) <= .02
    return y, ~(clipped | dark), clipped, dark, ids, {
        'dimensions': [width, height], 'roi': list(roi), 'box': list(box), 'stride': stride,
        'shape': list(y.shape), 'roi_pixels': (box[2]-box[0])*(box[3]-box[1]),
        'metadata': {'ExposureTime': exif.get(33434), 'FNumber': exif.get(33437),
                     'ISOSpeedRatings': exif.get(34855), 'DateTimeOriginal': exif.get(36867)},
        'untagged': not profile and exif.get(40961) is None}


def weighted_median(values, weights):
    values, weights = np.asarray(values), np.asarray(weights)
    order = np.argsort(values, kind='stable')
    return float(values[order[np.searchsorted(np.cumsum(weights[order]), weights.sum()/2, side='left')]])


def summarize(decoded, support):
    y, good, clipped, dark, ids, info = decoded
    tiles, valid, weights = [], [], []
    bounds, invalid_pixels, clipped_pixels, dark_pixels = [], [], [], []
    for j in range(64):
        mask = support & (ids == j)
        count = int(mask.sum())
        weights.append(count)
        tiles.append(float(np.log2(max(float(np.median(y[mask])), 1e-8))) if count else 0.)
        valid.append(bool(count and np.all(good[mask])))
        invalid_pixels.append(int((mask & ~good).sum()))
        clipped_pixels.append(int((mask & clipped).sum()))
        dark_pixels.append(int((mask & dark).sum()))
        limits = [tiles[-1], tiles[-1]]
        if count and invalid_pixels[-1]:
            # Bound the JPEG statistic without dropping/replacing its support.
            # All channels of a dark sample are <= .02 sRGB, so luminance
            # lies in [0, .02/12.92]. A clipped channel gives no useful
            # luminance bound beyond [0, 1]. These are not RAW estimates.
            values = y[mask]
            low, high = values.copy(), values.copy()
            low[~good[mask]] = 0.
            high[dark[mask]] = .02 / 12.92
            high[clipped[mask]] = 1.
            limits = [float(np.log2(max(float(np.median(v)), 1e-8))) for v in (low, high)]
        bounds.append(limits)
    return {'tiles': tiles, 'valid': valid, 'weights': weights,
            'tile_bounds': bounds, 'invalid_pixels': invalid_pixels,
            'clipped_pixels': clipped_pixels, 'dark_pixels': dark_pixels,
            'clipped': float(clipped.mean()) if clipped.size else 0.,
            'dark': float(dark.mean()) if dark.size else 0.,
            'dimensions': tuple(info['dimensions']), 'metadata': info['metadata'], 'untagged': info['untagged']}


def brightness(measure, report):
    common = report['common_tiles']
    if report.get('measurement_contract'):
        return weighted_median([measure['tiles'][j] for j in common], [measure['weights'][j] for j in common])
    return float(np.median([measure['tiles'][j] for j in common]))


def comparability(measure, report):
    """Certify the *same* frozen scalar, never select new pixels or weights."""
    common = report['common_tiles']
    strict = all(measure['valid'][j] for j in common)
    value = brightness(measure, report)
    limits = [value, value] if strict else None
    # v2 and unversioned packages retain their historical validation rules.
    if not strict and (report.get('measurement_contract') or {}).get('version') == VERSION:
        bounds = measure.get('tile_bounds')
        if bounds is not None:
            limits = [weighted_median([bounds[j][side] for j in common],
                                      [measure['weights'][j] for j in common]) for side in (0, 1)]
    # Exact equality is intentional: no EV tolerance or percentage waiver.
    comparable = strict or (limits is not None and limits[0] == value == limits[1])
    return {'comparable': bool(comparable), 'strict_support_valid': strict,
            'brightness_bounds_ev': limits,
            **{key: sum(measure[key][j] for j in common) if key in measure else None
               for key in ('invalid_pixels', 'clipped_pixels', 'dark_pixels')},
            'support_pixels': sum(measure.get('weights', [0] * 64)[j] for j in common)}


def brightness_bounds(measure, report):
    """Return finite bounds of the frozen statistic, or None when unknown.

    Exact comparability remains separate from bounded numerical evidence. In
    particular an invalid legacy support must never become a zero-width pair.
    """
    limits = comparability(measure, report)['brightness_bounds_ev']
    if limits is None:
        return None
    if len(limits) != 2 or not all(math.isfinite(v) for v in limits) or limits[0] > limits[1]:
        raise ValueError('冻结测光区间无效')
    return [float(v) for v in limits]


def response_bounds(reference_bounds, actual_bounds, exposure_delta):
    """Propagate both render intervals through a same-frame exposure secant."""
    if reference_bounds is None or actual_bounds is None:
        return None
    for bounds in (reference_bounds, actual_bounds):
        if len(bounds) != 2 or not all(math.isfinite(v) for v in bounds) or bounds[0] > bounds[1]:
            raise ValueError('显影响应的测光区间无效')
    if not math.isfinite(exposure_delta) or exposure_delta == 0:
        raise ValueError('显影响应需要非零的有限曝光变化')
    return sorted([(actual_bounds[0] - reference_bounds[1]) / exposure_delta,
                   (actual_bounds[1] - reference_bounds[0]) / exposure_delta])


def measure_frozen(data, roi, contract):
    if contract.get('version') not in (VERSION, LEGACY_VERSION) or contract.get('roi') != list(roi):
        raise ValueError('冻结测量规则版本或 ROI 不符')
    decoded = decode(data, roi)
    info = decoded[-1]
    if any(contract.get(k) != info[k] for k in ('dimensions', 'box', 'stride', 'shape')):
        raise ValueError('复核图像尺寸或冻结采样位置不一致')
    size = math.prod(info['shape'])
    try:
        encoded = contract['mask']
        if not isinstance(encoded, str) or len(encoded) > 50000:
            raise ValueError('mask size')
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != (size+7)//8:
            raise ValueError('mask length')
        bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))
        if bits[size:].any():
            raise ValueError('mask padding')
        support = bits[:size].reshape(info['shape']).astype(bool)
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError('冻结像素支持无效') from e
    if int(support.sum()) < MIN_PIXELS or (contract['version'] == LEGACY_VERSION and support.mean() < .125):
        raise ValueError('冻结像素支持不足')
    m = summarize(decoded, support)
    if contract.get('weights') != m['weights']:
        raise ValueError('冻结网格权重不符')
    return m


class SequenceMeter:
    def __init__(self, roi):
        self.roi = list(roi)
        self.info = None
        self.support = None
        self.frames = []
        # Retain only sampled luminance, never full images. Large sequences
        # spill to an automatically removed local file instead of filling RAM.
        self.samples = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        self.measures = []
        self.ids = None
        self.cells = [{'index': j, 'row': j//8, 'col': j%8, 'coverage_min': 1., 'pixel_count_min': None,
                       'failed_frames': [], 'clipped_frames': [], 'dark_frames': [], 'variation_frames': [],
                       'selected': False} for j in range(64)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.samples.close()


    def add(self, data):
        decoded = decode(data, self.roi)
        y, good, clipped, dark, ids, info = decoded
        if self.info is not None and any(info[k] != self.info[k] for k in ('dimensions', 'box', 'stride', 'shape')):
            raise ValueError('JPEG 画幅尺寸不一致；请用同样裁切与导出尺寸重新生成')
        if self.info is None:
            self.ids = ids
        self.info = info
        self.support = good.copy() if self.support is None else self.support & good
        n = len(self.frames)+1
        failed = []
        totals = np.bincount(ids.ravel(), minlength=64)
        counts = np.bincount(ids[good], minlength=64)
        clipped_counts = np.bincount(ids[clipped], minlength=64)
        dark_counts = np.bincount(ids[dark], minlength=64)
        for j, cell in enumerate(self.cells):
            total, count = int(totals[j]), int(counts[j])
            coverage = count/total if total else 0.
            cell['coverage_min'] = min(cell['coverage_min'], coverage)
            cell['pixel_count_min'] = min(cell['pixel_count_min'], count) if cell['pixel_count_min'] is not None else count
            if total == 0 or count < total:
                cell['failed_frames'].append(n)
                failed.append(j)
            if clipped_counts[j]: cell['clipped_frames'].append(n)
            if dark_counts[j]: cell['dark_frames'].append(n)
        self.frames.append({'frame': n, 'coverage': float(good.mean()) if good.size else 0.,
                            'valid_pixels': int(good.sum()), 'failed_cells': failed, 'variation_cells': []})
        self.samples.write(y.tobytes())
        measured = {'clipped': float(clipped.mean()) if clipped.size else 0.,
                    'dark': float(dark.mean()) if dark.size else 0.,
                    'dimensions': tuple(info['dimensions']), 'metadata': info['metadata'],
                    'untagged': info['untagged']}
        self.measures.append(measured)
        return measured

    def finish(self):
        if self.info is None:
            raise ValueError('序列没有可测量帧')
        measures, ids = self.measures, self.ids
        indices = [np.flatnonzero((self.support & (ids == j)).ravel()) for j in range(64)]
        tile_weights = [len(index) for index in indices]
        self.samples.seek(0)
        size = math.prod(self.info['shape']) * np.dtype(float).itemsize
        for measured in measures:
            y = np.frombuffer(self.samples.read(size), dtype=float)
            measured.update(
                tiles=[float(np.log2(max(float(np.median(y[index])), 1e-8))) if len(index) else 0.
                       for index in indices],
                valid=[bool(count) for count in tile_weights], weights=tile_weights.copy())
            # The sequence intersection contains only valid original samples.
            # Match summarize() exactly without reading/decoding images again.
            measured.update(tile_bounds=[[v, v] for v in measured['tiles']],
                            invalid_pixels=[0] * 64, clipped_pixels=[0] * 64, dark_pixels=[0] * 64)
        weights = np.asarray(measures[0]['weights'])
        candidates = np.flatnonzero(weights)
        # Brightness disagreement is diagnostic, not proof of motion: a
        # fixed ACR tone curve can give highlights and shadows different gains.
        if len(candidates):
            tiles = np.asarray([m['tiles'] for m in measures])
            # Locate departures from common changes for visual review only.
            # These can come from content, illumination, or rendering response.
            changes = np.diff(tiles[:, candidates], axis=0)
            common_change = np.array([weighted_median(row, weights[candidates]) for row in changes])
            residual_changes = changes-common_change[:, None]
            # Estimate long-term common change directly against a fixed
            # reference. Integrating medians of pairwise changes accumulates
            # JPEG quantization bias into a fictitious drift on long sequences.
            relative_levels = tiles[:, candidates] - tiles[0, candidates]
            common_levels = np.array([weighted_median(row, weights[candidates]) for row in relative_levels])
            residual_levels = relative_levels-common_levels[:, None]
            for k, j in enumerate(candidates):
                bad_pairs = np.flatnonzero(abs(residual_changes[:, k]) > MAX_LOCAL_CHANGE)
                bad_frames = set((bad_pairs+1).tolist()) | set((bad_pairs+2).tolist())
                if np.ptp(residual_levels[:, k]) > MAX_LOCAL_SPAN:
                    dev = abs(residual_levels[:, k]-np.median(residual_levels[:, k]))
                    bad_frames.update((np.flatnonzero(dev > MAX_LOCAL_SPAN/2)+1).tolist())
                if bad_frames:
                    self.cells[j]['variation_frames'] = sorted(bad_frames)
                    for frame in bad_frames:
                        self.frames[frame-1]['variation_cells'].append(int(j))
        selected = weights > 0
        final_support = self.support & selected[ids]
        pixels = int(final_support.sum())
        fixed_coverage = pixels/final_support.size if final_support.size else 0.
        coverage = fixed_coverage
        best_frame_pixels = max(f['valid_pixels'] for f in self.frames)
        stability = pixels/best_frame_pixels if best_frame_pixels else None
        for frame in self.frames:
            frame['retained_fraction'] = pixels/frame['valid_pixels'] if frame['valid_pixels'] else None
        for j, cell in enumerate(self.cells):
            cell['usable_fraction'] = 1-len(cell['failed_frames'])/len(self.frames)
            cell['selected'] = bool(selected[j])
            cell['support_pixels'] = int(weights[j]) if selected[j] else 0
        checks = [
            {'key': 'coverage', 'label': '全程固定有效覆盖', 'value': coverage, 'minimum': None, 'unit': 'fraction',
             'kind': 'diagnostic', 'note': '仅描述测量区域占比，不因扩大选区而拒绝同一批有效像素。'},
            {'key': 'pixels', 'label': '固定有效采样像素', 'value': pixels, 'minimum': MIN_PIXELS, 'unit': 'pixels'},
            {'key': 'stability', 'label': '跨帧固定像素保留率', 'value': stability, 'minimum': None, 'unit': 'fraction',
             'kind': 'diagnostic', 'note': '相对单帧最多有效像素的保留比例，仅供检查，不作为通过门槛。'}]
        for check in checks:
            check['passed'] = None if check.get('kind') == 'diagnostic' else check['value'] >= check['minimum']
        required = [c for c in checks if c.get('kind') != 'diagnostic']
        diagnostics = {'version': VERSION, 'passed': all(c['passed'] for c in required), 'checks': checks,
            'roi': self.roi, 'dimensions': self.info['dimensions'], 'grid': {'rows': 8, 'cols': 8},
            'cells': self.cells, 'frames': self.frames,
            'support': {'coverage': fixed_coverage, 'pixels': pixels, 'temporal_retention': stability, 'selected_cells': np.flatnonzero(selected).tolist(),
                        'best_frame_pixels': best_frame_pixels, 'sampled_pixels': final_support.size,
                        'roi_pixels': self.info['roi_pixels'], 'stride': self.info['stride']},
            'note': '按全图固定间距取不同 JPEG 像素，不插值放大；全序列使用相同像素交集。8×8 用于位置诊断与局部变化比较，不按合格格数验收。高光/暗部只剔除对应像素；跨帧保留率以单帧最多有效像素为基准，仅供诊断，不代表剩余固定像素的测量可靠性。局部亮度变化差异仅提示检查，可能源于非线性显影或自然光，不能据此判定运动。固定像素全程可测不证明景物不变；小 ROI 通过不代表全画面。'}
        if not diagnostics['passed']:
            def display(value, unit):
                if value is None:
                    return '无法评估'
                return f'{value:.1%}' if unit == 'fraction' else f'{value:,} 个像素'
            failed = '；'.join(f"{c['label']} {display(c['value'], c['unit'])} / 至少 {display(c['minimum'], c['unit'])}" for c in required if not c['passed'])
            raise MeasurementError('测量依据不足：'+failed+'。请扩大参考区域或换用更清晰的小图；可展开测量诊断查看。', diagnostics)
        for m in measures:
            m['weights'] = [w if selected[j] else 0 for j, w in enumerate(m['weights'])]
            m['valid'] = [v and bool(selected[j]) for j, v in enumerate(m['valid'])]
        contract = {k: self.info[k] for k in ('roi', 'dimensions', 'box', 'stride', 'shape')}
        contract.update(version=VERSION, mask=base64.b64encode(np.packbits(final_support).tobytes()).decode(),
                        weights=measures[0]['weights'])
        return measures, diagnostics, contract
