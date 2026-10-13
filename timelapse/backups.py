"""Durable, restartable XMP writeback. RAW bytes are never opened for writing."""
from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid
from .project import directory, safe_read, digest, MAX_FRAMES

FOLDER = 'Time-lapse-XMP备份'
LIMIT = 4 * 1024 * 1024


def exchange(source, target, identity=None):
    """Swap names without unlinking either inode; never fall back to overwrite."""
    lib = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        fn = lib.renameatx_np
        flags = 0x2 | 0x10  # RENAME_SWAP | RENAME_NOFOLLOW_ANY (SDK sys/stdio.h)
    elif sys.platform == 'linux' and hasattr(lib, 'renameat2'):
        fn = lib.renameat2
        flags = 2  # RENAME_EXCHANGE
    else:
        raise ValueError('当前系统不支持保留原文件的安全交换，请仅导出修正包')
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    # Directory descriptors keep a renamed parent from redirecting the write.
    a = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        b = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(b)
            if identity is not None and [info.st_dev, info.st_ino] != identity:
                raise ValueError('导入目录身份已改变，停止交换')
            if fn(a, os.fsencode(source.name), b, os.fsencode(target.name), flags):
                error = ctypes.get_errno()
                raise OSError(error, '文件系统不支持安全交换或交换失败；不会降级为直接覆盖：' + os.strerror(error))
            os.fsync(a)
            os.fsync(b)
            # Flush the captured outgoing file as well as its directory entry.
            captured = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=a)
            try:
                if not stat.S_ISREG(os.fstat(captured).st_mode):
                    raise ValueError('换出的路径不是普通文件，已保留并停止')
                os.fsync(captured)
            finally:
                os.close(captured)
        finally:
            os.close(b)
    finally:
        os.close(a)


def retain_replace(raw, package, name, data, allowed, identity):
    """Durably journal first; retain even a late ACR save at the swap boundary."""
    target = raw / name
    if digest(safe_read(target, LIMIT)) not in allowed:
        raise ValueError(f'文件已被修改：{name}')
    retained = package / 'retained'
    retained.mkdir(exist_ok=True)
    directory(retained)
    slot = Path(tempfile.mkdtemp(prefix='swap-', dir=retained))
    payload = slot / name
    atomic(payload, data)
    os.chmod(payload, stat.S_IMODE(target.lstat().st_mode))
    atomic(slot / 'receipt.json', json.dumps({'xmp': name, 'allowed': sorted(allowed), 'replacement': digest(data)}).encode())
    sync_dir(slot)
    sync_dir(retained)
    sync_dir(package)
    # The preceding check cannot exclude a concurrent ACR save. The swap captures
    # the actual outgoing inode instead of deleting it. Never delete this slot.
    exchange(payload, target, identity)
    if digest(safe_read(payload, LIMIT)) not in allowed:
        raise ValueError(f'检测到 ACR 并发修改，已停止；换出的版本完整保留在：{payload}')
    if digest(safe_read(target, LIMIT)) != digest(data):
        raise ValueError(f'交换后文件被修改：{name}；保留版本在：{slot}')


