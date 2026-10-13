"""Read-only companion for a running job; never imports or controls its worker."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

STATIC = Path(__file__).parent/'static'


class Monitor:
    def __init__(self, run):
        self.run = Path(run).resolve()
        self.token = secrets.token_urlsafe(32)
        self.assets = {name: (STATIC/name).read_bytes() for name in
                       ('monitor.html', 'monitor.js', 'monitor.css', 'progress.js')}
        self.read()

    def read(self):
        path = self.run/'run.json'
        with path.open('rb') as file:
            raw = file.read(32*1024*1024+1)
        if len(raw)>32*1024*1024:
            raise ValueError('任务记录过大')
        state = json.loads(raw)
        if Path(state['path']).resolve() != self.run:
            raise ValueError('任务路径与指定任务不符')
        data = {key: state.get(key) for key in
                ('id', 'state', 'phase', 'count', 'done', 'total', 'error', 'result')}
        data['updated_at'] = path.stat().st_mtime
        data['observed_at'] = time.time()
        data['name'] = Path(state['raw_dir']).name
        return data


def handler(monitor):
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
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_GET(self):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:
                return self.send(403, {'error':'Host 不允许'})
            route = urlparse(self.path).path
            asset = 'monitor.html' if route=='/' else route[1:]
            if asset in monitor.assets:
                mime = 'text/html; charset=utf-8' if asset.endswith('.html') else 'text/css; charset=utf-8' if asset.endswith('.css') else 'text/javascript; charset=utf-8'
                return self.send(200, monitor.assets[asset], mime)
            if (self.headers.get('Origin') not in (None, 'http://'+host)
                    or not secrets.compare_digest(self.headers.get('X-Timelapse-Token',''),monitor.token)):
                return self.send(403, {'error':'请使用本次进度页的完整启动链接'})
            if route != '/api/progress': return self.send(404, {'error':'不存在'})
            try: return self.send(200, monitor.read())
            except Exception as exc: return self.send(503, {'error':str(exc)})
        def do_POST(self):
            return self.send(405, {'error':'此页面只读，不控制处理任务'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='查看现有自动任务，不启动或修改任何处理任务')
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    monitor = Monitor(args.run)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(monitor))
    print(f'只读进度页: http://127.0.0.1:{server.server_port}/#token={monitor.token}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
