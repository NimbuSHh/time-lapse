"""Time-aware camera-step removal and explicit output-range trajectories.

This new automatic-workflow contract is independent of the manual six-feature
target solver. A positive affine range mapping is an output choice, not a claim
to recover physical scene illumination. No rendering or source writes occur.
"""
import hashlib
import json

import numpy as np

from .analysis import camera_ev, capture_time, robust_line
from . import trajectory_regions

LEGACY_VERSION = 'time-aware-destep-affine-v3'
VERSION = 'time-aware-destep-affine-v4'
# Retained solely for already-frozen v3 plans; new plans always use an
# explicit scene-independent trajectory_regions contract.
REGIONS = ('sky', 'sea', 'buildings', 'sunrise')
TARGET_REGIONS = REGIONS[:3]
REGION_ROIS = {'sky': [.1, .1, .9, .3], 'sea': [.05, .6, .4, .85],
               'buildings': [.7, .55, .9, .8], 'sunrise': [.3, .4, .65, .46]}
WINDOW = 31
CRITERIA = {'mid_error_ev': .04, 'adjacent_mid_residual_ev': .04,
            'clip_increase': .001, 'regional_window_error_ev': .08}
EVENT_CRITERIA = {'minimum_stable_samples': 5, 'stable_window_samples': 8,
                  'max_noise_ev': .15, 'already_compensated_ev': .06,
                  'minimum_camera_gain': .4, 'maximum_camera_gain': 1.8,
                  'minimum_primary_region_support': 2,
                  'maximum_cluster_span_frames': 12}


def _array(value, shape, name):
    result = np.asarray(value, dtype=float)
    if result.shape != shape or not np.all(np.isfinite(result)):
        raise ValueError(f'{name}: expected finite array of shape {shape}')
    return result


def _hash(plan):
    content = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _regions(rows, n, names=REGIONS):
    if len(rows) != n:
        raise ValueError('regional_observations must cover the complete frozen sequence')
    try:
        return {name: _array([r[name] for r in rows], (n,), name) for name in names}
    except KeyError as exc:
        raise ValueError(f'missing fixed-region observation: {exc.args[0]}') from exc


def _profile(values, times, indices, start, stop):
    first, last = indices[0], indices[-1]
    slopes, noises = [], []
    at = times[first]
    for a, b in ((start, first), (last, stop)):
        offset, noise = robust_line(times[a:b], values[a:b], at)
        next_offset, _ = robust_line(times[a:b], values[a:b], at+1)
        slopes.append(next_offset-offset)
        noises.append(noise)
    slope = float(np.mean(slopes))
    detrended = values-slope*(times-at)
    cuts = [start]+indices+[stop]
    levels = [float(np.median(detrended[a:b])) for a, b in zip(cuts, cuts[1:])]
    cadence = float(np.median(np.diff(times[start:stop])))
    return {'jumps': np.diff(levels).tolist(), 'natural_slope_ev_per_second': slope,
            'noise_ev': float(max(.01, 3*sum(noises))),
            'slope_disagreement_ev': float(abs(slopes[0]-slopes[1])*(times[last]-times[first]+cadence)),
            'left_slope_ev_per_second': slopes[0], 'right_slope_ev_per_second': slopes[1]}


