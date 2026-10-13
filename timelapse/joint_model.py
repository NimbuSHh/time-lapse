"""Measured multi-tone feedback. No Adobe rendering or universal slider model."""
from io import BytesIO
import warnings

import numpy as np
from PIL import Image, ImageCms, ImageOps

from .tone_xmp import LIMITS

SOLVER_VERSION = 'bounded-discrete-v2'

FEATURES = ('midtone', 'highlight', 'white', 'red_tail', 'green_tail', 'blue_tail')
RADII = np.array([.2, 10., 10.])
WEIGHTS = np.array([2., 2., 1., .8, .8, .8])
CEILING = float(np.log2(((.985 + .055) / 1.055) ** 2.4))


def roi_box(roi, width, height):
    if len(roi) != 4 or not all(np.isfinite(roi)) or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
        raise ValueError('测量区域须为 0–1 的左、上、右、下坐标')
    l, t, r, b = (round(roi[0]*width), round(roi[1]*height), round(roi[2]*width), round(roi[3]*height))
    if min(r-l, b-t) < 4:
        raise ValueError('联合反馈测量区域过小')
    return np.s_[t:b, l:r]


def pixels(data):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as im:
            if im.format != 'JPEG' or im.width*im.height > 24_000_000 or max(im.size) > 12000:
                raise ValueError('需要最多 2400 万像素的 JPEG 小图')
            profile = im.info.get('icc_profile')
            if profile:
                name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(BytesIO(profile)))
                if 'srgb' not in name.lower():
                    raise ValueError('联合反馈需要 sRGB 小图')
            elif im.getexif().get_ifd(34665).get(40961) != 1:
                raise ValueError('联合反馈需要明确标记为 sRGB 的小图')
            rgb = ImageOps.exif_transpose(im).convert('RGB')
            dimensions = rgb.size
            # Fixed nearest-neighbour sampling avoids hiding clipping by averaging.
            rgb.thumbnail((640, 640), Image.Resampling.NEAREST)
            arr = np.asarray(rgb, dtype=float)/255
    linear = np.where(arr <= .04045, arr/12.92, ((arr+.055)/1.055)**2.4)
    return linear, dimensions


def measure(linear, mid_roi, high_roi, reference=None, selection=None):
    """Identical pixels in baseline and each probe; never reselect by probe tone."""
    ref = linear if reference is None else reference
    if ref.shape != linear.shape:
        raise ValueError('校准与基准图尺寸必须相同')
    height, width = linear.shape[:2]
    if selection:
        from .auto_regions import masks
        mid, high = masks(selection, linear.shape)
    else:
        mid = roi_box(mid_roi, width, height)
        high = roi_box(high_roi, width, height)
    luminance = linear @ np.array([.2126, .7152, .0722])
    ref_y = ref @ np.array([.2126, .7152, .0722])
    mid_mask = (ref[mid].max(axis=-1) > .002) & (ref[mid].max(axis=-1) < .95)
    if np.count_nonzero(mid_mask) < max(16, mid_mask.size*.1):
        raise ValueError('中间调参考区域缺少可测像素，请换一个区域')
    bright_mask = ref_y[high] >= np.quantile(ref_y[high], .95)
    vals = [np.mean(luminance[mid][mid_mask]), np.mean(luminance[high]), np.mean(luminance[high][bright_mask])]
    for c in range(3):
        mask = ref[:, :, c] >= np.quantile(ref[:, :, c], .995)
        vals.append(np.mean(linear[:, :, c][mask]))
    if min(vals) <= 1e-7:
        raise ValueError('亮度区域过暗，无法可靠测量')
    clip = linear >= ((254/255+.055)/1.055)**2.4
    return {'brightness': np.log2(vals).tolist(), 'channel_clip_fraction': clip.mean(axis=(0, 1)).tolist(),
            'any_clip_fraction': float(clip.any(axis=2).mean()), 'white_clip_fraction': float(clip.all(axis=2).mean())}


