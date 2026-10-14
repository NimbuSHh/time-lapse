"""Content-addressed low-resolution review video; never exports final images.

JPEGs remain immutable. A video is usable only after its ordered source manifest,
source hashes (before and after), video hash and decoded stream metadata agree.
Encoding verification does not grant visual approval or permission to export.
"""
from contextlib import contextmanager
import fcntl
from fractions import Fraction
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageCms

from .photoshop import Cancelled
from .workspace import output_path

VERSION = 'ordered-srgb-preview-v1'
COLOR = {
    'input': 'sRGB JPEG; full-range JPEG YCbCr (BT.601 matrix)',
    'primaries': 'bt709',
    'transfer': 'iec61966-2-1',
    'matrix': 'bt709',
    'range': 'tv',
    'conversion': 'Matrix and range conversion only; sRGB transfer is retained, not converted to BT.709 gamma.',
    'profile_handling': 'Validated source sRGB ICC is replaced by explicit MP4/H.264 primaries, transfer and matrix tags.',
}
_HEX = re.compile(r'^[0-9a-f]{64}$')


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+'\n').encode()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024*1024), b''):
            h.update(data)
    return h.hexdigest()


def _cancelled(check):
    if check and check():
        raise Cancelled('预览视频已停止，已有图像与完成的视频均保留')


def _tools():
    # Frozen apps must use their own matching tools, never another application's
    # binaries or a shell PATH that Finder does not provide.
    bundled = Path(__file__).resolve().parent / 'bin'
    if getattr(sys, 'frozen', False):
        pair = tuple(bundled/name for name in ('ffmpeg', 'ffprobe'))
        if all(p.is_file() and os.access(p, os.X_OK) for p in pair):
            return pair
        raise ValueError('App 内置视频工具不完整，请重新下载或构建 Time-lapse.app')
    candidates = []
    found = shutil.which('ffmpeg')
    if found:
        candidates.append((Path(found), Path(found).with_name('ffprobe')))
    for root in ('/Applications/LosslessCut.app/Contents/Resources',
                 '/Applications/VideoFusion-macOS.app/Contents/Resources'):
        candidates.append((Path(root)/'ffmpeg', Path(root)/'ffprobe'))
    for ffmpeg, ffprobe in candidates:
        if all(p.is_file() and os.access(p, os.X_OK) for p in (ffmpeg, ffprobe)):
            return ffmpeg.resolve(), ffprobe.resolve()
    raise ValueError('未找到可用的 ffmpeg 与 ffprobe；未安装工具或开始编码')


def _jpeg_info(path):
    if path.stat().st_size > 25*1024*1024:
        raise ValueError('预览编码仅接收不超过25 MB的低分辨率JPEG')
    with Image.open(path) as im:
        if im.format != 'JPEG' or im.width*im.height > 24_000_000 or min(im.size) < 2 or max(im.size) > 12000:
            raise ValueError('预览编码需要有效的低分辨率JPEG')
        if im.getexif().get(274, 1) != 1:
            raise ValueError('JPEG仍含旋转标记，请先生成方向确定的显影预览')
        profile = im.info.get('icc_profile')
        if profile:
            name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(BytesIO(profile)))
            if 'srgb' not in name.lower():
                raise ValueError('视频预览需要明确的sRGB JPEG')
        elif im.getexif().get_ifd(34665).get(40961) != 1:
            raise ValueError('视频预览需要明确的sRGB JPEG')
        if im.mode not in ('RGB', 'L'):
            raise ValueError('视频预览不接受CMYK或其他未定义色彩的JPEG')
        return list(im.size)


def _freeze(entries, fps, ffmpeg, ffprobe, cancelled, progress):
    if not isinstance(entries, list) or not entries:
        raise ValueError('预览需要严格有序且非空的帧清单')
    if isinstance(fps, bool):
        raise ValueError('帧率必须为有效正数')
    try:
        rate = Fraction(str(fps)).limit_denominator(1_000_000)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('帧率必须为有效正数') from exc
    if not 0 < rate <= 120:
        raise ValueError('预览帧率必须大于0且不超过120')
    frames, dimensions = [], None
    for i, entry in enumerate(entries):
        _cancelled(cancelled)
        if not isinstance(entry, dict):
            raise ValueError('帧清单格式无效')
        for field in ('sha256', 'recipe_sha256'):
            if not isinstance(entry.get(field), str) or not _HEX.fullmatch(entry[field]):
                raise ValueError('帧清单缺少有效的图像或配方SHA-256')
        if not isinstance(entry.get('jpg'), (str, Path)):
            raise ValueError('帧清单缺少JPEG路径')
        path = Path(entry['jpg'])
        if not path.is_absolute() or not path.is_file():
            raise ValueError('预览JPEG必须是存在的绝对路径')
        path = path.resolve()
        if _file_hash(path) != entry['sha256']:
            raise ValueError('编码前预览图像哈希不符：'+path.name)
        size = _jpeg_info(path)
        if dimensions is not None and size != dimensions:
            raise ValueError('有序预览的图像尺寸必须一致')
        dimensions = size
        frames.append({'index': i+1, 'jpg': str(path), 'sha256': entry['sha256'],
                       'recipe_sha256': entry['recipe_sha256']})
        if progress:
            progress(i+1, len(entries), '校验完整预览的有序帧')
    width = 1280
    height = max(2, 2*math.floor(dimensions[1]*width/dimensions[0]/2+.5))
    return {'schema': 1, 'version': VERSION, 'frames': frames, 'frame_count': len(frames),
            'source_dimensions': dimensions, 'width': width, 'height': height,
            'fps': float(rate), 'fps_rational': str(rate), 'color': dict(COLOR),
            'encoder': {'codec': 'libx264', 'pixel_format': 'yuv420p', 'crf': 18, 'preset': 'medium',
                        'ffmpeg': str(ffmpeg), 'ffmpeg_sha256': _file_hash(ffmpeg),
                        'ffprobe': str(ffprobe), 'ffprobe_sha256': _file_hash(ffprobe)}}


