"""Sequence-wide fixed metering cells, never per-frame moving subjects."""
import numpy as np

ROWS, COLS = 24, 32
METHOD = 'sequence-fixed-grid-v1'


def grid_ids(shape, rows=ROWS, cols=COLS):
    height, width = shape[:2]
    if height < rows*4 or width < cols*4:
        # Keep small test/previews usable with the same normalised grid.
        if height < rows or width < cols:
            raise ValueError('自动测光需要至少 32×24 像素的小图')
    ry = np.repeat(np.arange(rows), np.diff(np.linspace(0, height, rows+1, dtype=int)))
    cx = np.repeat(np.arange(cols), np.diff(np.linspace(0, width, cols+1, dtype=int)))
    return ry[:, None]*cols+cx[None, :]


def statistics(linear):
    ids = grid_ids(linear.shape).ravel()
    count = np.bincount(ids, minlength=ROWS*COLS)
    y = linear @ np.array([.2126, .7152, .0722])
    clipping = linear >= ((254/255+.055)/1.055)**2.4
    return np.stack([np.bincount(ids, weights=a.ravel(), minlength=ROWS*COLS)/count
        for a in (y, clipping.any(axis=2), clipping.all(axis=2))], axis=1)


def choose(statistics):
    data = np.asarray(statistics, float)
    if data.ndim != 3 or data.shape[1:] != (ROWS*COLS, 3) or len(data) < 5 or not np.all(np.isfinite(data)):
        raise ValueError('自动测光需要完整序列的有效网格统计')
    y, clip, white = data[:, :, 0], data[:, :, 1], data[:, :, 2]
    q25, q65 = np.quantile(y, [.25, .65], axis=1)
    mid_quality = ((y > .004) & (y < .65) & (clip < .03)).mean(axis=0)
    high_quality = ((y > .015) & (y < .93) & (white < .1)).mean(axis=0)
    mid_frequency = ((y >= q25[:, None]) & (y <= q65[:, None])).mean(axis=0)
    high_frequency = (y >= q65[:, None]).mean(axis=0)
    changes = np.diff(np.log2(np.maximum(y, 1e-7)), axis=0)
    # Subtract each pair's common brightness change before estimating local
    # instability. A camera exposure change is not itself a moving object.
    instability = np.quantile(abs(changes-np.median(changes, axis=1)[:, None]), .8, axis=0)
    mid_candidates = np.flatnonzero((mid_quality >= .9) & (mid_frequency >= .55))
    high_candidates = np.flatnonzero((high_quality >= .85) & (high_frequency >= .6))
    if len(mid_candidates) < 16 or len(high_candidates) < 8:
        raise ValueError('整段序列缺少持续可测的中间调或高光区域，请使用手动框选；不会退回整图猜测')
    mid_score = 2*mid_frequency+mid_quality-2*instability
    high_score = 2*high_frequency+high_quality-2*instability-.5*clip.mean(axis=0)
    # Stable sorting gives repeatable selections when uniform cells tie.
    mid = mid_candidates[np.argsort(-mid_score[mid_candidates], kind='stable')[:48]]
    high_candidates = np.setdiff1d(high_candidates, mid)
    # Include both bright detail and near-white detail. Selecting only the very
    # brightest patch can make Highlights and Whites indistinguishable.
    ordered = high_candidates[np.argsort(np.median(y[:, high_candidates], axis=0), kind='stable')]
    groups = np.array_split(ordered, 2)
    high = np.concatenate([g[np.argsort(-high_score[g], kind='stable')[:16]] for g in groups])
    if len(high) < 8 or np.median(y[:, high]) < 1.6*np.median(y[:, mid]):
        raise ValueError('自动测光无法可靠区分两个亮度区域，请使用手动框选')
    return {'method': METHOD, 'rows': ROWS, 'cols': COLS, 'mid_tiles': sorted(mid.tolist()),
        'high_tiles': sorted(high.tolist()), 'analysed_frames': len(y),
        'mid_usable_fraction': float(np.min(mid_quality[mid])),
        'high_usable_fraction': float(np.min(high_quality[high])),
        'note': '按整段亮度、触顶比例和局部稳定性选择固定网格；不识别海面、建筑、云或太阳。'}


def masks(selection, shape):
    if selection.get('method') != METHOD or selection.get('rows') != ROWS or selection.get('cols') != COLS:
        raise ValueError('自动测光网格版本不符')
    for key, minimum in (('mid_tiles', 16), ('high_tiles', 8)):
        indices = selection.get(key)
        if not isinstance(indices, list) or not minimum <= len(indices) <= 64 or any(type(i) is not int or not 0 <= i < ROWS*COLS for i in indices) or len(set(indices)) != len(indices):
            raise ValueError('自动测光网格无效')
    if set(selection['mid_tiles']) & set(selection['high_tiles']):
        raise ValueError('自动中间调与高光区域不能重叠')
    ids = grid_ids(shape)
    return np.isin(ids, selection['mid_tiles']), np.isin(ids, selection['high_tiles'])
