from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
import numpy as np
from .workspace import output_path
from . import analysis
from .measurement import SequenceMeter, MeasurementError, brightness, comparability
from .xmp import parse

RAW = {'.arw', '.cr2', '.cr3', '.nef', '.nrw', '.orf', '.rw2', '.raf', '.pef', '.srw', '.raw'}
MAX_FRAMES = 5000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_read(path, limit):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f'文件类型或大小不支持: {Path(path).name}')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('读取期间文件超出大小限制')
        return data
    finally:
        os.close(fd)


def directory(value):
    p = Path(value).expanduser().absolute()
    if not p.is_dir() or p.is_symlink():
        raise ValueError('请输入真实目录路径，不支持符号链接目录')
    return p.resolve()


def scan(folder, extensions):
    found = {}
    for path in folder.iterdir():
        if path.suffix.lower() not in extensions:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'不支持符号链接或特殊文件: {path.name}')
        key = path.stem.casefold()
        if key in found:
            raise ValueError(f'同名或大小写冲突: {path.name} / {found[key].name}')
        found[key] = path
        if len(found) > MAX_FRAMES:
            raise ValueError(f'单个序列最多 {MAX_FRAMES} 张')
    return found


def natural(name):
    return [(1, int(p)) if p.isdigit() else (0, p.casefold()) for p in re.split(r'(\d+)', name)]


@dataclass
class Frame:
    name: str
    raw: Path
    xmp: Path
    jpeg: Path
    raw_stat: tuple
    source: object
    jpeg_hash: str
    measure: dict


