"""Scene-independent metering regions frozen over the complete baseline sequence.

Selection uses exposure quality and temporal stability, never scene labels or
per-frame re-selection. All inputs are preview statistics; this module does not
open files or modify source material.
"""
import copy

import numpy as np

from . import auto_regions

NAMES = ('primary', 'secondary', 'check_a', 'check_b')
METHOD = 'sequence-frozen-trajectory-regions-v1'
statistics = auto_regions.statistics


def validate(contract):
    if not isinstance(contract, dict) or contract.get('method') != METHOD:
        raise ValueError('测光区域版本无效')
    if contract.get('per_frame_pixel_reselection') is not False:
        raise ValueError('测光区域必须在整段序列中固定')
    if contract.get('mode') == 'manual':
        rois = contract.get('rois', {})
        if set(rois) != set(NAMES):
            raise ValueError('手动测光需要 primary、secondary、check_a、check_b 四个区域')
        for roi in rois.values():
            values = np.asarray(roi, dtype=float)
            if (values.shape != (4,) or not np.all(np.isfinite(values))
                    or not 0 <= values[0] < values[2] <= 1
                    or not 0 <= values[1] < values[3] <= 1):
                raise ValueError('手动区域必须是有效的归一化矩形 [x0,y0,x1,y1]')
        for i, name in enumerate(NAMES):
            a = rois[name]
            for other in NAMES[i+1:]:
                b = rois[other]
                if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                    raise ValueError('四个测光区域不能重叠，以保留独立空间证据')
    elif contract.get('mode') == 'automatic':
        if contract.get('rows') != auto_regions.ROWS or contract.get('cols') != auto_regions.COLS:
            raise ValueError('测光网格尺寸无效')
        tiles = contract.get('tiles', {})
        if set(tiles) != set(NAMES):
            raise ValueError('固定测光区域不完整')
        used = set()
        for name in NAMES:
            group = tiles[name]
            if (not isinstance(group, list) or not 8 <= len(group) <= 48
                    or any(type(i) is not int or not 0 <= i < auto_regions.ROWS*auto_regions.COLS for i in group)
                    or len(set(group)) != len(group) or used.intersection(group)):
                raise ValueError('测光网格必须有效且互不重叠')
            used.update(group)
    else:
        raise ValueError('未知的测光区域方式')
    return copy.deepcopy(contract)


def choose(grid_statistics, manual_rois=None):
    """Choose four independent groups from full-sequence linear RGB statistics.

    The bright and middle groups supply independent camera-step evidence; two
    other luminance bands provide additional diagnostics. Manual rectangles are
    kept exactly as supplied and receive the same sequence-quality validation.
    """
    data = np.asarray(grid_statistics, dtype=float)
    if (data.ndim != 3 or data.shape[1:] != (auto_regions.ROWS*auto_regions.COLS, 3)
            or len(data) < 5 or not np.all(np.isfinite(data))
            or np.any((data < 0) | (data > 1))):
        raise ValueError('测光需要至少五帧且覆盖完整序列的有效预览网格')
    y, clip = data[:, :, 0], data[:, :, 1]
    good = ((y > .003) & (y < .90) & (clip < .03)).mean(axis=0)
    changes = np.diff(np.log2(np.maximum(y, 1e-7)), axis=0)
    instability = np.quantile(abs(changes-np.median(changes, axis=1)[:, None]), .9, axis=0)
    contract = {'method': METHOD, 'per_frame_pixel_reselection': False,
                'analysed_frames': len(data), 'target_region': 'primary',
                'evidence_regions': ['primary', 'secondary'],
                'diagnostic_regions': ['secondary', 'check_a', 'check_b']}
    if manual_rois is not None:
        contract.update(mode='manual', rois=copy.deepcopy(manual_rois))
        validate(contract)
        yy, xx = np.meshgrid((np.arange(auto_regions.ROWS)+.5)/auto_regions.ROWS,
                             (np.arange(auto_regions.COLS)+.5)/auto_regions.COLS, indexing='ij')
        groups = {}
        for name, (x0, y0, x1, y1) in manual_rois.items():
            ids = np.flatnonzero(((xx >= x0) & (xx < x1) & (yy >= y0) & (yy < y1)).ravel())
            if len(ids) < 4 or np.mean(good[ids] >= .95) < .8:
                raise ValueError(f'手动区域 {name} 太小或整段序列中曝光不可测，请调整区域')
            groups[name] = ids
    else:
        eligible = np.flatnonzero((good >= .95) & (instability < .15))
        if len(eligible) < 64:
            raise ValueError('整段序列缺少足够稳定、未触顶的独立测光区域，请使用手动框选')
        levels = np.median(y, axis=0)
        order = eligible[np.argsort(levels[eligible], kind='stable')]
        groups = {}
        for name, band in zip(('check_a', 'secondary', 'check_b', 'primary'), np.array_split(order, 4)):
            score = instability[band] + (1-good[band])
            groups[name] = band[np.argsort(score, kind='stable')[:24]]
        primary = float(np.median(y[:, groups['primary']]))
        secondary = float(np.median(y[:, groups['secondary']]))
        if primary < 1.4*secondary:
            raise ValueError('画面缺少可区分的亮度区域，无法可靠辨认局部调色响应，请使用手动框选')
        contract.update(mode='automatic', rows=auto_regions.ROWS, cols=auto_regions.COLS,
                        tiles={name: sorted(ids.tolist()) for name, ids in groups.items()})
    contract['quality'] = {name: {'usable_fraction': float(np.mean(good[ids])),
                                'relative_instability_ev': float(np.median(instability[ids])),
                                'median_linear_luminance': float(np.median(y[:, ids]))}
                           for name, ids in groups.items()}
    return validate(contract)


def masks(contract, shape):
    validate(contract)
    height, width = shape[:2]
    if contract['mode'] == 'automatic':
        ids = auto_regions.grid_ids(shape)
        return {name: np.isin(ids, contract['tiles'][name]) for name in NAMES}
    result = {}
    occupied = np.zeros((height, width), dtype=bool)
    for name, (x0, y0, x1, y1) in contract['rois'].items():
        mask = np.zeros((height, width), dtype=bool)
        mask[round(y0*height):round(y1*height), round(x0*width):round(x1*width)] = True
        if mask.sum() < 16 or np.any(mask & occupied):
            raise ValueError(f'测光区域 {name} 在此预览分辨率下太小或重叠')
        occupied |= mask
        result[name] = mask
    return result


def measure(linear, contract):
    rgb = np.asarray(linear, dtype=float)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or not np.all(np.isfinite(rgb)) or np.any((rgb < 0) | (rgb > 1)):
        raise ValueError('测光需要有效的线性 RGB 图像')
    luminance = rgb @ np.array([.2126, .7152, .0722])
    return {name: float(np.log2(max(float(luminance[mask].mean()), 1e-7)))
            for name, mask in masks(contract, rgb.shape).items()}
