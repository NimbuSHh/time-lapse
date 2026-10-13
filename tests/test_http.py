import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from timelapse.server import App, handler

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.app=App(Path(cls.tmp.name))
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler(cls.app))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.app.pool.shutdown(); cls.tmp.cleanup()
    def call(self,path='/',method='GET',headers=None,body=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        c.request(method,path,body,headers or {}); r=c.getresponse(); data=r.read(); c.close(); return r.status,data,r.headers
    def test_static_csp_no_secret(self):
        code,body,headers=self.call()
        self.assertEqual(code,200); self.assertNotIn(self.app.token.encode(),body)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
    def test_no_token(self): self.assertEqual(self.call('/api/job?id=x')[0],403)
    def test_output_location_requires_token(self):
        self.assertEqual(self.call('/api/config')[0],403)
        code,body,_=self.call('/api/config',headers={'X-Timelapse-Token':self.app.token})
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['output_root'],str((self.app.root/'Time-lapse工作').resolve()))
        self.assertEqual(json.loads(body)['joint_api_version'], 1)
    def test_unknown_route_is_not_misreported_as_expired_analysis(self):
        for route in ('/api/unknown', '/api/joint/future-feature'):
            code, body, _ = self.call(route, 'POST', {'X-Timelapse-Token':self.app.token, 'Content-Type':'application/json'}, '{}')
            self.assertEqual(code, 400)
            self.assertIn('重新启动工作台', json.loads(body)['error'])
            self.assertNotIn('快照', json.loads(body)['error'])
    def test_assets_stay_paired_with_running_backend(self):
        expected = self.call('/app.js')[1]
        with patch.object(Path, 'read_bytes', return_value=b'newer disk version'):
            self.assertEqual(self.call('/app.js')[1], expected)
            fresh = App(Path(self.tmp.name))
            self.assertEqual(fresh.static_assets['app.js'], b'newer disk version')
            fresh.pool.shutdown()
    def test_wrong_host(self): self.assertEqual(self.call(headers={'Host':'evil.example'})[0],403)
    def test_wrong_origin(self):
        self.assertEqual(self.call('/api/demo','POST',{'X-Timelapse-Token':self.app.token,'Origin':'http://evil.example','Content-Type':'application/json'},'{}')[0],403)
    def test_traversal_and_request_size(self):
        self.assertEqual(self.call('/../pyproject.toml',headers={'X-Timelapse-Token':self.app.token})[0],404)
        self.assertEqual(self.call('/api/demo','POST',{'X-Timelapse-Token':self.app.token,'Content-Type':'application/json'},'x'*65537)[0],413)
    def test_nonfinite(self):
        self.assertEqual(self.call('/api/analyze','POST',{'X-Timelapse-Token':self.app.token,'Content-Type':'application/json'},'{"x": NaN}')[0],400)


class DurableVerificationHTTPTests(unittest.TestCase):
    def test_restored_target_api_verifies_and_serves_images_without_live_project(self):
        import time
        from timelapse.demo import create
        from timelapse.project import Project
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = create(root / 'demo', count=30)
            old = App(root)
            project = Project(**paths)
            project.calculate({'selected': [15]})
            old.save_verification_target(project)
            old.pool.shutdown()
            app = App(root)
            self.assertIsNotNone(app.project)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def call(path, data=None, authorized=True):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                headers = {'X-Timelapse-Token': app.token} if authorized else {}
                if data is not None:
                    headers['Content-Type'] = 'application/json'
                connection.request('GET' if data is None else 'POST', path,
                                   None if data is None else json.dumps(data), headers)
                response = connection.getresponse()
                code, body = response.status, response.read()
                connection.close()
                return code, body
            try:
                self.assertEqual(call('/api/verification-target', authorized=False)[0], 403)
                code, body = call('/api/verification-target')
                self.assertEqual(code, 200)
                state = json.loads(body)
                self.assertEqual(state['target']['result'], project.result)
                self.assertTrue(state['editable'])
                image_url = f'/api/image?id={project.id}&index=0'
                self.assertEqual(call(image_url), (200, project.frames[0].jpeg.read_bytes()))
                code, body = call('/api/verify', {'id': project.id,
                    'revision': project.result['revision'], 'preview_dir': paths['preview_dir']})
                self.assertEqual(code, 200)
                key = json.loads(body)['job']
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    code, body = call('/api/job?id=' + key)
                    job = json.loads(body)
                    if job['state'] != 'running':
                        break
                    time.sleep(.01)
                self.assertEqual(job['state'], 'done', job)
                self.assertEqual(len(job['result']['actual']), 30)
                self.assertEqual(call(image_url + '&kind=verified'), (200, project.frames[0].jpeg.read_bytes()))
            finally:
                server.shutdown()
                server.server_close()
                app.pool.shutdown()


if __name__ == '__main__':
    unittest.main()
