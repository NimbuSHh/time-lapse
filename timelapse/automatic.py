"""One-button RAW -> Adobe renders -> measured correction -> verified output.

Every Adobe input is an independent copy. Durable receipts let a stopped run
resume without silently reusing a changed source, recipe, or rendered image.
"""
from contextlib import contextmanager
import tempfile
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from . import joint_feedback as joint, tone_xmp
from .photoshop import PhotoshopRenderer, Cancelled
from .project import RAW, directory, digest, natural, safe_read, scan
from .workspace import output_path

SCHEMA = 1
MAX_ROUNDS = 2


def atomic_json(path, data):
    temp = path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush(); os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()


def fingerprint(path):
    info = path.stat()
    return {'name': path.name, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'device': info.st_dev, 'inode': info.st_ino}


def inputs(raw_dir):
    raw_dir = directory(raw_dir)
    if (raw_dir/'SYNTHETIC.txt').exists():
        raise ValueError('合成示例不是实际 RAW，不能交给 Photoshop 自动显影')
    raws = scan(raw_dir, RAW)
    if not 5 <= len(raws) <= 5000:
        raise ValueError('请选择包含 5–5000 张 RAW 的素材文件夹，无需预先导出 JPG')
    xmps, aux = scan(raw_dir, {'.xmp'}), scan(raw_dir, {'.acr'})
    snapshot = []
    for key in sorted(raws, key=natural):
        row = {'raw': fingerprint(raws[key])}
        for kind, mapping in (('xmp', xmps), ('acr', aux)):
            if key in mapping:
                row[kind] = {**fingerprint(mapping[key]), 'sha256': file_hash(mapping[key])}
        snapshot.append(row)
    return raw_dir, snapshot


def copy_raw(source, target):
    """APFS copy-on-write when available, never hard links to source media."""
    if target.exists():
        if (target.is_symlink() or target.stat().st_size != source.stat().st_size
                or (target.stat().st_ino == source.stat().st_ino and target.stat().st_dev == source.stat().st_dev)
                or file_hash(target) != file_hash(source)):
            raise ValueError('RAW 工作副本不完整，请保留现场并重新建立任务')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    cloned = False
    if sys.platform == 'darwin':
        library = ctypes.CDLL(None, use_errno=True)
        clone = library.clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clone.restype = ctypes.c_int
        cloned = clone(os.fsencode(source), os.fsencode(target), 0) == 0
        if not cloned and ctypes.get_errno() not in (errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOSYS):
            raise OSError(ctypes.get_errno(), '无法创建 RAW 工作副本')
    if not cloned:
        if shutil.disk_usage(target.parent).free < source.stat().st_size + 1024**3:
            raise ValueError('工作磁盘空间不足，无法继续建立独立 RAW 副本')
        shutil.copyfile(source, target)
    if target.stat().st_ino == source.stat().st_ino and target.stat().st_dev == source.stat().st_dev:
        raise ValueError('RAW 工作副本不能与原文件共享硬链接')


def load_latest(output):
    output = output_path(output)
    try:
        index = json.loads(safe_read(output/'automatic-latest.json', 1024*1024))
        run = Path(index['path']).resolve()
        if run.parent != output or not run.name.startswith('自动调色-'):
            raise ValueError('自动任务记录路径不符')
        state = json.loads(safe_read(run/'run.json', 32*1024*1024))
        if state['schema'] != SCHEMA or state['path'] != str(run):
            raise ValueError('自动任务记录版本或路径不符')
        return state
    except FileNotFoundError:
        return None


def public_state(state):
    if not state:
        return None
    return {key: state.get(key) for key in ('id', 'path', 'raw_dir', 'state', 'phase', 'count',
            'done', 'total', 'error', 'result')}


