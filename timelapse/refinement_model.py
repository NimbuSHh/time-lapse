"""Small, measured-data helpers for automatic refinement, without rendering.

The caller owns recipe provenance, fixed targets, fixed first-render clipping
references, and neighbor selection. A suggested exposure is an experiment, never
an accepted result. Manual joint-feedback behavior is deliberately independent.
"""
import math

import numpy as np

VERSION = 'measured-exposure-refinement-v1'
THRESHOLDS = {'tone_abs_ev': .08, 'tail_positive_ev': .02,
              'clip_fraction_increase': .001, 'adjacent_tone_residual_ev': .06}


def _vector(value, size, name):
    result = np.asarray(value, dtype=float)
    if result.shape != (size,) or not np.all(np.isfinite(result)):
        raise ValueError(f'{name} must contain {size} finite numbers')
    return result


def _clips(value, name):
    result = _vector(value, 3, name)
    if np.any(result < 0) or np.any(result > 1):
        raise ValueError(f'{name} must contain fractions in [0, 1]')
    return result


def _neighbors(value):
    result = np.asarray(value, dtype=float)
    if not result.size:
        return np.empty((0, 3))
    if result.ndim != 2 or result.shape[0] > 2 or result.shape[1] not in (3, 6) or not np.all(np.isfinite(result)):
        raise ValueError('neighbor_residuals must contain up to two finite 3- or 6-band residuals')
    return result[:, :3]


def assess(observation, target, reference_clip, neighbor_residuals=()):
    """Apply unchanged limits to a real observation and fixed neighboring errors.

    Neighbor residuals are actual brightness minus that neighbor's own frozen
    target, not brightness itself. The caller supplies applicable neighbors.
    No input is a prediction and this function never changes the target.
    """
    brightness = _vector(observation['brightness'], 6, 'brightness')
    target = _vector(target, 6, 'target')
    clip = _clips(observation['channel_clip_fraction'], 'channel_clip_fraction')
    reference = _clips(reference_clip, 'reference_clip')
    neighbors = _neighbors(neighbor_residuals)
    error = brightness-target
    increase = clip-reference
    adjacent = np.abs(error[:3]-neighbors)
    normalized = {
        'tone': np.abs(error[:3]) / THRESHOLDS['tone_abs_ev'],
        'tail': np.maximum(error[3:], 0) / THRESHOLDS['tail_positive_ev'],
        'clip': np.maximum(increase, 0) / THRESHOLDS['clip_fraction_increase'],
        'adjacent': adjacent / THRESHOLDS['adjacent_tone_residual_ev'],
    }
    all_values = np.concatenate([v.ravel() for v in normalized.values()])
    # Acceptance compares the original quantities, with no tolerance added.
    violations = {
        'tone': np.flatnonzero(np.abs(error[:3]) > THRESHOLDS['tone_abs_ev']).tolist(),
        'tail': np.flatnonzero(error[3:] > THRESHOLDS['tail_positive_ev']).tolist(),
        'clip': np.flatnonzero(clip > reference+THRESHOLDS['clip_fraction_increase']).tolist(),
        'adjacent': np.argwhere(adjacent > THRESHOLDS['adjacent_tone_residual_ev']).tolist(),
    }
    return {'accepted': not any(violations.values()), 'violations': violations,
            'tone_residual': error[:3].tolist(), 'tail_residual': error[3:].tolist(),
            'clip_increase': increase.tolist(), 'adjacent_residual': adjacent.tolist(),
            'normalized': {k: v.tolist() for k, v in normalized.items()},
            'normalized_max': float(np.max(all_values)),
            'squared_excess': float(np.sum(np.maximum(all_values-1, 0)**2))}


def _controls(value):
    values = np.asarray(value, dtype=float)
    if values.shape not in ((3,), (4,)) or not np.all(np.isfinite(values)):
        raise ValueError('controls must be [Exposure, Highlights, Whites, optional RGB white ceiling]')
    return values


