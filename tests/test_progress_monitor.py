import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from timelapse.progress_monitor import Monitor, handler


class ProgressMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.run=Path(self.temp.name)
        self.state={'id':'test','path':str(self.run),'raw_dir':'/source','state':'running',
                    'phase':'第 1 轮自动显影 · 高光','done':69,'total':810,'count':810,'snapshot':['private-originals']}
        self.path=self.run/'run.json'; self.path.write_text(json.dumps(self.state))
        self.monitor=Monitor(self.run)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.monitor))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
    def call(self,path='/',method='GET',token=True,**headers):
        if token: headers['X-Timelapse-Token']=self.monitor.token
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method,path,headers=headers); response=conn.getresponse(); data=response.read(); conn.close()
        return response.status,data
    def test_reads_live_record_without_mutating_it_or_exposing_snapshot(self):
        before=self.path.read_bytes(); stamp=self.path.stat().st_mtime_ns
        code,raw=self.call('/api/progress'); data=json.loads(raw)
        self.assertEqual(code,200); self.assertEqual(data['done'],69); self.assertNotIn('snapshot',data)
        self.assertEqual(self.path.read_bytes(),before); self.assertEqual(self.path.stat().st_mtime_ns,stamp)
        self.state['done']=70; self.path.write_text(json.dumps(self.state))
        self.assertEqual(json.loads(self.call('/api/progress')[1])['done'],70)
        self.assertEqual(list(self.run.iterdir()),[self.path])
    def test_no_worker_control_or_untrusted_cross_origin_access(self):
        self.assertEqual(self.call('/api/auto/start',method='POST')[0],405)
        self.assertEqual(self.call('/api/auto/cancel',method='POST')[0],405)
        self.assertEqual(self.call('/api/progress',token=False)[0],403)
        self.assertEqual(self.call('/api/progress',Origin='https://example.com')[0],403)
        self.assertEqual(self.call('/api/progress',Host='evil.example')[0],403)
        self.assertEqual(self.call('/../run.json')[0],404)
        self.assertNotIn(self.monitor.token.encode(),self.call('/')[1])
    def test_missing_record_is_not_reported_as_success(self):
        self.path.unlink()
        self.assertEqual(self.call('/api/progress')[0],503)
