"""Owned server lifecycle and offline self-test for the macOS application."""
import argparse
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading

from .server import App, handler
from .workspace import PROJECT_ROOT


class DesktopService:
    def __init__(self):
        # Never use Finder's cwd (often /) as a selected output workspace.
        self.app = App(PROJECT_ROOT)
        try:
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        except BaseException:
            self.app.pool.shutdown(wait=True)
            raise
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/#token={self.app.token}'

    def close(self):
        # Only this app's workers are stopped. Adobe writes finish or lose their
        # lease through the existing cancellation path; task receipts survive.
        self.app.auto_cancel.set()
        self.app.trajectory_cancel.set()
        self.app.refine_cancel.set()
        self.server.shutdown()
        self.thread.join()
        self.app.pool.shutdown(wait=True, cancel_futures=True)
        self.server.server_close()


def self_test():
    """Exercise bundled numerical/image/video/HTTP resources; never call Adobe."""
    import numpy as np
    from PIL import Image
    from .demo import create
    from .project import Project, digest, safe_read
    from .preview_video import _tools, encode_preview
    from .photoshop import SCRIPT

    ffmpeg, ffprobe = _tools()
    if getattr(sys, 'frozen', False):
        for path in (ffmpeg, ffprobe, SCRIPT):
            if PROJECT_ROOT not in path.resolve().parents:
                raise RuntimeError('Resource escaped the bundled runtime')
    if not SCRIPT.is_file():
        raise RuntimeError('Missing Photoshop bridge')
    with tempfile.TemporaryDirectory(prefix='timelapse-self-test-') as temporary:
        root = Path(temporary).resolve()
        paths = create(root/'synthetic', 30)
        project = Project(**paths, srgb=True)
        result = project.calculate({'selected': [15], 'strength': 1.0})
        if not np.isfinite(result['target']).all() or result['blocked']:
            raise RuntimeError('Bundled numerical pipeline failed')
        entries = [{'jpg': str(frame.jpeg), 'sha256': digest(safe_read(frame.jpeg, 25*1024*1024)),
                    'recipe_sha256': 'a'*64} for frame in project.frames[:3]]
        clip = encode_preview(entries, root/'video')
        if not clip['encoded_verified'] or clip['frame_count'] != 3:
            raise RuntimeError('Bundled video round trip failed')
        service = DesktopService()
        try:
            def get(route, authorized=False):
                connection = http.client.HTTPConnection('127.0.0.1', service.server.server_port, timeout=5)
                connection.request('GET', route, headers={'X-Timelapse-Token': service.app.token} if authorized else {})
                response = connection.getresponse()
                body = response.read()
                status = response.status
                connection.close()
                return status, body
            if get('/')[0] != 200 or get('/auto.js')[0] != 200 or get('/api/config')[0] != 403:
                raise RuntimeError('Bundled browser assets or authorization failed')
            status, body = get('/api/config', True)
            if status != 200 or json.loads(body)['workspace_explicit']:
                raise RuntimeError('Finder startup selected an unintended workspace')
            for route in ('/api/auto/status', '/api/trajectory/status'):
                status, body = get(route, True)
                if status != 200 or json.loads(body) != {'task': None, 'job': None}:
                    raise RuntimeError('Fresh app must show an empty task without an error')
        finally:
            service.close()
    return {'ok': True, 'frozen': bool(getattr(sys, 'frozen', False)),
            'python': sys.version.split()[0], 'numpy': np.__version__, 'pillow': Image.__version__,
            'ffmpeg': str(ffmpeg), 'ffprobe': str(ffprobe), 'synthetic_frames': 30,
            'encoded_frames': 3, 'http_and_auth': True, 'adobe_called': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--parent-pid', type=int)
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False), flush=True)
        return
    stopping = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopping.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    service = None
    try:
        service = DesktopService()
        print('TIMELAPSE_READY '+service.url, flush=True)
        while not stopping.wait(.25):
            if args.parent_pid is not None and os.getppid() != args.parent_pid:
                break
            if not service.thread.is_alive():
                raise RuntimeError('Local server stopped unexpectedly')
    finally:
        if service is not None:
            service.close()
        for sig, previous_handler in previous.items():
            signal.signal(sig, previous_handler)


if __name__ == '__main__':
    main()