def _bounds(baseline_exposure):
    if not isinstance(baseline_exposure, (int, float)) or not math.isfinite(baseline_exposure) or not -5 <= baseline_exposure <= 5:
        raise ValueError('baseline_exposure must be finite and in [-5, 5]')
    return max(-5., baseline_exposure-2), min(5., baseline_exposure+2)


def quantize_controls(controls, baseline_exposure):
    """Return an exportable bounded candidate; optional RGB ceiling is 220..255.

    This is only candidate construction. It must not rewrite controls in measured
    history: imported source exposures can legitimately have finer precision.
    """
    values = _controls(controls).copy()
    low, high = _bounds(baseline_exposure)
    # Round inward at a non-grid cumulative bound, never beyond +/-2 EV.
    low_tick = math.ceil(low*100-1e-10)
    high_tick = math.floor(high*100+1e-10)
    values[0] = min(high_tick, max(low_tick, round(values[0]*100)))/100
    values[1:3] = np.clip(np.rint(values[1:3]), -100, 100)
    if len(values) == 4:
        values[3] = min(255, max(220, round(values[3])))
    return values.tolist()


def _recipe_key(controls):
    # Omitted RGB curve ceiling denotes the same linear recipe as explicit 255.
    return tuple(controls[1:3])+(controls[3] if len(controls) == 4 else 255.,)


def _rank(assessment):
    return (not assessment['accepted'], max(assessment['normalized_max']-1, 0),
            assessment['squared_excess'], sum(v*v for v in assessment['tone_residual']))


