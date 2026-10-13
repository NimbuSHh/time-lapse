"""Authenticated review-only HTTP surface, with rendering mocked."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from timelapse.server import App,handler
from timelapse.project import digest


class TrajectoryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.app))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.app.trajectory_cancel.set();self.server.shutdown();self.server.server_close();self.app.pool.shutdown();self.tmp.cleanup()

    def call(self,path,data=None,auth=True):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers={'X-Timelapse-Token':self.app.token} if auth else {}
        if data is not None:headers['Content-Type']='application/json'
        c.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),headers)
        r=c.getresponse();out=(r.status,r.read());c.close();return out

    def test_authentication_and_static_review_page(self):
        self.assertEqual(self.call('/trajectory',auth=False)[0],200)
        self.assertIn('待确认的完整视频'.encode(),self.call('/trajectory')[1])
        for path,data in [('/api/trajectory/status',None),('/api/trajectory/start',{}),('/api/trajectory/cancel',{}),('/api/trajectory/video',None),('/api/trajectory/export',{}),('/api/trajectory/reference',None)]:
            self.assertEqual(self.call(path,data,False)[0],403)

    def test_start_cannot_pass_approval_or_export_parameters(self):
        with patch('timelapse.trajectory.load_latest',return_value=None),patch('timelapse.workflow.resume',return_value={'state':'awaiting-user-review','verified':False}) as run:
            code,body=self.call('/api/trajectory/start',{'approved':True,'export_full':True,'output':'/untrusted'})
            self.assertEqual(code,200);key=json.loads(body)['job']
            for _ in range(100):
                if self.app.jobs[key]['state']!='running':break
                time.sleep(.01)
            self.assertEqual(run.call_args.args,(self.app.work_output(),))
            self.assertEqual(set(run.call_args.kwargs),{'progress','cancelled'})
            status=json.loads(self.call('/api/trajectory/status')[1]);self.assertEqual(status['task']['state'],'awaiting-user-review')
            self.assertFalse(status['task']['result']['verified'])
            self.assertEqual(self.call('/api/trajectory/export',{})[0],400)

    def test_video_hash_and_path_are_checked_before_serving(self):
        folder=self.root/'task';folder.mkdir();video=folder/'preview.mp4';video.write_bytes(b'video')
        state={'state':'awaiting-user-review','path':str(folder),'result':{'video':str(video),'sha256':digest(b'video')}}
        with patch('timelapse.trajectory.load_latest',return_value=state):
            self.assertEqual(self.call('/api/trajectory/video'),(200,b'video'))
            video.write_bytes(b'changed');self.assertEqual(self.call('/api/trajectory/video')[0],400)
            outside=self.root/'outside.mp4';outside.write_bytes(b'video');state['result']['video']=str(outside)
            self.assertEqual(self.call('/api/trajectory/video')[0],400)

    def test_new_raw_start_uses_new_pipeline_and_cannot_approve_export(self):
        source=self.root/'new-source';source.mkdir()
        with patch('timelapse.workflow.run',return_value={'state':'awaiting-user-review'}) as new, patch('timelapse.automatic.run') as legacy:
            code,body=self.call('/api/auto/start',{'raw_dir':str(source),'confirmed':True,'export_full':True})
            self.assertEqual(code,200)
            key=json.loads(body)['job']
            for _ in range(100):
                if self.app.jobs[key]['state']!='running':break
                time.sleep(.01)
            legacy.assert_not_called()
            self.assertEqual(new.call_args.args[0],source.resolve())
            self.assertEqual(set(new.call_args.kwargs),{'regions','progress','cancelled'})
            config=json.loads(self.call('/api/config')[1])
            self.assertEqual(config['automatic_api_version'],2)
            self.assertEqual(config['trajectory_api_version'],2)

    def test_export_requires_explicit_binding_and_forwards_only_binding(self):
        with patch('timelapse.workflow.export',return_value={'state':'done'}) as export:
            for body in ({},{'confirmed':1},{'confirmed':True}):
                self.assertEqual(self.call('/api/trajectory/export',body)[0],400)
            export.assert_not_called()
            binding={'confirmed':True,'run_id':'run','video_sha256':'video','selection_sha256':'frames','target_sha256':'target','effective_policy_sha256':'policy'}
            code,body=self.call('/api/trajectory/export',{**binding,'output':'/unexpected'})
            self.assertEqual(code,200)
            key=json.loads(body)['job']
            for _ in range(100):
                if self.app.jobs[key]['state']!='running':break
                time.sleep(.01)
            self.assertEqual(export.call_args.args[1],binding)

    def test_completed_job_cannot_leak_to_another_workspace(self):
        self.app.trajectory_job='old'
        self.app.trajectory_output='/another-workspace'
        self.app.jobs={'old':{'state':'done','result':{'video':'old-video'}}}
        state=json.loads(self.call('/api/trajectory/status')[1])
        self.assertIsNone(state['task'])


if __name__=='__main__':unittest.main()
