from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from timelapse.folders import choose_folder
from timelapse.demo import create
from timelapse.project import Project

class FolderTests(unittest.TestCase):
    def test_same_folder_analysis_and_export(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); paths=create(root/'demo',30)
            raw=Path(paths['raw_dir']); preview=Path(paths['preview_dir'])
            for jpg in preview.iterdir(): jpg.rename(raw/jpg.name)
            p=Project(raw,raw,srgb=True)
            self.assertEqual(len(p.frames),30)
            r=p.calculate({'selected':[]})
            out=p.export(root/'out',r['revision'],p.id)
            self.assertEqual(out['count'],30)

    @patch('timelapse.folders.sys.platform','darwin')
    def test_cancel_and_special_path(self):
        with patch('timelapse.folders.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='\n')):
            self.assertEqual(choose_folder('raw')['path'],None)
        with tempfile.TemporaryDirectory(prefix='中文 space " quote ') as d:
            path=Path(d).resolve()
            with patch('timelapse.folders.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=str(path)+'/\n')) as run:
                self.assertEqual(choose_folder('previews',str(path))['path'],str(path))
                self.assertEqual(run.call_args.args[0][-1],str(path))
                self.assertNotIn(str(path),run.call_args.args[0][2])

    def test_invalid_target(self):
        with self.assertRaises(ValueError): choose_folder('unexpected')

if __name__=='__main__': unittest.main()