def _check_sources(frames, cancelled):
    for frame in frames:
        _cancelled(cancelled)
        if _file_hash(frame['jpg']) != frame['sha256']:
            raise ValueError('预览源文件在编码或核验期间改变：'+Path(frame['jpg']).name)


@contextmanager
def _lock(folder):
    path = folder/'.preview-video.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a+') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('此预览目录已有编码任务，未启动并发编码') from exc
        yield


def _run_process(command, work, *, cancelled=None, progress=None, total=None, timeout=1800):
    """Own and cancel only this child process. All diagnostics stay in work."""
    _cancelled(cancelled)
    stdout_path, stderr_path = work/'process.stdout', work/'process.stderr'
    with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
        process = subprocess.Popen([str(x) for x in command], stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr)
        start = time.monotonic()
        last_frame = -1
        try:
            while process.poll() is None:
                _cancelled(cancelled)
                if time.monotonic()-start > timeout:
                    raise ValueError('视频编码或解码核验超时，已停止本次进程')
                if progress and total:
                    matches = re.findall(r'^frame=(\d+)\s*$', stdout_path.read_text(errors='replace'), re.M)
                    if matches and int(matches[-1]) != last_frame:
                        last_frame = int(matches[-1])
                        progress(min(last_frame,total),total,'编码完整低分辨率预览')
                time.sleep(.05)
            _cancelled(cancelled)
            if process.returncode:
                detail = stderr_path.read_text(errors='replace')[-2000:].strip()
                raise ValueError('视频工具执行失败：'+detail)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    return stdout_path.read_bytes(), stderr_path.read_text(errors='replace').strip()


def _validate_probe(probe, manifest):
    streams = probe.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'video':
        raise ValueError('预览视频必须只含一条视频流')
    s = streams[0]
    try:
        count = int(s['nb_read_frames'])
        rate = Fraction(s['avg_frame_rate'])
        duration = float(s.get('duration', probe.get('format', {}).get('duration')))
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError('无法核对视频实际解码帧数、帧率或时长') from exc
    n, fps = manifest['frame_count'], Fraction(manifest['fps_rational'])
    if count != n:
        raise ValueError(f'视频实际解码帧数不符：{count}，应为{n}')
    if rate != fps:
        raise ValueError('视频帧率与冻结清单不符')
    if not math.isfinite(duration) or abs(duration-float(n/fps)) > float(1/fps)+1e-6:
        raise ValueError('视频时长与有序帧数不符')
    required = {'width':manifest['width'], 'height':manifest['height'], 'codec_name':'h264',
                'pix_fmt':'yuv420p', 'color_range':'tv', 'color_space':'bt709',
                'color_transfer':'iec61966-2-1', 'color_primaries':'bt709'}
    if any(s.get(k) != v for k,v in required.items()):
        raise ValueError('视频尺寸、编码或色彩标记与冻结清单不符')
    return {'frame_count':count, 'fps':float(rate), 'duration_seconds':duration,
            'width':s['width'], 'height':s['height'], 'codec':s['codec_name'],
            'pixel_format':s['pix_fmt'], 'color_range':s['color_range'],
            'color_matrix':s['color_space'], 'color_transfer':s['color_transfer'],
            'color_primaries':s['color_primaries']}


def _probe(ffprobe, video, manifest, work, cancelled):
    command = [ffprobe, '-v', 'error', '-count_frames', '-show_entries',
               'stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_frames,pix_fmt,color_range,color_space,color_transfer,color_primaries,duration:format=duration',
               '-of', 'json', video]
    data, errors = _run_process(command,work,cancelled=cancelled,
                                timeout=max(60,manifest['frame_count']/manifest['fps']*10))
    if errors:
        raise ValueError('视频解码出现错误：'+errors[-1000:])
    try:
        return _validate_probe(json.loads(data),manifest)
    except json.JSONDecodeError as exc:
        raise ValueError('视频解码核验未返回有效记录') from exc