def check_retained(package):
    retained = package / 'retained'
    if not retained.exists() and not retained.is_symlink():
        return
    directory(retained)
    for slot in retained.iterdir():
        directory(slot)
        receipt = slot / 'receipt.json'
        if not receipt.exists():
            # A process death while preparing this slot occurred before exchange.
            continue
        r = json.loads(safe_read(receipt, 16384))
        name = r['xmp']
        if not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() != '.xmp':
            raise ValueError('保留版本清单无效')
        if digest(safe_read(slot / name, LIMIT)) not in (*r['allowed'], r['replacement']):
            raise ValueError(f'检测到并发编辑的保留版本，停止自动恢复；请先检查：{slot / name}')


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'拒绝替换非普通文件：{path.name}')
        mode = stat.S_IMODE(info.st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix='.timelapse-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(package, manifest):
    atomic(package / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())


def backup_root(raw, create=False):
    root = raw / FOLDER
    if create:
        from .workspace import output_path
        output_path(root)
        root.mkdir(exist_ok=True)
        sync_dir(raw)
    if not root.exists() and not root.is_symlink():
        return None
    if directory(root) != root:
        raise ValueError('备份目录无效')
    return root


@contextmanager
def locked(raw):
    root = backup_root(raw, True)
    fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('备份锁文件无效')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('该目录正在写回或恢复，请稍后重试')
        yield root
    finally:
        os.close(fd)


def load(raw, key):
    if not isinstance(key, str) or not re.fullmatch(r'\d{8}T\d{12}Z-[a-f0-9]{32}', key):
        raise ValueError('备份编号无效')
    root = backup_root(raw)
    if root is None:
        raise ValueError('没有找到备份')
    package = root / key
    directory(package)
    m = json.loads(safe_read(package / 'manifest.json', 16 * 1024 * 1024))
    if not isinstance(m, dict) or m.get('schema') not in (1, 2) or m.get('id') != key or m.get('raw_dir') != str(raw):
        raise ValueError('备份不属于此 RAW 目录')
    if m.get('state') not in ('applying', 'applied', 'restoring', 'restored') or not isinstance(m.get('created'), str):
        raise ValueError('备份状态无效')
    if m['schema'] == 2 and (not isinstance(m.get('sequence'), int) or m['sequence'] < 1):
        raise ValueError('备份顺序无效')
    identity = raw.stat()
    if m.get('directory_identity') != [identity.st_dev, identity.st_ino]:
        raise ValueError('RAW 目录身份已改变')
    frames = m.get('frames')
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError('备份清单无效')
    names = set()
    for f in frames:
        if not isinstance(f, dict):
            raise ValueError('备份帧清单无效')
        name = f['xmp']
        if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name or Path(name).suffix.lower() != '.xmp' or name.casefold() in names:
            raise ValueError('备份文件名无效')
        names.add(name.casefold())
        if any(not isinstance(f.get(k), str) or not re.fullmatch(r'[a-f0-9]{64}', f[k]) for k in ('original', 'corrected')):
            raise ValueError('备份校验清单无效')
    return package, m


def list_backups(raw_dir):
    raw = directory(raw_dir)
    root = backup_root(raw)
    items = []
    if root:
        for p in sorted(root.iterdir(), reverse=True):
            if p.name.startswith('.'):
                continue
            try:
                _, m = load(raw, p.name)
                items.append({'id': m['id'], 'created': m['created'], 'state': m['state'], 'count': len(m['frames']), 'path': str(p), 'sequence': m.get('sequence', 0)})
            except (ValueError, OSError, KeyError, TypeError):
                items.append({'id': p.name, 'state': 'invalid', 'path': str(p), 'count': 0, 'created': p.name})
    items.sort(key=lambda item: (item.get('sequence', 0), item['id']), reverse=True)
    return {'raw_dir': str(raw), 'backups': items}


def require_latest(raw, key=None):
    active = [b for b in list_backups(raw)['backups'] if b['state'] != 'restored']
    if any(b['state'] == 'invalid' for b in active):
        raise ValueError('存在损坏或不完整备份，请先检查备份子文件夹')
    if key is not None and active and active[0]['id'] != key:
        raise ValueError('请先恢复最新一份备份，再恢复更早的版本')
    if key is None and any(b['state'] != 'applied' for b in active):
        raise ValueError('上次写回或恢复未完成，请先恢复该备份')


def apply(project, package_id):
    raw = project.raw_dir
    with locked(raw) as root:
        require_latest(raw)
        history = list_backups(raw)['backups']
        for b in history:
            _, previous = load(raw, b['id'])
            if previous.get('snapshot_id') == project.id:
                raise ValueError('此分析快照已经用于写回；请重新分析，不能重复应用')
        project.unchanged()
        key = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex
        package = Path(tempfile.mkdtemp(prefix='.preparing-', dir=root))
        try:
            (package / 'originals').mkdir()
            (package / 'corrected').mkdir()
            m = {'schema': 2, 'id': key, 'created': datetime.now(timezone.utc).isoformat(), 'raw_dir': str(raw),
                 'sequence': max((b.get('sequence', 0) for b in history), default=0) + 1, 'snapshot_id': project.id,
                 'directory_identity': [raw.stat().st_dev, raw.stat().st_ino], 'state': 'preparing', 'package_id': package_id, 'frames': []}
            # Complete and sync every original and corrected file before replacing any source.
            for f, value in zip(project.frames, project.result['final']):
                original, corrected = f.source.data, f.source.updated(value)
                atomic(package / 'originals' / f.xmp.name, original)
                atomic(package / 'corrected' / f.xmp.name, corrected)
                m['frames'].append({'xmp': f.xmp.name, 'original': digest(original), 'corrected': digest(corrected)})
            for f in m['frames']:
                for folder, key_hash in (('originals', 'original'), ('corrected', 'corrected')):
                    if digest(safe_read(package / folder / f['xmp'], LIMIT)) != f[key_hash]:
                        raise ValueError('备份落盘校验失败，未覆盖任何 XMP')
            m['state'] = 'applying'
            verification = {'schema': 1, 'package_id': package_id, 'raw_dir': str(raw), 'roi': project.roi,
                            'report': project.report, 'result': project.result,
                            'dimensions': list(project.frames[0].measure['dimensions']),
                            'frames': [{'name': f.name, 'preview_sha256': f.jpeg_hash} for f in project.frames]}
            atomic(package / 'verification.json', json.dumps(verification, ensure_ascii=False).encode())
            save(package, m)
            os.rename(package, root / key)
            package = root / key
            sync_dir(root)
        except Exception:
            if package.name.startswith('.preparing-'):
                shutil.rmtree(package, ignore_errors=True)
            raise
        try:
            project.unchanged()
            for f in m['frames']:
                target = raw / f['xmp']
                if digest(safe_read(target, LIMIT)) != f['original']:
                    raise ValueError(f"XMP 已被其他程序修改：{f['xmp']}")
                data = safe_read(package / 'corrected' / f['xmp'], LIMIT)
                if digest(data) != f['corrected']:
                    raise ValueError('修正备份校验失败')
                retain_replace(raw, package, f['xmp'], data, {f['original']}, m['directory_identity'])
            for f in m['frames']:
                if digest(safe_read(raw / f['xmp'], LIMIT)) != f['corrected']:
                    raise ValueError('写回后文件发生变化')
            m['state'] = 'applied'
            save(package, m)
        except Exception as e:
            raise ValueError(f'写回未全部完成，请关闭 ACR 后从备份恢复。备份：{package}。原因：{e}') from e
        project.exports[package_id] = verification
        return {'backup_id': key, 'backup_path': str(package), 'raw_dir': str(raw), 'count': len(m['frames']),
                'package_id': package_id, 'path': str(package)}


def restore(raw_dir, key):
    raw = directory(raw_dir)
    with locked(raw):
        package, m = load(raw, key)
        if m['state'] == 'restored':
            raise ValueError('此备份已经恢复，无需重复恢复')
        require_latest(raw, key)
        if m['state'] not in ('applied', 'applying', 'restoring'):
            raise ValueError('备份尚未完整，不能自动恢复')
        directory(package / 'originals')
        check_retained(package)
        originals = []
        total = 0
        # Preflight the whole batch. Never overwrite later ACR edits silently.
        for f in m['frames']:
            data = safe_read(package / 'originals' / f['xmp'], LIMIT)
            total += len(data)
            if total > 64 * 1024 * 1024:
                raise ValueError('备份总大小超过限制')
            if digest(data) != f['original']:
                raise ValueError(f"原始备份校验失败：{f['xmp']}")
            if digest(safe_read(raw / f['xmp'], LIMIT)) not in (f['original'], f['corrected']):
                raise ValueError(f"{f['xmp']} 在写回后被修改；为保留新编辑，已停止恢复。请先另存这些编辑。")
            originals.append(data)
        m['state'] = 'restoring'
        save(package, m)
        try:
            for f, data in zip(m['frames'], originals):
                if digest(safe_read(raw / f['xmp'], LIMIT)) not in (f['original'], f['corrected']):
                    raise ValueError(f"文件在恢复期间被修改：{f['xmp']}")
                if digest(safe_read(raw / f['xmp'], LIMIT)) != f['original']:
                    retain_replace(raw, package, f['xmp'], data, {f['original'], f['corrected']}, m['directory_identity'])
            for f in m['frames']:
                if digest(safe_read(raw / f['xmp'], LIMIT)) != f['original']:
                    raise ValueError('恢复后文件发生变化')
            m['state'] = 'restored'
            save(package, m)
        except Exception as e:
            raise ValueError(f'恢复中断，可重新选择此备份继续恢复。备份：{package}。原因：{e}') from e
        return {'backup_path': str(package), 'raw_dir': str(raw), 'count': len(m['frames'])}
