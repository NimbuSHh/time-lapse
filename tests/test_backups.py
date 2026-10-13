import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from timelapse import backups
from timelapse.demo import create
from timelapse.project import Project, digest
from timelapse.server import App


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        paths=create(self.root/'demo',30)
        self.raw=Path(paths['raw_dir']).resolve()
        self.preview=Path(paths['preview_dir']).resolve()
        self.p=Project(self.raw,self.preview,srgb=True)
        self.p.calculate({'selected':[], 'strength':.6,'window':15,'start':.2,'end':.2})
        self.before={p.name:p.read_bytes() for p in self.raw.iterdir() if p.is_file()}

    def tearDown(self): self.tmp.cleanup()

    def applied(self): return backups.apply(self.p,'test-package')

    def assert_original(self):
        for name,data in self.before.items(): self.assertEqual((self.raw/name).read_bytes(),data)

    def test_write_restore_after_restart_preserves_raw_and_xmp_bytes(self):
        result=self.applied()
        package=Path(result['backup_path'])
        for f,value in zip(self.p.frames,self.p.result['final']):
            self.assertEqual((package/'originals'/f.xmp.name).read_bytes(),self.before[f.xmp.name])
            self.assertEqual(f.xmp.read_bytes(),f.source.updated(value))
            self.assertEqual(f.raw.read_bytes(),self.before[f.raw.name])
        self.assertEqual(backups.list_backups(self.raw)['backups'][0]['state'],'applied')
        backups.restore(str(self.raw),result['backup_id'])
        self.assert_original()
        self.assertTrue(package.exists())
        self.assertEqual(backups.list_backups(self.raw)['backups'][0]['state'],'restored')
        with self.assertRaisesRegex(ValueError,'已经恢复'): backups.restore(self.raw,result['backup_id'])

    def test_changed_source_rejected_before_backup(self):
        self.p.frames[-1].xmp.write_bytes(b'new edits')
        with self.assertRaisesRegex(ValueError,'源文件已改变'): self.applied()
        self.assertEqual(self.p.frames[0].xmp.read_bytes(),self.before[self.p.frames[0].xmp.name])

    def test_all_originals_exist_before_first_write(self):
        real=backups.retain_replace
        def checked(raw,package,name,data,allowed,identity):
            if raw==self.raw:
                listing=backups.list_backups(self.raw)['backups']
                package=Path(listing[0]['path'])
                for f in self.p.frames:
                    self.assertEqual((package/'originals'/f.xmp.name).read_bytes(),f.source.data)
                self.assertTrue((package/'manifest.json').exists())
            return real(raw,package,name,data,allowed,identity)
        with patch.object(backups,'retain_replace',side_effect=checked): self.applied()

    def test_interrupted_write_recoverable_and_blocks_next_apply(self):
        real=backups.exchange; count=0
        def fail(source,target,identity=None):
            nonlocal count
            if target.parent==self.raw:
                count+=1
                if count==3: raise OSError('simulated disk failure')
            return real(source,target,identity)
        with patch.object(backups,'exchange',side_effect=fail), self.assertRaisesRegex(ValueError,'未全部完成'): self.applied()
        entry=backups.list_backups(self.raw)['backups'][0]
        self.assertEqual(entry['state'],'applying')
        with self.assertRaisesRegex(ValueError,'未完成'): self.applied()
        backups.restore(self.raw,entry['id']); self.assert_original()

    def test_restore_interruption_can_resume(self):
        result=self.applied(); real=backups.exchange; count=0
        def fail(source,target,identity=None):
            nonlocal count
            if target.parent==self.raw:
                count+=1
                if count==3: raise OSError('simulated disk failure')
            return real(source,target,identity)
        with patch.object(backups,'exchange',side_effect=fail), self.assertRaisesRegex(ValueError,'恢复中断'):
            backups.restore(self.raw,result['backup_id'])
        backups.restore(self.raw,result['backup_id']); self.assert_original()

    def test_backup_failure_never_changes_inputs_or_blocks_retry(self):
        real=backups.atomic
        def fail(path,data):
            if path.parent.name=='originals': raise OSError('no space')
            return real(path,data)
        with patch.object(backups,'atomic',side_effect=fail), self.assertRaises(OSError): self.applied()
        self.assert_original()
        self.assertEqual(backups.list_backups(self.raw)['backups'],[])
        self.applied()

    def test_concurrent_writer_rejected(self):
        with backups.locked(self.raw), self.assertRaisesRegex(ValueError,'正在写回'):
            self.applied()
        self.assert_original()

    def test_external_edit_or_corrupt_backup_blocks_whole_restore(self):
        result=self.applied(); current={f.xmp.name:f.xmp.read_bytes() for f in self.p.frames}
        last=self.p.frames[-1].xmp; last.write_bytes(b'ACR new edits')
        with self.assertRaisesRegex(ValueError,'被修改'): backups.restore(self.raw,result['backup_id'])
        self.assertEqual(self.p.frames[0].xmp.read_bytes(),current[self.p.frames[0].xmp.name])
        last.write_bytes(current[last.name])
        (Path(result['backup_path'])/'originals'/last.name).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'校验失败'): backups.restore(self.raw,result['backup_id'])
        self.assertEqual(self.p.frames[0].xmp.read_bytes(),current[self.p.frames[0].xmp.name])

    def test_symlink_and_traversal_rejected(self):
        outside=self.root/'outside'; outside.mkdir()
        (self.raw/backups.FOLDER).symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError): self.applied()
        self.assertEqual(list(outside.iterdir()),[])
        (self.raw/backups.FOLDER).unlink()
        result=self.applied()
        with self.assertRaisesRegex(ValueError,'编号无效'): backups.restore(self.raw,'../outside')
        original=Path(result['backup_path'])/'originals'
        original.rename(original.with_name('held'))
        original.symlink_to(original.with_name('held'),target_is_directory=True)
        with self.assertRaises(ValueError): backups.restore(self.raw,result['backup_id'])

    def test_multiple_versions_restore_newest_first(self):
        first=self.applied()
        p2=Project(self.raw,self.preview,srgb=True)
        p2.calculate({'selected':[], 'strength':.6,'window':15,'start':.1,'end':.1})
        second=backups.apply(p2,'second')
        with self.assertRaisesRegex(ValueError,'最新'): backups.restore(self.raw,first['backup_id'])
        backups.restore(self.raw,second['backup_id'])
        backups.restore(self.raw,first['backup_id']); self.assert_original()

    def test_routes_require_acr_confirmation_and_restore_without_project(self):
        app=App(self.root)
        try:
            with self.assertRaisesRegex(ValueError,'关闭 ACR'): app.dispatch('/api/restore',{})
            app.project=self.p
            with self.assertRaisesRegex(ValueError,'关闭 ACR'): app.dispatch('/api/apply',{'id':self.p.id})
            result=self.applied(); app.project=None
            task=app.dispatch('/api/restore',{'raw_dir':str(self.raw),'backup_id':result['backup_id'],'acr_closed':True})
            app.pool.shutdown(wait=True)
            self.assertEqual(app.jobs[task['job']]['state'],'done')
            self.assert_original()
        finally: app.pool.shutdown()