class Project:
    def __init__(self, raw_dir, preview_dir, roi=(0., 0., 1., 1.), mode='manual', srgb=False, progress=None):
        # Legacy srgb argument remains accepted; a checkbox is not evidence.
        # The decoder checks the actual JPEG profile, format and dimensions.
        if mode not in ('manual', 'visual'):
            raise ValueError('不支持的分析模式')
        if len(roi) != 4 or not all(np.isfinite(v) for v in roi) or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
            raise ValueError('ROI 必须为 0–1 的左、上、右、下坐标')
        self.raw_dir, self.preview_dir = directory(raw_dir), directory(preview_dir)
        if any(p.suffix.lower() == '.dng' for p in self.raw_dir.iterdir()):
            raise ValueError('首版不支持 DNG 内嵌设置；请使用保存独立 XMP 的相机 RAW 序列')
        raws, xmps, jpegs = scan(self.raw_dir, RAW), scan(self.raw_dir, {'.xmp'}), scan(self.preview_dir, {'.jpg', '.jpeg'})
        if not 5 <= len(raws) <= MAX_FRAMES:
            raise ValueError(f'需要 5–{MAX_FRAMES} 张相机 RAW 与独立 XMP')
        if raws.keys() != xmps.keys() or raws.keys() != jpegs.keys():
            mismatch = sorted((raws.keys() ^ xmps.keys()) | (raws.keys() ^ jpegs.keys()))[:8]
            raise ValueError('RAW / XMP / JPEG 必须按同名一一对应；不匹配: ' + ', '.join(mismatch))
        self.id = uuid.uuid4().hex
        self.roi, self.mode, self.frames = list(roi), mode, []
        measures, exposures, cameras, times = [], [], [], []
        total = 0
        dimensions = None
        render_contract = None
        with SequenceMeter(roi) as meter:
            for i, key in enumerate(sorted(raws, key=natural)):
                xdata = safe_read(xmps[key], 4 * 1024 * 1024)
                total += len(xdata)
                if total > 64 * 1024 * 1024:
                    raise ValueError('全部 XMP 合计不能超过 64 MB')
                try:
                    source = parse(xdata)
                    version = analysis.numeric(source.metadata.get('ProcessVersion'))
                    if version is None or version < 6.7:
                        raise ValueError('需要支持 Exposure2012 的现代 ACR ProcessVersion')
                    from .provenance import _scalar
                    contract = {k: _scalar(v) for k, v in source.metadata.items() if k == 'ProcessVersion' or k.startswith(('Crop', 'Perspective'))}
                    if render_contract is not None and contract != render_contract:
                        raise ValueError('处理版本、裁切或透视设置跨帧不一致，固定参考区域无法比较')
                    render_contract = contract
                    jpg = safe_read(jpegs[key], 25 * 1024 * 1024)
                    measured = meter.add(jpg)
                except Exception as e:
                    raise ValueError(f'{raws[key].name}: {e}') from e
                if dimensions is not None and measured['dimensions'] != dimensions:
                    raise ValueError('JPEG 画幅尺寸不一致；请用同样裁切与导出尺寸重新生成')
                dimensions = measured['dimensions']
                metadata = {**measured['metadata'], **source.metadata}
                info = raws[key].stat()
                self.frames.append(Frame(raws[key].name, raws[key], xmps[key], jpegs[key], (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns), source, digest(jpg), measured))
                measures.append(measured)
                exposures.append(source.exposure)
                cameras.append(analysis.camera_ev(metadata))
                times.append(analysis.capture_time(metadata))
                if progress:
                    progress(i + 1, len(raws), 'analysis')
            self.exposures = exposures
            if progress:
                progress(len(self.frames), len(self.frames), 'finalize')
            try:
                measures, diagnostics, contract = meter.finish()
            except MeasurementError as e:
                for row, frame in zip(e.measurement['frames'], self.frames):
                    row['name'] = frame.jpeg.name
                raise
        for row, frame, measured in zip(diagnostics['frames'], self.frames, measures):
            row['name'] = frame.jpeg.name
            frame.measure = measured
        self.report = analysis.infer(measures, exposures, cameras, times, mode, diagnostics, contract)
        self.result = None
        self.exports = {}
        self.verified_images = None
        self.exposure_calibrations = {}
        self.exposure_response = None

    def public(self):
        return {'id': self.id, 'roi': self.roi, 'mode': self.mode, 'report': self.report,
                'frames': [{'name': f.name, 'exposure': f.source.exposure, 'clipped': f.measure['clipped'], 'dark': f.measure['dark']} for f in self.frames]}

    def calculate(self, settings):
        self.result = analysis.correct(self.report, self.exposures, **settings)
        if self.exposure_response is not None:
            from .exposure_calibration import apply_response
            self.result = apply_response(self.result, self.exposures, self.exposure_response)
        self.result['revision'] = digest(json.dumps(self.result, sort_keys=True).encode())
        return self.result

    def verification_target(self):
        """Freeze measurement evidence without validating or exporting XMP."""
        if not self.result:
            raise ValueError('尚无分析目标，请分析序列并更新预览')
        return {'snapshot_id': self.id, 'raw_dir': str(self.raw_dir),
                'preview_dir': str(self.preview_dir), 'roi': self.roi,
                'report': self.report, 'result': self.result,
                'dimensions': list(self.frames[0].measure['dimensions']),
                'frames': [{'name': f.name, 'xmp': f.xmp.name, 'preview': f.jpeg.name,
                            'source_xmp_sha256': digest(f.source.data),
                            'preview_sha256': f.jpeg_hash} for f in self.frames],
                'public': self.public(),
                'checkpoint': {'version': 1, 'written': getattr(self, 'written', False),
                    'frames': [{'xmp': base64.b64encode(f.source.data).decode(),
                                'raw_stat': list(f.raw_stat),
                                'measure': {k: f.measure[k] for k in ('dimensions', 'clipped', 'dark')}} for f in self.frames],
                    'exposure_response': self.exposure_response,
                    'exposure_calibrations': {key: {**value, 'path': str(value['path'])}
                                             for key, value in self.exposure_calibrations.items()}}}

    @classmethod
    def restore(cls, target):
        """Restore recorded evidence only; all write/calibration paths still check unchanged()."""
        checkpoint = target.get('checkpoint')
        if not checkpoint:
            return None  # Earlier saved goals remain reviewable without inventing source evidence.
        if checkpoint.get('version') != 1 or len(checkpoint['frames']) != len(target['frames']):
            raise ValueError('保存的分析来源记录不完整')
        p = cls.__new__(cls)
        p.id, p.roi = target['snapshot_id'], target['roi']
        p.raw_dir, p.preview_dir = Path(target['raw_dir']), Path(target['preview_dir'])
        p.mode = target['public']['mode']
        p.report, p.result = target['report'], target['result']
        p.frames = []
        for frame, stored in zip(target['frames'], checkpoint['frames']):
            if any(Path(frame[key]).name != frame[key] or '\\' in frame[key] for key in ('name', 'xmp', 'preview')):
                raise ValueError('保存的分析来源文件名无效')
            data = base64.b64decode(stored['xmp'], validate=True)
            if digest(data) != frame['source_xmp_sha256']:
                raise ValueError('保存的原 XMP 校验失败')
            p.frames.append(Frame(frame['name'], p.raw_dir / frame['name'], p.raw_dir / frame['xmp'],
                                  p.preview_dir / frame['preview'], tuple(stored['raw_stat']), parse(data),
                                  frame['preview_sha256'], {**stored['measure'],
                                      'dimensions': tuple(stored['measure']['dimensions'])}))
        p.exposures = [f.source.exposure for f in p.frames]
        p.exposure_response = checkpoint.get('exposure_response')
        p.exposure_calibrations = {key: {**value, 'path': Path(value['path'])}
                                  for key, value in checkpoint.get('exposure_calibrations', {}).items()}
        p.written = checkpoint.get('written', False)
        p.exports, p.verified_images = {}, None
        return p

    def unchanged(self):
        # This checks the snapshot at validation time, not a lock on external ACR.
        for f in self.frames:
            if directory(f.raw.parent) != self.raw_dir or directory(f.jpeg.parent) != self.preview_dir:
                raise ValueError('来源目录已改变')
            info = f.raw.lstat()
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != f.raw_stat:
                raise ValueError(f'RAW 身份已改变: {f.name}')
            if digest(safe_read(f.xmp, 4 * 1024 * 1024)) != digest(f.source.data) or digest(safe_read(f.jpeg, 25 * 1024 * 1024)) != f.jpeg_hash:
                raise ValueError(f'分析后源文件已改变: {f.name}。请重新分析，不会导出过期修正。')

    def validate_write(self, revision, order_confirmed=None):
        if not self.result or revision != self.result['revision'] or (order_confirmed is not None and order_confirmed != self.id):
            raise ValueError('当前预览已失效，请更新预览后再导出')
        if self.result.get('calibration_required'):
            raise ValueError('缺少亮度到 XMP 曝光的响应依据；当前仅为亮度目标，请先使用显影配对数据校准')
        if self.result['blocked']:
            raise ValueError('曝光超出首版安全范围，请减少修正或调整首尾偏移')
        self.unchanged()

    def export_direct(self, output_dir, revision, order_confirmed=None):
        """Place corrected sidecars directly in the chosen folder, never replace files."""
        output_path(output_dir)
        self.validate_write(revision, order_confirmed)
        root = directory(output_dir)
        names = {p.name.casefold() for p in root.iterdir()}
        conflicts = [f.xmp.name for f in self.frames if f.xmp.name.casefold() in names]
        if conflicts:
            raise ValueError('导出目录已有同名文件，未导出或覆盖：' + '、'.join(conflicts[:5])
                             + '。请选择其他目录；写回原目录请使用“备份 + 导出到原目录”。')
        with tempfile.TemporaryDirectory(prefix='.export-', dir=root) as temp:
            staging = Path(temp)
            published = []
            try:
                for frame, value in zip(self.frames, self.result['final']):
                    with (staging / frame.xmp.name).open('xb') as stream:
                        stream.write(frame.source.updated(value))
                        stream.flush()
                        os.fsync(stream.fileno())
                self.unchanged()
                for frame in self.frames:
                    source, target = staging / frame.xmp.name, root / frame.xmp.name
                    # link is exclusive: a file arriving after preflight cannot
                    # be overwritten, unlike rename/replace on POSIX.
                    os.link(source, target)
                    published.append((source, target))
            except Exception:
                for source, target in reversed(published):
                    try:
                        a, b = source.stat(), target.lstat()
                        if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                            target.unlink()
                    except FileNotFoundError:
                        pass
                raise
        return {'path': str(root), 'count': len(self.frames), 'layout': 'direct'}

    def export(self, output_root, revision, order_confirmed=None, *, allow_source_subdir=False):
        output_path(output_root)
        self.validate_write(revision, order_confirmed)
        root = Path(output_root).absolute().resolve()
        # Validate before creating anything in a user-owned input directory.
        if not allow_source_subdir and (root == self.raw_dir or self.raw_dir in root.parents or root == self.preview_dir or self.preview_dir in root.parents):
            raise ValueError('输出目录不能位于素材目录内')
        root.mkdir(parents=True, exist_ok=True)
        root = directory(root)
        package_id = uuid.uuid4().hex
        staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
        target = root / ('timelapse-' + package_id)
        manifest = {'schema': 1, 'package_id': package_id, 'snapshot_id': self.id, 'synthetic': (self.raw_dir / 'SYNTHETIC.txt').exists(),
                    'notice': '此包属于分析快照；哈希校验不锁定 ACR。仅 Exposure2012 数字被修改。目标为 EV 近似，必须 ACR 重显影复核。',
                    'raw_dir': str(self.raw_dir), 'preview_dir': str(self.preview_dir), 'roi': self.roi, 'mode': self.mode,
                    'report': self.report, 'result': self.result, 'dimensions': list(self.frames[0].measure['dimensions']), 'frames': []}
        try:
            (staging / 'corrected').mkdir()
            (staging / 'originals').mkdir()
            for f, value in zip(self.frames, self.result['final']):
                output = f.source.updated(value)
                for sub, data in (('originals', f.source.data), ('corrected', output)):
                    path = staging / sub / f.xmp.name
                    with path.open('xb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                manifest['frames'].append({'name': f.name, 'xmp': f.xmp.name, 'preview': f.jpeg.name, 'original_exposure': f.source.exposure, 'final_exposure': value,
                                          'source_xmp_sha256': digest(f.source.data), 'preview_sha256': f.jpeg_hash, 'corrected_xmp_sha256': digest(output)})
            with (staging / 'manifest.json').open('x', encoding='utf-8') as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            (staging / '使用说明.txt').write_text('先复制 RAW 和原 XMP 到工作副本目录。关闭 ACR 中的该序列，用 corrected 中的 XMP 替换工作副本中的同名文件，然后重新打开 RAW 并核对曝光值。不要批量同步曝光覆盖逐帧修正。导出同名、同尺寸、同裁切的 sRGB 小图，在本工具复核。originals 为原 XMP 字节备份。不要将 corrected 当作 JPG 或 RAW。\n', encoding='utf-8')
            # Reserve a nonempty target with mkdir(exclusive), so no existing output
            # can ever be replaced, then atomically publish the payload inside it.
            target.mkdir()
            os.rename(staging, target / 'package')
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            if target.exists() and not any(target.iterdir()):
                target.rmdir()
            raise
        self.exports[package_id] = manifest
        return {'package_id': package_id, 'path': str(target / 'package'), 'count': len(self.frames)}

    def verify(self, preview_dir, package_id):
        if package_id not in self.exports:
            raise ValueError('请先导出本次修正，再复核该包对应的 ACR 小图')
        result = verify_manifest(self.exports[package_id], preview_dir)
        self.verified_images = result.pop('_images')
        return result


