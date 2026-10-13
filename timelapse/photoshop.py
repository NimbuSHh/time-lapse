"""Local Photoshop/Camera Raw bridge. Only generated RAW copies are opened."""
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import uuid

from .project import digest
from .workspace import output_path

SCRIPT = Path(__file__).with_name('photoshop-render.jsx')
APPLESCRIPT = '''on run argv
    set scriptFile to POSIX file (item 1 of argv) as alias
    set scriptCode to read scriptFile as «class utf8»
    set scriptArguments to items 2 thru -1 of argv
    with timeout of 600 seconds
        tell application id "com.adobe.Photoshop"
            return do javascript scriptCode with arguments scriptArguments
        end tell
    end timeout
end run
'''


# This exact predecessor differs only in its return value (no native-size
# receipt). Allow reading its preview receipts with the same Photoshop version;
# future pixel-affecting script/bridge changes cannot inherit this compatibility.
LEGACY_RENDER_DIGEST = '492c99821bd5eea299c4c2a82d2a2ea22a791860408ee33f2a4ee6572d9fcd14'
DIMENSION_REPLY = b'return "TIMELAPSE_RENDERED|" + Math.round(width) + "|" + Math.round(height) + "|" + Math.round(doc.width.as("px")) + "|" + Math.round(doc.height.as("px"));'


def compatible_identities(version, script, bridge):
    previous=script.replace(DIMENSION_REPLY,b'return "TIMELAPSE_RENDERED";')
    if script.count(DIMENSION_REPLY)==1 and digest(previous+bridge.encode())==LEGACY_RENDER_DIGEST:
        return ['photoshop-'+version+'-'+LEGACY_RENDER_DIGEST]
    return []


class Cancelled(Exception):
    pass


def installed():
    if sys.platform != 'darwin':
        return {'available': False, 'reason': '自动显影目前需要 macOS 与本机 Photoshop'}
    candidates = sorted(Path('/Applications').glob('Adobe Photoshop */Adobe Photoshop *.app'), reverse=True)
    for app in candidates:
        try:
            info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
            if info.get('CFBundleIdentifier') == 'com.adobe.Photoshop':
                return {'available': True, 'path': str(app), 'version': info.get('CFBundleShortVersionString', '')}
        except (OSError, ValueError):
            continue
    return {'available': False, 'reason': '未找到本机 Photoshop；安装后即可自动显影，也可使用手动流程'}


class PhotoshopRenderer:
    def __init__(self, run_dir, cancelled=None):
        self.root = output_path(run_dir)
        self.cancelled = cancelled or (lambda: False)
        self.info = installed()
        if not self.info['available']:
            raise ValueError(self.info['reason'])
        self.identity = 'photoshop-' + self.info['version'] + '-' + digest(SCRIPT.read_bytes()+APPLESCRIPT.encode())
        self.compatible_identities = compatible_identities(self.info['version'],SCRIPT.read_bytes(),APPLESCRIPT)

    def _call(self, arguments, timeout):
        if self.cancelled():
            raise Cancelled('已停止，已完成的显影会保留，可稍后继续')
        # No shell, script interpolation, UI keystrokes or global app termination.
        process = subprocess.Popen(['/usr/bin/osascript', '-e', APPLESCRIPT, str(SCRIPT), *arguments],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        start = time.monotonic()
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancelled() or time.monotonic() - start > timeout:
                        # Photoshop may still finish its current open operation.
                        # The lease prevents that late call from saving anything.
                        (self.root/'render.lease').unlink(missing_ok=True)
                        process.terminate()
                        try:
                            process.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.communicate()
                        if self.cancelled():
                            raise Cancelled('已请求停止；Photoshop 当前照片结束后不再继续，已完成的显影已保留')
                        raise ValueError('Photoshop 响应超时。请先保存并关闭 Camera Raw 窗口或处理 Adobe 提示，再点击继续；未关闭你的文档')
        finally:
            if process.poll() is None:
                process.terminate()
        if process.returncode:
            if '-1743' in stderr:
                raise ValueError('macOS 尚未允许自动控制 Photoshop。请在系统设置 → 隐私与安全性 → 自动化中允许启动工作台的应用控制 Photoshop，再点击继续')
            raise ValueError('Photoshop 未能显影。请确认已保存并关闭 Camera Raw 窗口，且没有待处理的 Adobe 提示。' + stderr.strip()[-1000:])
        return stdout.strip()

    def preflight(self):
        response = self._call(['check'], 20)
        if not response.startswith('TIMELAPSE_READY|'):
            raise ValueError('Photoshop 脚本接口未就绪，请关闭 Camera Raw 窗口后继续')
        return response.split('|', 1)[1]

    def render(self, raw, destination, edge=1600):
        raw, destination = Path(raw).resolve(), output_path(destination)
        if self.root not in raw.parents or self.root not in destination.parents:
            raise ValueError('自动显影只能打开当前任务的 RAW 工作副本，并写入当前工作目录')
        if destination.exists():
            raise ValueError('自动显影目标已存在，不能覆盖未核对的文件')
        destination.parent.mkdir(parents=True, exist_ok=True)
        nonce = uuid.uuid4().hex
        lease = self.root/'render.lease'
        lease.write_text(nonce, encoding='ascii')
        try:
            response = self._call(['render', str(raw), str(destination), str(edge), str(lease), nonce,
                                   str(int((time.time()+30)*1000))], 300)
            parts=response.split('|')
            if len(parts)!=5 or parts[0]!='TIMELAPSE_RENDERED' or not destination.is_file():
                raise ValueError('Photoshop 未返回完整的显影结果，保留任务以便继续')
            try:
                native=[int(v) for v in parts[1:3]]
                actual=[int(v) for v in parts[3:5]]
                if min(native+actual)<1 or (edge==0 and native!=actual): raise ValueError()
            except ValueError as exc:
                raise ValueError('Adobe未返回有效的原生显影尺寸') from exc
            self.last_render={'raw':str(raw),'native_size':native,'output_size':actual,'edge':edge}
        finally:
            lease.unlink(missing_ok=True)
        return destination