def _smooth(values, times, boundaries, window=WINDOW):
    result = values.copy()
    cuts = [0]+sorted(set(boundaries))+[len(values)]
    for start, stop in zip(cuts, cuts[1:]):
        width = min(window, stop-start)
        if width < 3:
            continue
        for i in range(start+1, stop-1):
            a = max(start, min(i-width//2, stop-width))
            result[i] = robust_line(times[a:a+width], values[a:a+width], times[i])[0]
    return result


def _trajectory(values, times, boundaries, steps, *, general=False):
    natural = values+steps
    smoothed = _smooth(natural, times, boundaries)
    denominator = float(smoothed[-1]-smoothed[0])
    numerator = float(values[-1]-values[0])
    if general and (abs(denominator) <= .02 or numerator/denominator <= 0
                    or not .1 <= numerator/denominator <= 4):
        # Equal endpoints, nearly constant light, or a camera change larger
        # than the natural trend do not identify a sensible endpoint affine
        # range. Preserve natural amplitude and the first frame's level.
        return natural, values[0]+smoothed-smoothed[0], 1.
    if abs(denominator) <= 1e-8:
        raise ValueError('zero de-stepped endpoint span: affine output range is not identifiable')
    scale = numerator/denominator
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError(f'non-positive affine output scale {scale}: output endpoint choice is incompatible with the de-stepped sequence')
    target = values[0]+(smoothed-smoothed[0])*scale
    target[0], target[-1] = values[0], values[-1]
    return natural, target, float(scale)


def build_plan(base_manifest, regional_observations, region_contract=None):
    """Freeze a trajectory from baseline measurements and independent spatial regions.

    Event and gap indices are zero-based first frames AFTER the discontinuity.
    Missing spatial evidence is an error, including for synthetic callers: tests
    must supply the same explicit region schema as production. Passing a frozen
    region_contract uses the generic v4 method. Omission is a legacy v3 adapter.
    """
    general = region_contract is not None
    contract = trajectory_regions.validate(region_contract) if general else None
    names = trajectory_regions.NAMES if general else REGIONS
    target_names = names if general else TARGET_REGIONS
    primary_names = ('primary', 'secondary') if general else ('sky', 'sea')
    n = len(base_manifest['frames'])
    if n < 5:
        raise ValueError('trajectory planning requires at least five frames')
    frames = base_manifest['frames']
    brightness = _array([o['brightness'] for o in base_manifest['observations']], (n, 6), 'brightness')
    clips = _array([o['channel_clip_fraction'] for o in base_manifest['observations']], (n, 3), 'reference_clips')
    if np.any((clips < 0) | (clips > 1)):
        raise ValueError('reference clips must be fractions in [0,1]')
    times = _array([capture_time(f['identity']) for f in frames], (n,), 'capture times')
    if np.any(np.diff(times) <= 0):
        raise ValueError('capture times must be strictly increasing')
    cameras = _array([camera_ev(f['identity']) for f in frames], (n,), 'camera exposure metadata')
    controls = _array([f['original_controls'] for f in frames], (n, 3), 'original_controls')
    regions = _regions(regional_observations, n, names)
    y = brightness[:, 0]
    cadence = float(np.median(np.diff(times)))
    gaps = (np.flatnonzero(np.diff(times) > 2.5*cadence)+1).tolist()
    camera_events = (np.flatnonzero(np.abs(np.diff(cameras)) > .1)+1).tolist()
    if sorted(set(base_manifest.get('camera_events', camera_events))) != camera_events:
        raise ValueError('recorded camera events disagree with original capture metadata')
    protected = sorted(set(gaps+base_manifest.get('boundaries', [])))
    if any(type(i) is not int or not 0 < i < n for i in protected):
        raise ValueError('invalid protected boundary')
    groups = []
    for index in camera_events:
        if groups and index-groups[-1][-1] < 3:
            groups[-1].append(index)
        else:
            groups.append([index])
    boundaries = sorted(set(protected+camera_events))
    events, steps = [], np.zeros(n)
    regional_steps = {name: np.zeros(n) for name in target_names}
    for indices in groups:
        first, last = indices[0], indices[-1]
        if any(first <= gap <= last for gap in protected):
            raise ValueError(f'camera event at frame {first+1} intersects a time gap or protected edit; step is not identifiable')
        start = max([b for b in boundaries if b < first]+[0, first-8])
        stop = min([b for b in boundaries if b > last]+[n, last+8])
        if first-start < 5 or stop-last < 5 or last-first > 12:
            raise ValueError(f'camera event at frame {first+1} lacks five stable samples on each side or spans over 12 frames')
        cuts = [start]+indices+[stop]
        if any(np.ptp(cameras[a:b]) > .06 for a, b in zip(cuts, cuts[1:])):
            raise ValueError(f'camera event at frame {first+1} has unstable supporting camera plateaus')
        mid = _profile(y, times, indices, start, stop)
        spatial = {name: _profile(values, times, indices, start, stop) for name, values in regions.items()}
        camera_jumps = np.array([cameras[i]-cameras[i-1] for i in indices])
        jumps = np.asarray(mid['jumps'])
        small = bool(np.all(abs(jumps) <= max(.06, 2*mid['noise_ev'])) and all(
            np.all(abs(np.asarray(spatial[name]['jumps'])) <= max(.06, 2*spatial[name]['noise_ev']))
            for name in primary_names))
        event = {'indices': indices, 'frames': [i+1 for i in indices], 'window': [start, stop],
                 'time_window': [float(times[start]), float(times[stop-1])],
                 'camera_jumps': camera_jumps.tolist(), 'jumps': mid['jumps'], 'noise': mid['noise_ev'],
                 'mid': mid, 'regions': spatial, 'status': 'already-compensated' if small else 'applied'}
        # Existing source adjustments can explain an absent camera step. They
        # cannot justify automatically removing a nonzero creative discontinuity.
        if not small and np.any(np.ptp(controls[start:stop], axis=0) > .06):
            raise ValueError(f'camera event at frame {first+1} overlaps changing original controls; residual is not camera-only evidence')
        gain = float(sum(jumps)/sum(camera_jumps)) if abs(sum(camera_jumps)) > .2 else None
        if gain is None and not small:
            references = [e['response_gain'] for e in events if e['status'] == 'applied' and e.get('response_gain') is not None]
            gain = float(np.median(references)) if references else None
        expected = camera_jumps*gain if gain is not None else np.full(len(indices), np.nan)
        support = {name: bool(np.all(np.asarray(spatial[name]['jumps'])*camera_jumps > 0)
                              and spatial[name]['noise_ev'] < .15)
                   for name in target_names}
        checks = {'noise': mid['noise_ev'] < .15,
                  'trend': mid['slope_disagreement_ev'] < max(.08, 2*mid['noise_ev']),
                  'direction': bool(np.all(jumps*camera_jumps > 0)),
                  'gain': gain is not None and .4 <= gain <= 1.8,
                  'components': bool(np.all(abs(jumps-expected) < np.maximum(.15, abs(expected)*.25)+mid['noise_ev'])),
                  'primary_regions': all(support[name] for name in primary_names)}
        event.update(response_gain=gain, spatial_support=support, checks=checks)
        if not small and not all(checks.values()):
            failed = [key for key, passed in checks.items() if not passed]
            raise ValueError(f'camera event at frame {first+1} has insufficient evidence: {failed}; noise={mid["noise_ev"]:.5f}, gain={gain}, spatial={support}')
        if small and (mid['noise_ev'] >= .15 or any(spatial[name]['noise_ev'] >= .15 for name in primary_names)):
            raise ValueError(f'camera event at frame {first+1} is too noisy to classify as already compensated')
        if not small:
            for j, index in enumerate(indices):
                steps[index:] -= jumps[j]
                for name in target_names:
                    regional_steps[name][index:] -= spatial[name]['jumps'][j]
        events.append(event)
    natural, target, scale = _trajectory(y, times, protected, steps, general=general)
    regional_targets, regional_natural, regional_scales = {}, {}, {}
    for name in target_names:
        rnatural, rtarget, rscale = _trajectory(regions[name], times, protected, regional_steps[name], general=general)
        regional_targets[name], regional_natural[name], regional_scales[name] = rtarget.tolist(), rnatural.tolist(), rscale
    plan = {'schema': 1, 'version': VERSION if general else LEGACY_VERSION, 'frame_count': n, 'ready': True,
            'criteria': dict(CRITERIA), 'event_criteria': dict(EVENT_CRITERIA),
            'smoothing_window': WINDOW, 'observed_mid': y.tolist(), 'target_mid': target.tolist(),
            'natural_mid': natural.tolist(), 'step_correction_mid': steps.tolist(),
            'times': times.tolist(), 'camera_events': camera_events, 'gaps': gaps,
            'protected_boundaries': protected, 'events': events, 'scale': scale,
            'endpoints': {'observed': [float(y[0]), float(y[-1])],
                          'de_stepped': [float(natural[0]), float(natural[-1])]},
            'regional_observations': {name: values.tolist() for name, values in regions.items()},
            'regional_targets': regional_targets, 'regional_natural': regional_natural,
            'regional_scales': regional_scales, 'reference_clips': clips.tolist(),
            'regional_measurement_contract': {'method': 'fixed-roi-linear-luminance-mean-log2',
                                              'rois': {name: list(roi) for name, roi in REGION_ROIS.items()},
                                              'per_frame_pixel_reselection': False},
            'source_target_sha256': base_manifest.get('target_sha256'),
            'interpretation': 'positive affine display-range mapping of measured de-stepped brightness; not physical scene-light recovery',
            'sunrise_role': 'diagnostic only; never supplies exposure target or step evidence',
            'visual_playback_verified': False}
    if general:
        plan['regional_measurement_contract'] = contract
        plan['region_names'] = list(names)
        plan['target_region_names'] = list(target_names)
        plan['primary_region'] = 'primary'
        plan['range_policy'] = 'endpoint-affine-when-identifiable-otherwise-natural-amplitude'
        plan.pop('sunrise_role')
    plan['plan_sha256'] = _hash(plan)
    return plan


def _target_integrity(plan):
    general = plan['version'] == VERSION
    target_names = trajectory_regions.NAMES if general else TARGET_REGIONS
    n = plan['frame_count']
    y = _array(plan['observed_mid'], (n,), 'observed_mid')
    times = _array(plan['times'], (n,), 'times')
    reconstructed = np.zeros(n)
    for event in plan['events']:
        if event['status'] == 'applied':
            for i, jump in zip(event['indices'], event['jumps']):
                reconstructed[i:] -= jump
    natural, expected, scale = _trajectory(y, times, plan['protected_boundaries'], reconstructed, general=general)
    deviation = float(np.max(abs(expected-np.asarray(plan['target_mid']))))
    natural_error = float(np.max(abs(natural-np.asarray(plan['natural_mid']))))
    regional_errors = {}
    for name in target_names:
        regional_steps = np.zeros(n)
        for event in plan['events']:
            if event['status'] == 'applied':
                for i, jump in zip(event['indices'], event['regions'][name]['jumps']):
                    regional_steps[i:] -= jump
        regional_values = _array(plan['regional_observations'][name], (n,), name)
        rnatural, rtarget, rscale = _trajectory(regional_values, times, plan['protected_boundaries'], regional_steps, general=general)
        regional_errors[name] = float(max(np.max(abs(rtarget-np.asarray(plan['regional_targets'][name]))),
                                          np.max(abs(rnatural-np.asarray(plan['regional_natural'][name]))),
                                          abs(rscale-plan['regional_scales'][name])))
    return {'valid': deviation <= 1e-10 and natural_error <= 1e-10 and abs(scale-plan['scale']) <= 1e-10
                     and max(regional_errors.values()) <= 1e-10,
            'reconstructed_target_max_error_ev': deviation, 'natural_mid_max_error_ev': natural_error,
            'regional_construction_error': regional_errors,
            'method': 'reconstruct camera-step removal and positive affine mapping independently of rendered output'}


def review_sequence(plan, observations):
    """Review full-sequence numeric conditions, never claim visual acceptance.

    Every row is a real measurement with brightness[6], channel_clip_fraction[3]
    and the region keys frozen in the plan. Primary event windows are
    independent spatial checks; remaining regions are explicit diagnostics.
    Legacy v3 names and review fields are preserved for existing previews.
    """
    if (plan.get('version') not in (VERSION, LEGACY_VERSION) or plan.get('criteria') != CRITERIA
            or plan.get('smoothing_window') != WINDOW or plan.get('plan_sha256') != _hash(plan)):
        raise ValueError('trajectory plan version, criteria or content hash mismatch')
    general = plan['version'] == VERSION
    names = trajectory_regions.NAMES if general else REGIONS
    target_names = names if general else TARGET_REGIONS
    primary = 'primary' if general else 'sky'
    diagnostic = 'check_b' if general else 'sunrise'
    if general:
        trajectory_regions.validate(plan['regional_measurement_contract'])
    n = plan['frame_count']
    if len(observations) != n:
        raise ValueError('sequence review requires every frozen frame')
    brightness = _array([o['brightness'] for o in observations], (n, 6), 'brightness')
    clips = _array([o['channel_clip_fraction'] for o in observations], (n, 3), 'channel_clip_fraction')
    if np.any((clips < 0) | (clips > 1)):
        raise ValueError('channel clip fractions must be in [0,1]')
    regions = _regions([o['regions'] for o in observations], n, names)
    target = _array(plan['target_mid'], (n,), 'target_mid')
    residual = brightness[:, 0]-target
    bad = (np.flatnonzero(abs(residual) > .04)+1).tolist()
    adjacent = abs(np.diff(residual))
    bad_pairs = [[int(i+1), int(i+2)] for i in np.flatnonzero(adjacent > .04)]
    clip_bad = (np.flatnonzero(np.any(clips > np.asarray(plan['reference_clips'])+.001, axis=1))+1).tolist()
    integrity = _target_integrity(plan)
    event_windows, primary_failures, multi = [], [], []
    boundaries = plan['protected_boundaries']
    for radius in (15, 31, 61):
        for event in plan['events']:
            first, last = event['indices'][0], event['indices'][-1]
            a, b = max(0, first-radius), min(n-1, last+radius)
            record = {'frames': event['frames'], 'radius': radius, 'window_frames': [a+1, b+1]}
            if any(a < boundary <= b for boundary in boundaries):
                record.update(evaluated=False, reason='window crosses protected gap/edit; no continuity inference')
                event_windows.append(record)
                continue
            regional = {}
            for name in target_names:
                expected = plan['regional_targets'][name][b]-plan['regional_targets'][name][a]
                actual = float(regions[name][b]-regions[name][a])
                regional_error = regions[name][a:b+1]-np.asarray(plan['regional_targets'][name][a:b+1])
                regional[name] = {'actual_change_ev': actual, 'target_change_ev': expected,
                                  'change_error_ev': actual-expected,
                                  'window_residual_range_ev': float(np.ptp(regional_error))}
            # Endpoint contrast alone misses a slow dip followed by recovery
            # wholly inside the window. Compare every relative regional level.
            primary_ok = regional[primary]['window_residual_range_ev'] <= .08
            record.update(evaluated=True, regional=regional, **{('primary_condition_met' if general else 'sky_condition_met'): primary_ok},
                          mid_change_ev=float(brightness[b, 0]-brightness[a, 0]),
                          target_mid_change_ev=float(target[b]-target[a]),
                          mid_change_error_ev=float(residual[b]-residual[a]),
                          **{('diagnostic_change_ev' if general else 'sunrise_change_ev'): float(regions[diagnostic][b]-regions[diagnostic][a])})
            if not primary_ok:
                primary_failures.append({'frames': event['frames'], 'radius': radius,
                                     'change_error_ev': regional[primary]['change_error_ev'],
                                     'window_residual_range_ev': regional[primary]['window_residual_range_ev']})
            event_windows.append(record)
        pairs = [(a, a+radius) for a in range(n-radius)
                 if not any(a < boundary <= a+radius for boundary in boundaries)]
        multi.append({'span_frames': radius, 'pair_count': len(pairs),
                      'max_mid_change_error_ev': max((float(abs(residual[b]-residual[a])) for a, b in pairs), default=None),
                      'minimum_actual_mid_change_ev': min((float(brightness[b, 0]-brightness[a, 0]) for a, b in pairs), default=None),
                      'downward_changes_are_not_failures': True})
    met = integrity['valid'] and not bad and not bad_pairs and not clip_bad and not primary_failures
    adjacent_indices = [i for i in range(1, n) if i not in boundaries]
    bright_diagnostics = {name: {
        'minimum_ev': float(np.min(brightness[:, band])), 'maximum_ev': float(np.max(brightness[:, band])),
        'max_adjacent_change_ev': max((float(abs(brightness[i, band]-brightness[i-1, band])) for i in adjacent_indices), default=None)}
        for band, name in ((1, 'highlight'), (2, 'white'))}
    return {'version': plan['version'], 'frame_count': n, 'numeric_conditions_met': bool(met),
            'visual_playback_verified': False, 'verified': False,
            'criteria': dict(CRITERIA), 'target_integrity': integrity,
            'failed_mid_frames': bad, 'failed_adjacent_pairs': bad_pairs,
            'channel_clipping_worsened_frames': clip_bad,
            ('failed_primary_event_windows' if general else 'failed_sky_event_windows'): primary_failures,
            'max_mid_error_ev': float(np.max(abs(residual))),
            'max_adjacent_mid_residual_ev': float(np.max(adjacent)),
            'bright_band_diagnostics': bright_diagnostics,
            'event_windows': event_windows, 'multiscale': multi,
            'delivery_gate': 'full-sequence low-resolution video requires explicit user approval before full-size export'}
