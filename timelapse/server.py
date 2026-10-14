from concurrent.futures import ThreadPoolExecutor
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import tempfile
from urllib.parse import urlparse, parse_qs
import uuid
import webbrowser
from .project import Project, safe_read, verify_manifest, digest, directory
from .demo import create
from .preview import region_preview
from .folders import choose_folder
from . import backups, workspace

STATIC = Path(__file__).parent / 'static'


class App:
    def __init__(self, root, work_dir=None):
        self.root = Path(root).absolute()
        # --root outside the installed program remains a supported explicit
        # working directory (also useful for isolated tests).
        outside_project = self.root.resolve() != workspace.PROJECT_ROOT and workspace.PROJECT_ROOT not in self.root.resolve().parents
        self.workspace_explicit = work_dir is not None or outside_project
        self.work_dir = workspace.owner(work_dir or self.root) if self.workspace_explicit else None
        if self.work_dir is not None:
            workspace.output_root(self.work_dir)
        self.token = secrets.token_urlsafe(32)
        self.project = None
        self.jobs = {}
        self.auto_job = None
        self.auto_cancel = threading.Event()
        self.refine_job = None
        self.refine_cancel = threading.Event()
        self.trajectory_job = None
        self.trajectory_output = None
        self.trajectory_cancel = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.verification_target = None
        self.verification_error = ''
        self.verification_images = None
        self.target_path = workspace.output_root(self.work_dir) / 'verification-target.json' if self.work_dir else None
        # Legacy state is read-only. New saves always use the selected workspace.
        legacy = self.root / 'outputs' / 'verification-target.json'
        if self.target_path is None and legacy.is_file():
            try:
                saved = json.loads(safe_read(legacy, 128 * 1024 * 1024))
                self.work_dir = workspace.owner(saved['target']['raw_dir'])
                self.target_path = workspace.output_root(self.work_dir) / 'verification-target.json'
            except Exception:
                pass
        self.load_verification_target(self.target_path if self.target_path and self.target_path.is_file() else legacy)
        # Keep the browser bundle paired with this running backend. Editing the
        # files on disk must not silently give an old process a newer UI.
        self.static_assets = {name: (STATIC / name).read_bytes()
                              for name in ('index.html', 'app.js', 'auto.js', 'progress.js', 'style.css',
                                           'refine.html', 'refine.js', 'refine.css',
                                           'trajectory.html', 'trajectory.js')}

    def load_verification_target(self, path):
        self.verification_target = None
        self.verification_error = ''
        self.verification_images = None
        self.project = None
        if path is None:
            return
        try:
            saved = json.loads(safe_read(path, 128 * 1024 * 1024))
            payload = saved['target']
            if saved['sha256'] != digest(json.dumps(payload, sort_keys=True, allow_nan=False).encode()):
                raise ValueError('保存的分析目标校验失败')
            self.verification_target = payload
            self.project = Project.restore(payload)
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.verification_error = f'无法恢复保存的分析目标：{exc}。请重新分析序列。'

    def work_output(self, preferred=None):
        if preferred and not self.workspace_explicit:
            self.work_dir = workspace.owner(directory(preferred))
        if self.work_dir is None:
            raise ValueError('请先选择素材所在的工作目录，输出不会保存到程序项目目录')
        output = workspace.output_root(self.work_dir)
        self.target_path = output / 'verification-target.json'
        return output

    def select_workspace(self, value):
        if any(job['state'] == 'running' for job in self.jobs.values()):
            raise ValueError('正在处理任务，暂时不能更换工作目录')
        old_path = self.target_path
        self.work_output(value)
        if self.target_path != old_path:
            self.load_verification_target(self.target_path)
        return {'work_dir': str(self.work_dir), 'output_root': str(self.target_path.parent)}

    def save_verification_target(self, project):
        target = project.verification_target()
        output = self.work_output(project.raw_dir)
        output.mkdir(parents=True, exist_ok=True)
        directory(self.target_path.parent)
        payload = json.dumps(target, sort_keys=True, allow_nan=False)
        saved = json.dumps({'target': target, 'sha256': digest(payload.encode())},
                           ensure_ascii=False, allow_nan=False)
        if len(saved.encode('utf-8')) > 128 * 1024 * 1024:
            raise ValueError('分析来源记录超过 128 MB，无法完整保存')
        fd, name = tempfile.mkstemp(prefix='.verification-', dir=output)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(saved)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.target_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.verification_target = json.loads(payload)
        project.verified_images = None
        self.verification_images = None
        self.verification_error = ''
        return project.result

    def verification_state(self):
        target = self.verification_target
        if target is None:
            return {'target': None, 'reason': self.verification_error or '尚无分析结果，请先分析序列。'}
        live = (self.project is not None and self.project.id == target['snapshot_id']
                and self.project.result is not None
                and self.project.result['revision'] == target['result']['revision'])
        plans = getattr(self.project, 'exposure_calibrations', {}) if live else {}
        latest = next(reversed(plans), None)
        return {'target': {k: v for k, v in target.items() if k != 'checkpoint'},
                'editable': live and not getattr(self.project, 'written', False),
                'written': live and getattr(self.project, 'written', False),
                'reason': self.verification_error,
                'exposure_calibration': {'id': self.project.id, 'revision': self.project.result['revision'],
                    'calibration_id': latest, 'path': str(plans[latest]['path'])} if latest else None}

    def verify_target(self, data):
        target = self.verification_target
        if target is None:
            raise ValueError(self.verification_error or '尚无分析目标，请先分析序列并更新预览')
        if data.get('id') != target['snapshot_id'] or data.get('revision') != target['result']['revision']:
            raise ValueError('复核目标已改变，请刷新页面恢复已保存的目标，或更新当前预览')
        if not str(data.get('preview_dir', '')).strip():
            raise ValueError('请选择 ACR 重新导出的小图文件夹')
        result = verify_manifest(target, data['preview_dir'], diagnostic=True)
        self.verification_images = result.pop('_images')
        if self.project is not None and self.project.id == target['snapshot_id']:
            self.project.verified_images = self.verification_images
        return result

    def submit(self, task):
        with self.lock:
            if any(j['state'] == 'running' for j in self.jobs.values()):
                raise ValueError('正在处理上一项任务，请稍候')
            key = uuid.uuid4().hex
            self.jobs = {key: {'state': 'running', 'progress': None}}
        def progress(done, total, phase=None):
            with self.lock:
                self.jobs[key]['progress'] = [done, total]
                self.jobs[key]['phase'] = phase
        def run():
            try:
                result = task(progress)
                with self.lock:
                    self.jobs[key].update(state='done', result=result)
            except Exception as e:
                with self.lock:
                    self.jobs[key].update(state='error', error=str(e))
                    if hasattr(e, 'measurement'):
                        self.jobs[key]['measurement'] = e.measurement
        self.pool.submit(run)
        return {'job': key}

    def automatic_state(self):
        return self.trajectory_state()

    def automatic_image(self, run_id, index, original=False):
        from .automatic import load_latest
        state = load_latest(self.work_output())
        if not state or state['id'] != run_id or not state.get('result'):
            raise ValueError('自动调色预览已失效，请重新载入当前结果')
        if not 0 <= index < len(state['snapshot']):
            raise ValueError('帧序号无效')
        folder = Path(state['path'])
        previews = Path(state['result']['originals' if original else 'previews']).resolve()
        if folder not in previews.parents:
            raise ValueError('预览目录与当前任务不符')
        stem = Path(state['snapshot'][index]['raw']['name']).stem
        receipt = json.loads(safe_read(previews.parent/'receipts'/(stem+'.json'), 1024*1024))
        data = safe_read(previews/(stem+'.jpg'), 25*1024*1024)
        if digest(data) != receipt['sha256']:
            raise ValueError('预览小图已被修改，不能继续显示为已核对结果')
        return data

    def refinement_state(self):
        from .refinement import load_latest
        saved = load_latest(self.work_output()) if self.work_dir else None
        task = dict(saved) if saved else None
        with self.lock:
            job = dict(self.jobs.get(self.refine_job, {}))
        active = job.get('state') == 'running'
        if job:
            task = task or {}
            if active or job.get('state') == 'error':
                task['state'] = job['state']
            if job.get('phase') is not None:
                task['phase'] = job['phase']
            if job.get('progress'):
                task['done'], task['total'] = job['progress']
            if job.get('error'):
                task['error'] = job['error']
            if job.get('state') == 'done':
                result = job.get('result') or {}
                task['result'] = result
                saved_state = task.get('state')
                task['state'] = result.get('state') or (saved_state if saved_state != 'running' else None) or 'review-needed'
        return {'task': task, 'job': self.refine_job if active else None}

    def trajectory_state(self):
        from .workflow import load_latest
        output = self.work_output() if self.work_dir else None
        saved = load_latest(output) if output is not None else None
        task = dict(saved) if saved else None
        with self.lock:
            job = dict(self.jobs.get(self.trajectory_job, {})) if output is not None and self.trajectory_output==str(output) else {}
        active = job.get('state') == 'running'
        if job:
            task = task or {}
            if active or job.get('state') == 'error': task['state'] = job['state']
            if job.get('phase') is not None: task['phase'] = job['phase']
            if job.get('progress'): task['done'], task['total'] = job['progress']
            if job.get('error'): task['error'] = job['error']
            if job.get('state') == 'done':
                task['result'] = job.get('result') or {}
                task['state'] = task['result'].get('state', 'awaiting-user-review')
        return {'task': task, 'job': self.trajectory_job if active else None}

    def trajectory_reference(self,index=0):
        from .workflow import load_preparation
        from .automatic import load_latest
        state=load_preparation(self.work_output()) or load_latest(self.work_output())
        if not state or not 0<=index<len(state['snapshot']): raise ValueError('原始参考尚未生成或帧序号无效')
        folder=Path(state['path']).resolve()
        stem=Path(state['snapshot'][index]['raw']['name']).stem
        jpg=folder/'initial/jpg'/(stem+'.jpg')
        receipt=folder/'initial/receipts'/(stem+'.json')
        data=safe_read(jpg,25*1024*1024)
        if digest(data)!=json.loads(safe_read(receipt,1024*1024))['sha256']:
            raise ValueError('原始参考图已改变')
        return data

    def trajectory_video(self):
        from .workflow import load_latest
        task = load_latest(self.work_output())
        if not task or not task.get('result',{}).get('video'): raise ValueError('完整视频尚未生成')
        folder = Path(task['path']).resolve()
        result = task['result']; path = Path(result['video']).resolve()
        if folder not in path.parents: raise ValueError('预览视频路径越界')
        data = safe_read(path, 256*1024*1024)
        if digest(data) != result['sha256']: raise ValueError('预览视频已改变')
        return data

    def dispatch(self, route, data):
        if route == '/api/trajectory/start':
            from .workflow import resume as run
            output = self.work_output()
            cancelled = threading.Event()
            task = self.submit(lambda progress: run(output, progress=progress, cancelled=cancelled.is_set))
            self.trajectory_cancel = cancelled
            self.trajectory_job = task['job']
            self.trajectory_output = str(output)
            return {**task, 'output_root': str(output)}
        if route == '/api/trajectory/cancel':
            if self.jobs.get(self.trajectory_job, {}).get('state') == 'running':
                self.trajectory_cancel.set()
            return {'requested': True}
        if route == '/api/refine/start':
            from .refinement import run
            output = self.work_output()
            cancelled = threading.Event()
            task = self.submit(lambda progress: run(output, progress=progress, cancelled=cancelled.is_set))
            self.refine_cancel = cancelled
            self.refine_job = task['job']
            return {**task, 'output_root': str(output)}
        if route == '/api/refine/cancel':
            if self.jobs.get(self.refine_job, {}).get('state') == 'running':
                self.refine_cancel.set()
            return {'requested': True}
        if route == '/api/auto/start':
            from .workflow import run, options
            raw_dir = directory(data['raw_dir'])
            regions=options(data.get('regions'))
            # Resolve the workspace only after checking job ownership. An old
            # browser cannot change the active workspace while a job is running.
            if any(j['state']=='running' for j in self.jobs.values()):
                raise ValueError('正在处理上一项任务，请稍候')
            output=self.work_output(raw_dir)
            cancelled=threading.Event()
            task=self.submit(lambda progress:run(raw_dir,output,regions=regions,progress=progress,cancelled=cancelled.is_set))
            self.auto_cancel=self.trajectory_cancel=cancelled
            self.auto_job=self.trajectory_job=task['job']
            self.trajectory_output=str(output)
            return {**task,'output_root':str(output)}
        if route == '/api/trajectory/export':
            from .workflow import export
            if data.get('confirmed') is not True:
                raise ValueError('请先完整观看并确认当前视频，再导出全尺寸')
            required=('run_id','video_sha256','selection_sha256','target_sha256','effective_policy_sha256')
            if any(not isinstance(data.get(k),str) or not data[k] for k in required):
                raise ValueError('确认缺少具体视频与配方版本，请重新载入预览')
            confirmation={k:data[k] for k in required};confirmation['confirmed']=True
            output=self.work_output()
            cancelled=threading.Event()
            task=self.submit(lambda progress:export(output,confirmation,progress=progress,cancelled=cancelled.is_set))
            self.auto_cancel=self.trajectory_cancel=cancelled
            self.auto_job=self.trajectory_job=task['job']
            self.trajectory_output=str(output)
            return {**task,'output_root':str(output)}
        if route == '/api/auto/cancel':
            self.auto_cancel.set();self.trajectory_cancel.set()
            return {'requested':True}

        if route == '/api/workspace':
            return self.select_workspace(data['work_dir'])
        if route == '/api/verify' and 'package_id' not in data:
            return self.submit(lambda progress: self.verify_target(data))
        if route == '/api/joint/regions':
            from .joint_feedback import suggest_regions
            return self.submit(lambda progress: suggest_regions(data['previews'], progress=progress))
        if route == '/api/joint/prepare':
            from .joint_feedback import prepare
            return self.submit(lambda progress: prepare(data['previews'], self.work_output(data.get('work_dir') or data['previews']),
                mid_roi=data.get('mid_roi'), high_roi=data.get('high_roi'), window=data.get('window', 31),
                source=data.get('source') or None, progress=progress, region_mode=data.get('region_mode')))
        if route == '/api/joint/calibrate':
            from .joint_feedback import calibrate
            return self.submit(lambda progress: calibrate(data['source'], data['baseline_previews'],
                data['exposure_previews'], data['highlights_previews'], data['whites_previews'],
                self.work_output(data.get('work_dir')), progress=progress))
        if route == '/api/joint/verify':
            from .joint_feedback import verify
            return self.submit(lambda progress: verify(data['source'], data['previews'],
                self.work_output(data.get('work_dir')), progress=progress))
        if route == '/api/feedback':
            # Independent from the live analysis: use its durable frozen package
            # even after refresh/writeback. This route never applies any XMP.
            from .feedback import build
            source = str(data.get('source', '')).strip()
            if not source:
                target = self.verification_target
                if (target is None or data.get('id') != target['snapshot_id']
                        or data.get('revision') != target['result']['revision']):
                    raise ValueError('残差修正目标已失效，请恢复当前分析目标或选择来源包')
                source = self.target_path
            return self.submit(lambda progress: build(source, data['original_previews'],
                data['current_xmps'], data['previews'], self.work_output(data.get('work_dir')), progress=progress))
        if route == '/api/backups':
            return self.submit(lambda progress: backups.list_backups(data['raw_dir']))
        if route == '/api/restore':
            if data.get('acr_closed') is not True:
                raise ValueError('请先关闭 ACR 中的此序列，再确认恢复')
            def restore(progress):
                try:
                    return backups.restore(data['raw_dir'], data['backup_id'])
                finally:
                    self.project = None
            return self.submit(restore)
        if route == '/api/choose-folder':
            return self.submit(lambda progress: choose_folder(data.get('field'), data.get('initial', '')))
        if route == '/api/demo':
            def demo(progress):
                dest = self.work_output(data.get('work_dir')) / 'demo' / uuid.uuid4().hex
                dest.parent.mkdir(parents=True, exist_ok=True)
                return create(dest)
            return self.submit(demo)
        if route == '/api/region-preview':
            return self.submit(lambda progress: region_preview(data['preview_dir'], data.get('index', 0)))
        if route == '/api/analyze':
            def analyze(progress):
                self.work_output(data['raw_dir'])
                self.project = None
                project = Project(data['raw_dir'], data['preview_dir'], data.get('roi', [0, 0, 1, 1]), data.get('mode', 'manual'), data.get('srgb', False), progress)
                self.project = project
                return project.public()
            return self.submit(analyze)
        if route not in ('/api/calculate', '/api/apply', '/api/export', '/api/verify',
                         '/api/exposure/prepare', '/api/exposure/calibrate'):
            raise ValueError('当前服务不支持此操作，请重新启动工作台并使用新打开的页面')
        project = self.project
        if project is None or data.get('id') != project.id:
            raise ValueError('分析快照已失效，请重新分析')
        if route == '/api/exposure/prepare':
            from .exposure_calibration import prepare
            def prepared(progress):
                result = prepare(project, self.work_output(project.raw_dir), data['revision'])
                self.save_verification_target(project)
                return result
            return self.submit(prepared)
        if route == '/api/exposure/calibrate':
            from .exposure_calibration import calibrate
            def calibrated(progress):
                calibrate(project, data['revision'], data['calibration_id'],
                          data['baseline_previews'], data['exposure_previews'], progress)
                return self.save_verification_target(project)
            return self.submit(calibrated)
        if route == '/api/calculate':
            if getattr(project, 'written', False):
                raise ValueError('XMP 已写回或写回中断，请重新显影小图并重新分析')
            def calculate(progress):
                project.calculate(data['settings'])
                return self.save_verification_target(project)
            return self.submit(calculate)
        if route == '/api/apply':
            if data.get('acr_closed') is not True:
                raise ValueError('请先关闭 ACR 中的此序列，再确认写回')
            def apply(progress):
                if getattr(project, 'written', False):
                    raise ValueError('此分析快照已经用于写回，请重新分析')
                project.validate_write(data['revision'], data.get('order_confirmed'))
                # Persist the consumed state before any source sidecar is written.
                project.written = True
                if self.verification_target is not None:
                    try:
                        self.save_verification_target(project)
                    except Exception:
                        # No source write has started, so a storage failure
                        # must not consume an otherwise usable live snapshot.
                        project.written = False
                        raise
                try:
                    return backups.apply(project, uuid.uuid4().hex)
                finally:
                    project.written = True
            return self.submit(apply)
        if route == '/api/export':
            mode = data.get('destination', 'source_subdir')
            if mode not in ('source_subdir', 'custom', 'source_backup'):
                raise ValueError('未知的导出位置，请重新选择')
            if mode == 'custom':
                value = data.get('output_dir')
                if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
                    raise ValueError('请选择或输入导出目录的完整路径')
                output = directory(value.strip())
            else:
                output = project.raw_dir
            if output == self.root.resolve() or self.root.resolve() in output.parents:
                raise ValueError('修正包不能导出到项目目录下，请选择项目目录以外的位置')
            if mode == 'source_backup':
                return self.dispatch('/api/apply', data)
            if mode == 'custom':
                return self.submit(lambda progress: project.export_direct(output, data['revision'],
                    data.get('order_confirmed')))
            return self.submit(lambda progress: project.export(output, data['revision'],
                data.get('order_confirmed'), allow_source_subdir=True))
        if route == '/api/verify':
            return self.submit(lambda progress: project.verify(data['preview_dir'], data['package_id']))
        raise ValueError('未知操作')


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, mime='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def host_ok(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def authorized(self):
            origin = self.headers.get('Origin')
            return self.host_ok() and (origin is None or origin == f'http://127.0.0.1:{self.server.server_port}') and secrets.compare_digest(self.headers.get('X-Timelapse-Token', ''), app.token)

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, {'error': 'Host 不允许'})
            parts = urlparse(self.path)
            if parts.path in ('/', '/app.js', '/auto.js', '/progress.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/auto.js': 'auto.js', '/progress.js': 'progress.js', '/style.css': 'style.css'}[parts.path]
                mime = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'auto.js': 'text/javascript; charset=utf-8', 'progress.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
                return self.send(200, app.static_assets[name], mime)
            if parts.path in ('/refine', '/refine/', '/refine.js', '/refine.css'):
                name = {'/refine': 'refine.html', '/refine/': 'refine.html',
                        '/refine.js': 'refine.js', '/refine.css': 'refine.css'}[parts.path]
                mime = {'refine.html': 'text/html; charset=utf-8', 'refine.js': 'text/javascript; charset=utf-8',
                        'refine.css': 'text/css; charset=utf-8'}[name]
                return self.send(200, app.static_assets[name], mime)
            if parts.path in ('/trajectory', '/trajectory/', '/trajectory.js'):
                name = 'trajectory.js' if parts.path.endswith('.js') else 'trajectory.html'
                return self.send(200, app.static_assets[name], 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/html; charset=utf-8')
            if not self.authorized():
                return self.send(403, {'error': '需要本机会话密钥，请从启动链接打开'})
            try:
                query = parse_qs(parts.query)
                if parts.path == '/api/config':
                    return self.send(200, {'output_root': str(workspace.output_root(app.work_dir)) if app.work_dir else None,
                                           'work_dir': str(app.work_dir) if app.work_dir else None, 'joint_api_version': 1, 'automatic_api_version': 2, 'trajectory_api_version': 2, 'workspace_explicit': app.workspace_explicit})
                if parts.path == '/api/auto/status':
                    return self.send(200, app.automatic_state())
                if parts.path == '/api/refine/status':
                    return self.send(200, app.refinement_state())
                if parts.path == '/api/trajectory/status':
                    return self.send(200, app.trajectory_state())
                if parts.path == '/api/trajectory/reference':
                    return self.send(200, app.trajectory_reference(int(query.get('index',['0'])[0])), 'image/jpeg')
                if parts.path == '/api/trajectory/video':
                    return self.send(200, app.trajectory_video(), 'video/mp4')
                if parts.path == '/api/auto/image':
                    return self.send(200, app.automatic_image(query['id'][0], int(query['index'][0]), query.get('kind', ['corrected'])[0] == 'original'), 'image/jpeg')
                if parts.path == '/api/verification-target':
                    with app.lock:
                        if any(j['state'] == 'running' for j in app.jobs.values()):
                            raise ValueError('后台正在处理任务，请完成后刷新页面恢复分析目标。')
                        return self.send(200, app.verification_state())
                if parts.path == '/api/job':
                    with app.lock:
                        value = dict(app.jobs[query['id'][0]])
                    return self.send(200, value)
                if parts.path == '/api/image':
                    p = app.project
                    target = app.verification_target
                    if (p is None or query['id'][0] != p.id) and target and query['id'][0] == target['snapshot_id']:
                        index = int(query['index'][0])
                        if not 0 <= index < len(target['frames']):
                            raise ValueError('帧序号无效')
                        if query.get('kind', ['original'])[0] == 'verified':
                            if app.verification_images is None:
                                raise ValueError('尚未完成 ACR 复核')
                            entry = app.verification_images[index]
                            path, expected = Path(entry['path']), entry['sha256']
                        else:
                            frame = target['frames'][index]
                            path = Path(target['preview_dir']) / frame['preview']
                            expected = frame['preview_sha256']
                        image = safe_read(path, 25 * 1024 * 1024)
                        if digest(image) != expected:
                            raise ValueError('原始预览文件已改变；已保存的目标仍可用于复核')
                        return self.send(200, image, 'image/jpeg')
                    if p is None or query['id'][0] != p.id:
                        raise ValueError('预览快照失效')
                    index = int(query['index'][0])
                    if not 0 <= index < len(p.frames):
                        raise ValueError('帧序号无效')
                    f = p.frames[index]
                    if query.get('kind', ['original'])[0] == 'verified':
                        if p.verified_images is None:
                            raise ValueError('尚未完成 ACR 复核')
                        entry = p.verified_images[index]
                        path, expected = Path(entry['path']), entry['sha256']
                    else:
                        path, expected = f.jpeg, f.jpeg_hash
                    data = safe_read(path, 25 * 1024 * 1024)
                    if digest(data) != expected:
                        raise ValueError('预览文件已改变，请重新分析')
                    return self.send(200, data, 'image/jpeg')
                return self.send(404, {'error': '不存在'})
            except Exception as e:
                return self.send(400, {'error': str(e)})

        def do_POST(self):
            if not self.authorized():
                return self.send(403, {'error': '本机会话或来源校验失败'})
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= 64 * 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.send(413, {'error': '请求类型或大小不支持'})
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('不允许非有限数字')))
                if not isinstance(data, dict):
                    raise ValueError('请求必须为对象')
                return self.send(200, app.dispatch(urlparse(self.path).path, data))
            except Exception as e:
                return self.send(400, {'error': str(e)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Time-lapse 本地曝光工作台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--workdir', type=Path, help='素材工作目录，输出保存到其中的 Time-lapse工作')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--verify-manifest', type=Path)
    parser.add_argument('--previews', type=Path)
    args = parser.parse_args()
    if args.verify_manifest:
        if not args.previews:
            parser.error('--verify-manifest 需要 --previews')
        manifest = json.loads(args.verify_manifest.read_text())
        result = verify_manifest(manifest, args.previews)
        result.pop('_images', None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    app = App(args.root, args.workdir)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(app))
    url = f'http://127.0.0.1:{server.server_port}/#token={app.token}'
    print(f'Time-lapse 本地工作台: {url}', flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.pool.shutdown(wait=True)

if __name__ == '__main__':
    main()