class Run:
    def __init__(self, raw_dir, output, progress=None, cancelled=None, renderer_factory=PhotoshopRenderer):
        self.source, self.snapshot = inputs(raw_dir)
        self.output = output_path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.cancelled = cancelled or (lambda: False)
        self.progress = progress
        signature = digest(json.dumps([str(self.source), self.snapshot, SCHEMA], sort_keys=True).encode())
        old = load_latest(self.output)
        if old and old.get('signature') == signature:
            self.state = old
        else:
            identity = uuid.uuid4().hex
            folder = self.output/('自动调色-'+identity[:12])
            folder.mkdir()
            self.state = {'schema': SCHEMA, 'id': identity, 'path': str(folder), 'raw_dir': str(self.source),
                'signature': signature, 'snapshot': self.snapshot, 'count': len(self.snapshot),
                'state': 'ready', 'phase': '准备', 'phases': {}}
        self.folder = Path(self.state['path'])
        self.renderer = renderer_factory(self.folder, self.cancelled)
        if self.state.get('renderer') not in (None, self.renderer.identity):
            identity = uuid.uuid4().hex
            self.folder = self.output/('自动调色-'+identity[:12])
            self.folder.mkdir()
            self.state = {'schema': SCHEMA, 'id': identity, 'path': str(self.folder), 'raw_dir': str(self.source),
                'signature': signature, 'snapshot': self.snapshot, 'count': len(self.snapshot),
                'state': 'ready', 'phase': '准备', 'phases': {}}
            self.renderer = renderer_factory(self.folder, self.cancelled)
        self.state['renderer'] = self.renderer.identity
        self.save()
        atomic_json(self.output/'automatic-latest.json', {'path': str(self.folder)})

    def save(self):
        atomic_json(self.folder/'run.json', self.state)

    def report(self, label, done=0, total=None):
        if self.cancelled():
            raise Cancelled('已停止，已完成的显影已保留，点击继续即可接着处理')
        self.state.update(phase=label, done=done, total=total or len(self.snapshot), state='running')
        self.save()
        if self.progress:
            self.progress(done, total or len(self.snapshot), label)

    def unchanged(self):
        _, current = inputs(self.source)
        if current != self.snapshot:
            raise ValueError('原素材或 XMP 在运行期间发生改变，已停止；请确认编辑后重新开始')

    def phase(self, name, operation, version=None):
        completed = self.state['phases'].get(name)
        if completed:
            path = Path(completed['result']['path']).resolve()
            if self.folder not in path.parents or digest(safe_read(path/'manifest.json', 64*1024*1024)) != completed['sha256']:
                raise ValueError('已完成的中间包发生改变，不能继续沿用')
            if version is None or completed.get('version') == version:
                return completed['result']
            if completed['result'].get('ready') is not False:
                raise ValueError('计算算法已更新，已发布的完整候选不能静默替换；请保留当前结果另建任务')
            self.state.setdefault('superseded_phases', []).append({'name': name, **completed})
            del self.state['phases'][name]
            self.save()
        self.report(name)
        result = operation()
        self.unchanged()
        self.state['phases'][name] = {'result': result, 'version': version, 'sha256': digest(safe_read(Path(result['path'])/'manifest.json', 64*1024*1024))}
        self.save()
        return result

    def render_group(self, name, xmp_folder=None, edge=1600, label=None):
        group = self.folder/name
        raws, jpgs, receipts = group/'raw', group/'jpg', group/'receipts'
        for path in (raws, jpgs, receipts):
            path.mkdir(parents=True, exist_ok=True)
        self.unchanged()
        for i, source in enumerate(self.snapshot):
            self.report(label or name, i)
            raw = self.source/source['raw']['name']
            stem = raw.stem
            original_xmp = self.source/source['xmp']['name'] if 'xmp' in source else None
            recipe = safe_read(Path(xmp_folder)/(stem+'.xmp'), 4*1024*1024) if xmp_folder else (safe_read(original_xmp, 4*1024*1024) if original_xmp else None)
            key = digest(json.dumps([source, digest(recipe) if recipe else None, edge, self.renderer.identity], sort_keys=True).encode())
            jpg, receipt = jpgs/(stem+'.jpg'), receipts/(stem+'.json')
            if receipt.exists():
                saved = json.loads(safe_read(receipt, 1024*1024))
                if saved['key'] == key and jpg.is_file() and saved['sha256'] == file_hash(jpg):
                    continue
                raise ValueError(f'已显影文件发生变化：{jpg.name}，不能把旧结果当作本轮结果')
            # An unreceipted file belongs to an interrupted call in this run.
            if jpg.exists():
                jpg.rename(receipts/(stem+'.incomplete-'+uuid.uuid4().hex+'.jpg'))
            copied = raws/raw.name
            copy_raw(raw, copied)
            if recipe is not None:
                (raws/(stem+'.xmp')).write_bytes(recipe)
            if 'acr' in source:
                shutil.copyfile(self.source/source['acr']['name'], raws/source['acr']['name'])
            self.renderer.render(copied, jpg, edge)
            rendered = safe_read(jpg, 128*1024*1024)
            if edge:
                # Real embedded metadata, never fabricated from the input XMP.
                tone_xmp.recipe_from_jpeg(rendered)
            if recipe is not None:
                joint.match(recipe, rendered, jpg.name, full_size=not edge)
            if fingerprint(raw) != source['raw']:
                raise ValueError('显影期间原 RAW 发生变化，已停止')
            atomic_json(receipt, {'key': key, 'sha256': digest(rendered)})
        self.unchanged()
        self.report(label or name, len(self.snapshot))
        return jpgs

    def execute(self):
        if self.state.get('state') == 'done' and self.state.get('result'):
            self.unchanged()
            for name, expected in self.state.get('delivery', {}).items():
                path = self.folder/name
                if self.folder not in path.resolve().parents or file_hash(path) != expected:
                    raise ValueError('已经导出的结果发生改变，请保留当前文件，不会自动覆盖')
            return self.state['result']
        try:
            self.state.pop('error', None)
            self.report('连接 Photoshop')
            self.renderer.preflight()
            current = self.render_group('initial', label='自动生成原始预览')
            source, best = None, None
            packages = self.folder/'packages'
            for number in range(1, MAX_ROUNDS+1):
                prefix = f'第 {number} 轮'
                prepared = self.phase(prefix+'准备校准', lambda: joint.prepare(current, packages, source=source, region_mode='auto'))
                package = Path(prepared['path'])
                renders = []
                for group, label in (('baseline','基准'), ('probe-exposure','曝光'), ('probe-highlights','高光'), ('probe-whites','白色色阶')):
                    renders.append(self.render_group(f'round-{number}/{group}', package/group, label=prefix+'自动显影 · '+label))
                solution = self.phase(prefix+'计算修正', lambda: joint.calibrate(package, *renders, packages,
                    progress=lambda done, total: self.report(prefix+'计算修正', done, total)),
                    version=joint.model.SOLVER_VERSION)
                if not solution['ready']:
                    messages = '；'.join(f"{i['count']} 帧{i['reason']}：{i['action']}" for i in solution.get('issues', []))
                    if best:
                        break
                    raise ValueError('自动校准未得到完整修正。'+messages+' 诊断目录：'+solution['path'])
                previews = self.render_group(f'round-{number}/corrected', Path(solution['path'])/'corrected', label=prefix+'自动复核显影')
                checked = self.phase(prefix+'检查效果', lambda: joint.verify(solution['path'], previews, packages))
                manifest = json.loads(safe_read(Path(checked['path'])/'manifest.json', 64*1024*1024))
                score = sum(v*v for v in manifest['metrics']['rmse_ev_by_band'][:3])
                if best and score >= best['score'] and not checked['numerically_accepted']:
                    break
                best = {'solution': solution, 'check': checked, 'previews': str(previews), 'score': score, 'round': number}
                if checked['numerically_accepted']:
                    break
                current, source = previews, solution['path']
            if best is None:
                raise ValueError('尚无可交付的联合修正，请查看当前任务诊断')
            # Full-size output also uses Adobe, not upscaled calibration JPGs.
            finals = self.render_group('final', Path(best['solution']['path'])/'corrected', edge=0, label='导出全尺寸 JPG')
            self.report('整理结果')
            result_folder = self.folder/'结果'
            xmps = result_folder/'修正XMP'
            jpgs = result_folder/('JPG' if best['check']['numerically_accepted'] else '候选JPG-需检查')
            xmps.mkdir(parents=True, exist_ok=True); jpgs.mkdir(parents=True, exist_ok=True)
            for xmp in (Path(best['solution']['path'])/'corrected').glob('*.xmp'):
                shutil.copyfile(xmp, xmps/xmp.name)
            for jpg in finals.glob('*.jpg'):
                if '.incomplete-' not in jpg.name:
                    shutil.copyfile(jpg, jpgs/jpg.name)
            self.unchanged()
            accepted = best['check']['numerically_accepted']
            result = {'id': self.state['id'], 'path': str(result_folder), 'count': len(self.snapshot),
                'xmps': str(xmps), 'jpgs': str(jpgs), 'previews': best['previews'], 'originals': str(self.folder/'initial/jpg'),
                'review': best['check']['path'], 'verified': accepted, 'rounds': best['round'],
                'message': '自动调色与数值复核完成，请播放确认视觉效果。' if accepted else '已自动完成两轮以内的校准与复核，仍有残差；已保留候选，请检查视觉效果。'}
            atomic_json(result_folder/'结果说明.json', result)
            self.state['delivery'] = {str(path.relative_to(self.folder)): file_hash(path) for directory in (xmps, jpgs) for path in directory.iterdir() if path.is_file()}
            self.state.update(state='done', result=result, phase='完成', done=len(self.snapshot), total=len(self.snapshot))
            self.save()
            return result
        except Exception as exc:
            self.state.update(state='stopped' if isinstance(exc, Cancelled) else 'error', error=str(exc))
            self.save()
            raise


@contextmanager
def render_lock(output):
    """Serialize Adobe workflows across all workspaces of this user."""
    root=output_path(output);root.mkdir(parents=True,exist_ok=True)
    global_path=Path(tempfile.gettempdir())/('timelapse-adobe-'+str(os.getuid())+'.lock')
    with global_path.open('a+') as global_lock, (root/'.automatic.lock').open('a+') as local_lock:
        try:
            fcntl.flock(global_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(local_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('另一个显影任务仍在运行，未启动并发 Adobe 任务') from exc
        yield


def run(raw_dir, output, progress=None, cancelled=None, renderer_factory=PhotoshopRenderer):
    output = output_path(output)
    output.mkdir(parents=True, exist_ok=True)
    with render_lock(output):
        return Run(raw_dir, output, progress, cancelled, renderer_factory).execute()