def _result(final, manifest, manifest_sha, receipt, reused):
    return {'schema':1, 'state':'awaiting-user-review', 'video':str(final/'preview.mp4'),
            'path':str(final/'preview.mp4'), 'sha256':receipt['video_sha256'],
            'manifest':str(final/'manifest.json'), 'manifest_sha256':manifest_sha,
            'ordered_frame_sha256':_digest(_json_bytes([f['sha256'] for f in manifest['frames']])),
            'ordered_recipe_sha256':_digest(_json_bytes([f['recipe_sha256'] for f in manifest['frames']])),
            **receipt['decoded'], 'color':dict(COLOR), 'encoded_verified':True,
            'visual_playback_verified':False, 'reused':reused}


def encode_preview(entries, folder, *, fps=25, cancelled=None, progress=None):
    """Encode every ordered input once, verify it, and stop at visual review.

    Recipe hashes are frozen provenance supplied by the renderer's validated
    receipts; this function does not infer a recipe from JPEG metadata.
    """
    _cancelled(cancelled)
    folder = output_path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    ffmpeg, ffprobe = _tools()
    with _lock(folder):
        manifest = _freeze(entries,fps,ffmpeg,ffprobe,cancelled,progress)
        manifest_bytes = _json_bytes(manifest)
        manifest_sha = _digest(manifest_bytes)
        final = folder/('preview-'+manifest_sha)
        stage = Path(tempfile.mkdtemp(prefix='.preview-incomplete-',dir=folder))
        try:
            if final.exists():
                if final.is_symlink() or not final.is_dir():
                    raise ValueError('现有预览路径类型不符，未覆盖')
                paths = [final/name for name in ('manifest.json','receipt.json','preview.mp4')]
                if any(p.is_symlink() or not p.is_file() for p in paths):
                    raise ValueError('现有预览不完整，未覆盖')
                if paths[0].read_bytes() != manifest_bytes:
                    raise ValueError('现有预览清单被改变，未覆盖')
                receipt = json.loads(paths[1].read_text())
                if receipt.get('manifest_sha256') != manifest_sha or _file_hash(paths[2]) != receipt.get('video_sha256'):
                    raise ValueError('现有预览视频或回执哈希不符，未覆盖')
                before = receipt['video_sha256']
                decoded = _probe(ffprobe,paths[2],manifest,stage,cancelled)
                if _file_hash(paths[2]) != before:
                    raise ValueError('现有视频在核验期间改变')
                _check_sources(manifest['frames'],cancelled)
                return _result(final,manifest,manifest_sha,{**receipt,'decoded':decoded},True)
            frames_dir = stage/'frames'; frames_dir.mkdir()
            for i, frame in enumerate(manifest['frames']):
                _cancelled(cancelled)
                dest = frames_dir/f'{i:06}.jpg'
                shutil.copyfile(frame['jpg'],dest)
                if _file_hash(dest) != frame['sha256']:
                    raise ValueError('预览工作副本哈希不符')
            (stage/'manifest.json').write_bytes(manifest_bytes)
            video = stage/'preview.mp4'
            # JPEG's gamma-coded samples are transformed only in matrix/range.
            # No colorspace transfer transform or gamma filter is applied.
            scale = (f"scale={manifest['width']}:{manifest['height']}:flags=lanczos:"
                     'in_color_matrix=bt601:out_color_matrix=bt709:in_range=full:out_range=limited,'
                     'format=yuv420p,setsar=1,sidedata=delete:type=ICC_PROFILE,'
                     'setparams=range=limited:color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709')
            command = [ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-xerror',
                       '-framerate',manifest['fps_rational'],'-start_number','0','-i',frames_dir/'%06d.jpg',
                       '-frames:v',str(manifest['frame_count']),'-vf',scale,'-an','-sn','-dn',
                       '-c:v','libx264','-preset','medium','-crf','18','-threads','2','-pix_fmt','yuv420p',
                       '-fps_mode','passthrough','-color_range','tv','-colorspace','bt709',
                       '-color_trc','iec61966-2-1','-color_primaries','bt709',
                       '-movflags','+faststart+write_colr','-progress','pipe:1','-nostats',video]
            if progress: progress(0,manifest['frame_count'],'编码完整低分辨率预览')
            _run_process(command,stage,cancelled=cancelled,progress=progress,
                         total=manifest['frame_count'],timeout=max(120,manifest['frame_count']*2))
            if progress: progress(0,manifest['frame_count'],'解码核验完整预览视频')
            decoded = _probe(ffprobe,video,manifest,stage,cancelled)
            _check_sources(manifest['frames'],cancelled)
            receipt = {'schema':1,'manifest_sha256':manifest_sha,'video_sha256':_file_hash(video),
                       'decoded':decoded,'sources_unchanged':True,'visual_playback_verified':False}
            (stage/'receipt.json').write_bytes(_json_bytes(receipt))
            # Working copies and process logs are not part of the deliverable.
            shutil.rmtree(frames_dir)
            for name in ('process.stdout','process.stderr'):
                (stage/name).unlink(missing_ok=True)
            _cancelled(cancelled)
            if final.exists():
                raise ValueError('预览输出在发布前已存在，未覆盖')
            stage.rename(final)
            if progress: progress(manifest['frame_count'],manifest['frame_count'],'完整预览已核验，等待观看确认')
            return _result(final,manifest,manifest_sha,receipt,False)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
