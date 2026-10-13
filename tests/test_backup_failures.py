"""Adversarial cases at actual publication boundaries, using isolated media only."""
from datetime import datetime, timedelta, timezone
import errno
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_backups
from timelapse import backups
from timelapse.project import Project
from timelapse.server import App


class BackupFailureTests(unittest.TestCase):
    setUp = test_backups.BackupTests.setUp
    tearDown = test_backups.BackupTests.tearDown
    applied = test_backups.BackupTests.applied
    assert_original = test_backups.BackupTests.assert_original

    def test_acr_save_at_apply_exchange_is_retained(self):
        real=backups.exchange; edit=self.p.frames[0].source.updated(.75); changed=False
        def race(source,target,identity=None):
            nonlocal changed
            if not changed:
                changed=True
                temp=target.with_suffix('.new');temp.write_bytes(edit);temp.replace(target)
            return real(source,target,identity)
        with patch.object(backups,'exchange',side_effect=race),self.assertRaisesRegex(ValueError,'并发修改'):
            self.applied()
        entry=backups.list_backups(self.raw)['backups'][0];package=Path(entry['path'])
        self.assertIn(edit,[f.read_bytes() for f in (package/'retained').rglob('*.xmp')])
        self.assertEqual((package/'originals'/self.p.frames[0].xmp.name).read_bytes(),self.p.frames[0].source.data)
        with self.assertRaisesRegex(ValueError,'并发编辑'): backups.restore(self.raw,entry['id'])

    def test_acr_save_at_restore_exchange_is_retained(self):
        out=self.applied();real=backups.exchange; edit=self.p.frames[0].source.updated(.75); changed=False
        def race(source,target,identity=None):
            nonlocal changed
            if not changed:
                changed=True;target.write_bytes(edit)
            return real(source,target,identity)
        with patch.object(backups,'exchange',side_effect=race),self.assertRaisesRegex(ValueError,'并发修改'):
            backups.restore(self.raw,out['backup_id'])
        self.assertIn(edit,[f.read_bytes() for f in (Path(out['backup_path'])/'retained').rglob('*.xmp')])
        with self.assertRaisesRegex(ValueError,'并发编辑'): backups.restore(self.raw,out['backup_id'])

    def test_open_acr_descriptor_late_save_keeps_the_displaced_inode(self):
        frame=self.p.frames[0];edit=frame.source.updated(.75)
        with frame.xmp.open('r+b') as acr:
            out=self.applied()
            acr.seek(0);acr.write(edit);acr.truncate();acr.flush()
        package=Path(out['backup_path'])
        self.assertIn(edit,[f.read_bytes() for f in (package/'retained').rglob('*.xmp')])
        self.assertEqual((package/'originals'/frame.xmp.name).read_bytes(),frame.source.data)
        with self.assertRaisesRegex(ValueError,'并发编辑'): backups.restore(self.raw,out['backup_id'])

    def test_error_after_swap_keeps_outgoing_version_and_can_restore(self):
        real=backups.exchange
        def after(source,target,identity=None):
            real(source,target,identity)
            raise OSError('simulated sync failure after successful exchange')
        with patch.object(backups,'exchange',side_effect=after),self.assertRaisesRegex(ValueError,'未全部完成'):
            self.applied()
        entry=backups.list_backups(self.raw)['backups'][0]
        backups.restore(self.raw,entry['id']);self.assert_original()

    def test_final_receipt_failures_do_not_lose_recoverability(self):
        real=backups.save
        def fail_applied(path,m):
            if m['state']=='applied': raise OSError('disk full publishing result')
            return real(path,m)
        with patch.object(backups,'save',side_effect=fail_applied),self.assertRaisesRegex(ValueError,'未全部完成'):
            self.applied()
        entry=backups.list_backups(self.raw)['backups'][0]
        self.assertEqual(entry['state'],'applying')
        def fail_restored(path,m):
            if m['state']=='restored': raise OSError('disk full publishing restore')
            return real(path,m)
        with patch.object(backups,'save',side_effect=fail_restored),self.assertRaisesRegex(ValueError,'恢复中断'):
            backups.restore(self.raw,entry['id'])
        self.assert_original()
        backups.restore(self.raw,entry['id']);self.assert_original()

    def test_unsupported_filesystem_never_falls_back_to_overwrite(self):
        with patch.object(backups,'exchange',side_effect=OSError(errno.ENOTSUP,'unsupported')),self.assertRaisesRegex(ValueError,'未全部完成'):
            self.applied()
        self.assert_original()
        entry=backups.list_backups(self.raw)['backups'][0]
        backups.restore(self.raw,entry['id']);self.assert_original()

    def test_duplicate_snapshot_cannot_create_another_backup_even_after_restore(self):
        out=self.applied()
        with self.assertRaisesRegex(ValueError,'已经用于写回'): self.applied()
        self.assertEqual(len(backups.list_backups(self.raw)['backups']),1)
        backups.restore(self.raw,out['backup_id'])
        with self.assertRaisesRegex(ValueError,'已经用于写回'): self.applied()
        self.assertEqual(len(backups.list_backups(self.raw)['backups']),1)
        self.assert_original()

    def test_clock_rollback_does_not_reverse_restore_order(self):
        first=self.applied()
        p2=Project(self.raw,self.preview,srgb=True)
        p2.calculate({'selected':[], 'start':.1,'end':.1})
        yesterday=datetime.now(timezone.utc)-timedelta(days=1)
        with patch.object(backups,'datetime') as dt:
            dt.now.return_value=yesterday
            second=backups.apply(p2,'second')
        listing=backups.list_backups(self.raw)['backups']
        self.assertEqual(listing[0]['id'],second['backup_id'])
        with self.assertRaisesRegex(ValueError,'最新'): backups.restore(self.raw,first['backup_id'])
        backups.restore(self.raw,second['backup_id']);backups.restore(self.raw,first['backup_id']);self.assert_original()

    def test_writeback_has_no_dependency_on_project_output_directory(self):
        # Project output is deliberately unwritable as a directory. Apply must
        # still succeed, placing every recovery artifact below imported RAW.
        blocked=self.root/'project';blocked.write_text('not a directory')
        app=App(blocked);app.project=self.p
        try:
            job=app.dispatch('/api/apply',{'id':self.p.id,'revision':self.p.result['revision'],'order_confirmed':self.p.id,'acr_closed':True})
            app.pool.shutdown(wait=True)
            state=app.jobs[job['job']];self.assertEqual(state['state'],'done',state)
            out=state['result']
            self.assertEqual(Path(out['backup_path']).parent,self.raw/backups.FOLDER)
            self.assertEqual(blocked.read_text(),'not a directory')
            # The persisted target also remains usable for ACR verification.
            self.assertEqual(self.p.verify(self.preview,out['package_id'])['unchanged_previews'],30)
            backups.restore(self.raw,out['backup_id']);self.assert_original()
        finally: app.pool.shutdown()

    def test_legacy_backup_remains_restorable(self):
        out=self.applied();package=Path(out['backup_path']);m=json.loads((package/'manifest.json').read_text())
        m['schema']=1;m.pop('sequence');m.pop('snapshot_id');backups.save(package,m)
        backups.restore(self.raw,out['backup_id']);self.assert_original()

    def test_directory_replacement_before_exchange_cannot_redirect_write(self):
        root=self.raw; moved=root.with_name('moved-raw'); real=backups.exchange
        injected=False
        def race(source,target,identity=None):
            nonlocal injected
            if not injected:
                injected=True; root.rename(moved); root.mkdir()
                target.write_bytes(b'unrelated XMP in replacement directory')
                # Use the moved, real slot to isolate target identity validation.
                source=moved/source.relative_to(root)
            return real(source,target,identity)
        with patch.object(backups,'exchange',side_effect=race),self.assertRaisesRegex(ValueError,'身份已改变'):
            self.applied()
        self.assertEqual((root/self.p.frames[0].xmp.name).read_bytes(),b'unrelated XMP in replacement directory')
        for name,data in self.before.items(): self.assertEqual((moved/name).read_bytes(),data)

    def test_corrected_backup_corruption_between_frames_stops_before_using_it(self):
        real=backups.retain_replace;changed=False
        def corrupt(raw,package,name,data,allowed,identity):
            nonlocal changed
            result=real(raw,package,name,data,allowed,identity)
            if not changed:
                changed=True
                (package/'corrected'/self.p.frames[1].xmp.name).write_bytes(b'corruption')
            return result
        with patch.object(backups,'retain_replace',side_effect=corrupt),self.assertRaisesRegex(ValueError,'修正备份校验失败'):
            self.applied()
        self.assertEqual(self.p.frames[1].xmp.read_bytes(),self.p.frames[1].source.data)
        entry=backups.list_backups(self.raw)['backups'][0]
        backups.restore(self.raw,entry['id']);self.assert_original()

    def test_malformed_manifest_is_visible_and_blocks_new_writes(self):
        out=self.applied();package=Path(out['backup_path']);(package/'manifest.json').write_text('[]')
        self.assertEqual(backups.list_backups(self.raw)['backups'][0]['state'],'invalid')
        with self.assertRaisesRegex(ValueError,'损坏'): self.applied()
        for f in self.p.frames:
            self.assertEqual((package/'originals'/f.xmp.name).read_bytes(),f.source.data)

    def test_real_process_exit_after_swap_recovers_without_python_finally(self):
        script='''
import os,sys
from timelapse import backups
from timelapse.project import Project
real=backups.exchange
calls=0
def crash(source,target,identity=None):
 global calls
 real(source,target,identity)
 calls+=1
 if calls==3: os._exit(73)
backups.exchange=crash
if sys.argv[3]=='apply':
 p=Project(sys.argv[1],sys.argv[2],srgb=True)
 p.calculate({'selected':[],'start':.2,'end':.2})
 backups.apply(p,'child')
else:
 backups.restore(sys.argv[1],sys.argv[3])
'''
        cmd=[sys.executable,'-c',script,str(self.raw),str(self.preview)]
        self.assertEqual(subprocess.run(cmd+['apply'],capture_output=True,timeout=20).returncode,73)
        entry=backups.list_backups(self.raw)['backups'][0]
        self.assertEqual(entry['state'],'applying')
        self.assertEqual(subprocess.run(cmd+[entry['id']],capture_output=True,timeout=20).returncode,73)
        self.assertEqual(backups.list_backups(self.raw)['backups'][0]['state'],'restoring')
        backups.restore(self.raw,entry['id']);self.assert_original()