def suggest(history, target, reference_clip, baseline_exposure, neighbor_residuals=(),
            max_step=.2, stagnation_patience=4):
    """Suggest at most one real-render experiment, changing exposure only.

    Each history row contains controls, brightness (six EV values), and
    channel_clip_fraction (three fractions). The best actually measured recipe
    selects Highlights/Whites/optional curve ceiling; only measurements of that
    exact recipe may supply a secant or bracket. Brackets take precedence over
    extrapolation. There is no assumed one-EV slider-to-image response.
    """
    low, high = _bounds(baseline_exposure)
    if not isinstance(max_step, (int, float)) or not math.isfinite(max_step) or not .01 <= max_step <= 1:
        raise ValueError('max_step must be in [.01, 1] EV')
    if not isinstance(stagnation_patience, int) or stagnation_patience < 2:
        raise ValueError('stagnation_patience must be an integer >= 2')
    target = _vector(target, 6, 'target')
    neighbors = _neighbors(neighbor_residuals)
    if not history:
        raise ValueError('at least one actual measured observation is required')
    controls, assessments = [], []
    for row in history:
        control = _controls(row['controls'])
        if not low <= control[0] <= high or np.any(np.abs(control[1:3]) > 100):
            raise ValueError('measured controls violate the original control bounds')
        if np.any(control[1:3] != np.rint(control[1:3])) or (len(control) == 4 and
                (not 220 <= control[3] <= 255 or control[3] != round(control[3]))):
            raise ValueError('measured Highlights, Whites and optional RGB ceiling must be legal integers')
        controls.append(control)
        assessments.append(assess(row, target, reference_clip, neighbors))
    best = min(range(len(history)), key=lambda i: _rank(assessments[i]))
    anchor = controls[best]
    result = {'status': 'needs-render', 'candidate': None, 'needs_render': False,
              'measured_best_index': best, 'assessment': assessments[best],
              'reason': '', 'bracket': None}

    def stop(status, reason):
        return {**result, 'status': status, 'reason': reason}

    if assessments[best]['accepted']:
        return stop('accepted-measurement', 'An actual measured observation satisfies every supplied constraint.')
    # With fixed neighbors, a disjoint allowed interval cannot be solved by
    # changing this frame alone; do not silently move the frozen target.
    allowed_low, allowed_high = np.full(3, -.08), np.full(3, .08)
    for neighbor in neighbors:
        allowed_low = np.maximum(allowed_low, neighbor-.06)
        allowed_high = np.minimum(allowed_high, neighbor+.06)
    if np.any(allowed_low > allowed_high):
        return stop('neighbors-incompatible', 'Fixed neighbor residuals require incompatible per-frame tone residuals.')
    desired_mid = float(np.clip(0., allowed_low[0], allowed_high[0]))
    family = [i for i, c in enumerate(controls) if _recipe_key(c) == _recipe_key(anchor)]
    # Keep duplicate measured points for validation but never count them as
    # evidence of progress or a second independent exposure response.
    unique = {}
    for i in family:
        exposure = float(controls[i][0])
        if exposure in unique:
            previous = unique[exposure]
            if np.max(np.abs(np.asarray(history[i]['brightness'])-history[previous]['brightness'])) > .04:
                return stop('inconsistent-measurement', 'The same exact recipe did not reproduce its measured brightness.')
        unique[exposure] = i
    measured = sorted(unique)
    residual = {e: assessments[unique[e]]['tone_residual'][0]-desired_mid for e in measured}
    anchor_error = assessments[best]['tone_residual'][0]-desired_mid
    # This is a refinement aim, not a weakened acceptance criterion: failure of
    # another constraint remains explicit in the returned assessment.
    if abs(anchor_error) <= .01:
        return stop('needs-other-controls', 'Midtone is near its allowed aim but the full strict assessment still fails.')
    chronological = sorted(unique.values())
    if len(chronological) > stagnation_patience:
        old = chronological[:-stagnation_patience]
        recent = chronological[-stagnation_patience:]
        old_rank = min(_rank(assessments[i]) for i in old)
        recent_rank = min(_rank(assessments[i]) for i in recent)
        old_mid = min(abs(assessments[i]['tone_residual'][0]-desired_mid) for i in old)
        recent_mid = min(abs(assessments[i]['tone_residual'][0]-desired_mid) for i in recent)
        if recent_rank >= old_rank and recent_mid >= old_mid-1e-4:
            return stop('stalled', 'Distinct recent measured exposures improve neither constraint violations nor midtone error.')
    brackets = [(a, b) for a, b in zip(measured, measured[1:])
                if residual[a] <= 0 <= residual[b] and residual[b] > residual[a]]
    search_low, search_high = max(low, anchor[0]-max_step), min(high, anchor[0]+max_step)
    if brackets:
        a, b = min(brackets, key=lambda pair: pair[1]-pair[0])
        result['bracket'] = [a, b]
        proposal = a-residual[a]*(b-a)/(residual[b]-residual[a])
        if not a+.1*(b-a) <= proposal <= b-.1*(b-a):
            proposal = (a+b)/2
        search_low, search_high = max(search_low, a), min(search_high, b)
        reason = 'Measured sign-changing bracket; safeguarded secant/bisection, bounded by max_step.'
    elif len(measured) >= 2:
        close = sorted(measured, key=lambda e: abs(e-anchor[0]))[:2]
        a, b = sorted(close)
        slope = (residual[b]-residual[a])/(b-a)
        if slope <= 0:
            return stop('inconsistent-response', 'Measured exposure response is flat or reversed; no supported secant step.')
        proposal = anchor[0]-anchor_error/slope
        reason = 'Secant from actual same-recipe measurements; extrapolation requires a new render.'
    else:
        proposal = anchor[0]-math.copysign(max_step, anchor_error)
        reason = 'First bounded exposure response probe; no image-response slope has been assumed.'
    if search_low > search_high:
        return stop('stalled', 'The measured bracket is outside the allowed bounded step.')
    proposal = float(np.clip(proposal, search_low, search_high))
    ticks = range(math.ceil(search_low*100-1e-10), math.floor(search_high*100+1e-10)+1)
    untested = [tick/100 for tick in ticks if tick/100 not in unique and abs(tick/100-anchor[0]) > 1e-10]
    if not brackets:
        untested = [e for e in untested if (e-anchor[0])*anchor_error < 0]
    if not untested:
        return stop('stalled', 'No unmeasured legal exposure remains in the supported direction or bracket.')
    exposure = min(untested, key=lambda e: (abs(e-proposal), abs(e-anchor[0])))
    candidate = anchor.copy()
    candidate[0] = exposure
    result.update(candidate=candidate.tolist(), needs_render=True, reason=reason)
    return result