def targets(observed, boundaries=(), window=31):
    values = np.asarray(observed, float)
    if values.ndim != 2 or values.shape[1] != 6 or len(values) < 5 or not np.all(np.isfinite(values)):
        raise ValueError('联合目标需要至少五帧、每帧六项有效测量')
    if not isinstance(window, int) or window < 5 or window > 121 or window % 2 == 0:
        raise ValueError('联合平滑窗口须为 5–121 的奇数')
    if any(not isinstance(b, int) or not 0 < b < len(values) for b in boundaries):
        raise ValueError('联合目标分段边界无效')
    result = values.copy()
    cuts = [0] + sorted(set(boundaries)) + [len(values)]
    for start, stop in zip(cuts, cuts[1:]):
        for i in range(start+1, stop-1):
            a, b = max(start, i-window//2), min(stop, i+window//2+1)
            x = np.arange(a, b)-i
            weight = (1-np.abs(x)/(window//2+1))**2
            design = np.column_stack([np.ones(len(x)), x])
            result[i] = np.linalg.lstsq(design*weight[:, None], values[a:b]*weight[:, None], rcond=None)[0][0]
    # A smooth trend must never raise the current channel ceiling. Later bright
    # scenes retain their shape below the SDR cap; this is not RAW recovery.
    result[:, 3:] = np.minimum(result[:, 3:], CEILING)
    return result.tolist()


def probes(controls):
    controls = np.asarray(controls, float)
    result = []
    for j, radius in enumerate(RADII):
        p = controls.copy()
        # Downward white/exposure probe can reveal a clipped channel; at -100
        # highlights must move upward. Every exact perturbation is recorded.
        direction = -1 if controls[j]-radius >= LIMITS[j][0] else 1
        p[j] = round(p[j]+direction*radius, 2 if j == 0 else 0)
        result.append(p.tolist())
    return result


def solve(observed, target, controls, probe_controls, probe_observations, baseline=None):
    observed, target, controls, pc, py = (np.asarray(x, float) for x in
        (observed, target, controls, probe_controls, probe_observations))
    if observed.shape != (6,) or target.shape != (6,) or controls.shape != (3,) or pc.shape != (3, 3) or py.shape != (3, 6):
        raise ValueError('联合校准需要同帧三组独立参数扰动及六项测量')
    if not all(np.all(np.isfinite(x)) for x in (observed, target, controls, pc, py)):
        raise ValueError('联合校准包含无效数值')
    baseline = controls if baseline is None else np.asarray(baseline, float)
    if baseline.shape != (3,) or not np.all(np.isfinite(baseline)):
        raise ValueError('联合反馈基线无效')
    dx = pc-controls
    if np.any(np.abs(dx-np.diag(np.diag(dx))) > 1e-6) or np.any(np.abs(np.diag(dx)) < RADII*.5):
        raise ValueError('每组校准必须只改变一个参数，且有足够扰动')
    jac = (py-observed).T/np.diag(dx)
    scaled = jac*RADII
    singular = np.linalg.svd(scaled*WEIGHTS[:, None], compute_uv=False)
    condition = float(singular[0]/singular[-1]) if singular[-1] > 1e-10 else None
    base_result = {'current': controls.tolist(), 'final': controls.tolist(), 'delta': [0., 0., 0.],
                   'prediction': observed.tolist(), 'jacobian': jac.tolist(), 'condition': condition, 'rendered': False}
    if jac[0, 0] <= 0 or np.max(jac[1:3, 1]) <= 0 or np.max(jac[2:, 2]) <= 0:
        return {**base_result, 'status': 'inconsistent-slider-response', 'blocked': True}
    if condition is None or condition > 150 or singular[-1] < .004:
        return {**base_result, 'status': 'insufficient-independent-response', 'blocked': True}
    # Search the controls that can actually be exported. Independently rounding
    # a continuous optimum is not an optimum on this coupled three-slider grid.
    lower = np.maximum(np.array([x[0] for x in LIMITS]), controls-RADII)
    upper = np.minimum(np.array([x[1] for x in LIMITS]), controls+RADII)
    lower[0] = max(lower[0], baseline[0]-2)
    upper[0] = min(upper[0], baseline[0]+2)
    if np.any(lower > upper):
        return {**base_result, 'status': 'cumulative-exposure-limit', 'blocked': True}
    can_hold = bool(np.all(controls >= lower-1e-8) and np.all(controls <= upper+1e-8))
    before = float(np.linalg.norm((observed-target)*WEIGHTS))
    if before <= .04 and can_hold:
        return {**base_result, 'status': 'within-noise', 'blocked': False}
    scale = np.array([100., 1., 1.])
    low = np.ceil(lower*scale-1e-8)/scale
    high = np.floor(upper*scale+1e-8)/scale
    axes = [np.arange(round(lo*k), round(hi*k)+1)/k for lo, hi, k in zip(low, high, scale)]
    candidates = np.array(np.meshgrid(*axes, indexing='ij')).reshape(3,-1).T
    # Keeping the exact source value is legitimate even when an imported
    # exposure has finer precision than the exported adjustment grid.
    if can_hold:
        candidates = np.vstack([controls, candidates])
    delta = candidates-controls
    predictions = observed+delta@jac.T
    feasible = np.all(predictions[:,3:] <= np.maximum(observed[3:], CEILING)+1e-8, axis=1)
    candidates, delta, predictions = candidates[feasible], delta[feasible], predictions[feasible]
    if not len(candidates):
        return {**base_result, 'status': 'no-feasible-bounded-solution', 'blocked': True}
    errors = np.sum(((predictions-target)*WEIGHTS)**2, axis=1)
    costs = errors+np.sum((delta/RADII*.02)**2, axis=1)
    best = int(np.argmin(costs))
    candidate, predicted = candidates[best], predictions[best]
    after = float(np.sqrt(errors[best]))
    if after >= before-1e-4:
        if not can_hold:
            return {**base_result, 'status': 'no-supported-improvement', 'blocked': True}
        # This is a valid calibrated hold, not missing/bad calibration. Preserve
        # the source and let unchanged, strict full-sequence verification decide
        # quality. Never describe this as a successful improvement.
        return {**base_result, 'status': 'unchanged-at-discrete-optimum', 'blocked': False,
                'needs_review': True, 'weighted_error_before': before,
                'weighted_error_predicted': before}
    return {**base_result, 'final': candidate.tolist(), 'delta': (candidate-controls).tolist(),
            'prediction': predicted.tolist(), 'status': 'measured-joint-response',
            'blocked': False, 'weighted_error_before': before, 'weighted_error_predicted': after,
            'at_limit': bool(np.any(np.isclose(candidate, low)) or np.any(np.isclose(candidate, high)))}