def verify_manifest(manifest, preview_dir, *, diagnostic=False):
    # Display review can report observed JPEG values and uncertainty. Callers
    # requiring certified residuals retain the strict default; no thresholds,
    # frozen pixels, weights, or targets are changed by diagnostic mode.
    folder = directory(preview_dir)
    jpegs = scan(folder, {'.jpg', '.jpeg'})
    frames = manifest['frames']
    if set(jpegs) != {Path(f['name']).stem.casefold() for f in frames}:
        raise ValueError('复核 JPEG 与原序列未一一对应')
    actual, clip, dark, hashes = [], [], [], []
    invalid = []
    support_checks = []
    dimensions = None
    for i, f in enumerate(frames):
        data = safe_read(jpegs[Path(f['name']).stem.casefold()], 25 * 1024 * 1024)
        m = analysis.measure_for_report(data, manifest['roi'], manifest['report'])
        if list(m['dimensions']) != manifest['dimensions']:
            raise ValueError('复核图像尺寸与原批次不一致；请使用原导出尺寸和裁切')
        dimensions = m['dimensions']
        check = comparability(m, manifest['report'])
        support_checks.append({'frame': i + 1, 'name': f['name'], **check})
        if not check['comparable']:
            invalid.append(i)
        actual.append(brightness(m, manifest['report']))
        clip.append(m['clipped'])
        dark.append(m['dark'])
        hashes.append(digest(data))
    if invalid and not diagnostic:
        raise ValueError(f'复核测量不可比：{len(invalid)} 帧的冻结参考像素出现高光/暗部失效，且无法证明最终测光值不受影响，不能计算残差；冻结目标未改变。前几个帧序号: {[i+1 for i in invalid[:8]]}。请检查这些帧的曝光与导出设置；如需更换参考区域，须重新分析并建立新目标。')
    target = np.array(manifest['result']['target'])
    error = np.array(actual) - target
    uncertain = []
    for i in invalid:
        check = support_checks[i]
        limits = check['brightness_bounds_ev']
        uncertain.append({**check, 'observed_ev': actual[i], 'target_ev': float(target[i]),
                          'residual_ev': float(error[i]),
                          'residual_bounds_ev': [v - float(target[i]) for v in limits] if limits else None,
                          'interval_width_ev': limits[1] - limits[0] if limits else None})
    rmse_bounds = None
    if all(c['brightness_bounds_ev'] is not None for c in support_checks):
        residual_limits = np.array([c['brightness_bounds_ev'] for c in support_checks]) - target[:, None]
        low, high = residual_limits[:, 0], residual_limits[:, 1]
        nearest = np.where((low <= 0) & (high >= 0), 0., np.minimum(np.abs(low), np.abs(high)))
        farthest = np.maximum(np.abs(low), np.abs(high))
        rmse_bounds = [float(np.sqrt(np.mean(v ** 2))) for v in (nearest, farthest)]
    certified_tails = sum(not c['strict_support_valid'] and c['comparable'] for c in support_checks)
    edge = min(8, len(frames) // 2)
    events = []
    by_index = {e['index']: e for e in manifest['report']['events']}
    for i in manifest['result']['settings']['selected']:
        events.append(analysis.event_residual(actual, by_index[i], manifest['report']['boundaries']))
    return {'actual': actual, 'rmse_ev': float(np.sqrt(np.mean(error ** 2))), 'median_bias_ev': float(np.median(error)),
            'measurement_status': 'needs-review' if invalid else 'comparable',
            'total_frames': len(frames), 'comparable_frames': len(frames) - len(invalid),
            'uncertain_frames': uncertain, 'rmse_bounds_ev': rmse_bounds,
            'trend_error_ev': float(np.median(error[-edge:]) - np.median(error[:edge])), 'max_clipped_fraction': max(clip),
            'max_dark_fraction': max(dark), 'events': events, 'measurement_checks': support_checks,
            'unchanged_previews': sum(h == f['preview_sha256'] for h, f in zip(hashes, frames)),
            '_images': [{'path': str(jpegs[Path(f['name']).stem.casefold()]), 'sha256': h} for f,h in zip(frames, hashes)],
            'note': (f'{len(invalid)} 帧未通过严格可比性检查，实测曲线与误差仍可查看；请结合测光区间判断，不能作为已通过校验的自动修正依据。' if invalid else '')
                    + (f'{certified_tails} 帧含失效参考像素，经上下界检查确认不影响最终测光值。' if certified_tails else '')
                    + '像素、权重和目标均未改变。'
                    + '数值仅描述重显影后与冻结近似目标的差异；仍需连续播放判断自然光、色彩与局部细节。'}
