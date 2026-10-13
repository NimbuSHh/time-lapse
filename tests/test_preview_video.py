"""Tiny synthetic sRGB clips only; no Adobe, real media or installed tools changed."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from timelapse import preview_video as video
from timelapse.photoshop import Cancelled


def probe_fixture():
    manifest = {'frame_count':3,'fps_rational':'25','width':1280,'height':854}
    stream = {'codec_type':'video','codec_name':'h264','width':1280,'height':854,
              'avg_frame_rate':'25/1','nb_read_frames':'3','duration':'0.12',
              'pix_fmt':'yuv420p','color_range':'tv','color_space':'bt709',
              'color_transfer':'iec61966-2-1','color_primaries':'bt709'}
    return manifest, {'streams':[stream],'format':{'duration':'0.12'}}


class ProbeChecks(unittest.TestCase):
    def test_decoded_counts_duration_dimensions_rate_and_color_are_all_required(self):
        manifest, probe = probe_fixture()
        self.assertEqual(video._validate_probe(probe,manifest)['frame_count'],3)
        for field,value in [('nb_read_frames','2'),('avg_frame_rate','24/1'),('duration','0.3'),
                            ('width',1920),('height',855),('color_transfer','bt709'),
                            ('color_range','pc'),('color_space','smpte170m'),
                            ('color_primaries','unknown'),('pix_fmt','yuv444p')]:
            with self.subTest(field=field):
                changed = copy.deepcopy(probe); changed['streams'][0][field] = value
                with self.assertRaises(ValueError): video._validate_probe(changed,manifest)
        changed = copy.deepcopy(probe); changed['streams'].append({'codec_type':'audio'})
        with self.assertRaises(ValueError): video._validate_probe(changed,manifest)
        changed = copy.deepcopy(probe); del changed['streams'][0]['nb_read_frames']
        changed['streams'][0]['nb_frames'] = '3'
        with self.assertRaises(ValueError): video._validate_probe(changed,manifest)

    def test_duration_tolerance_is_at_most_one_frame(self):
        manifest, probe = probe_fixture()
        probe['streams'][0]['duration'] = '0.16'
        video._validate_probe(probe,manifest)
        probe['streams'][0]['duration'] = '0.161'
        with self.assertRaises(ValueError): video._validate_probe(probe,manifest)

    def test_cancel_terminates_only_the_process_it_started(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            children = []
            popen = subprocess.Popen
            def owned(*args,**kwargs):
                process = popen(*args,**kwargs); children.append(process); return process
            started = time.monotonic()
            with patch.object(video.subprocess,'Popen',side_effect=owned):
                with self.assertRaises(Cancelled):
                    video._run_process([sys.executable,'-c','import time; time.sleep(30)'],work,
                                       cancelled=lambda:time.monotonic()-started>.1)
            self.assertEqual(len(children),1)
            self.assertIsNotNone(children[0].poll())


class EncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg, cls.ffprobe = video._tools()
        except ValueError as exc:
            raise unittest.SkipTest(str(exc))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.folder = self.root/'output'; self.entries = []
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        # Deliberately nonalphabetic order: encoding must use list order.
        self.colors = [(48,48,48),(110,90,70),(180,180,180)]
        for name,color in zip(('z-last-name','a-first-name','m-middle'),self.colors):
            path = self.root/(name+'.jpg')
            Image.new('RGB',(96,64),color).save(path,quality=100,subsampling=0,icc_profile=profile)
            self.entries.append({'jpg':str(path),'sha256':video._file_hash(path),'recipe_sha256':'a'*64})
        self.originals = {entry['jpg']:Path(entry['jpg']).read_bytes() for entry in self.entries}

    def tearDown(self): self.tmp.cleanup()

    def test_real_clip_has_all_ordered_frames_correct_gamma_and_review_only_state(self):
        result = video.encode_preview(self.entries,self.folder)
        self.assertEqual(result['state'],'awaiting-user-review')
        self.assertTrue(result['encoded_verified']); self.assertNotIn('approved',result)
        self.assertNotIn('verified',result)
        self.assertEqual((result['frame_count'],result['fps'],result['width'],result['height']),(3,25.,1280,854))
        self.assertAlmostEqual(result['duration_seconds'],3/25)
        self.assertEqual(result['color_transfer'],'iec61966-2-1')
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual([e['jpg'] for e in manifest['frames']],[e['jpg'] for e in self.entries])
        self.assertEqual(video._file_hash(result['manifest']),result['manifest_sha256'])
        self.assertEqual(video._file_hash(result['video']),result['sha256'])
        # Actually decode all frames to RGB. Mean channel values catch order,
        # full/limited range errors, and an accidental sRGB->709 gamma remap.
        decoded = subprocess.run([str(self.ffmpeg),'-v','error','-i',result['video'],
                                  '-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                                 check=True,capture_output=True,timeout=15).stdout
        rgb = np.frombuffer(decoded,dtype=np.uint8).reshape(3,854,1280,3)
        expected = np.array([np.array(Image.open(e['jpg'])).mean(axis=(0,1)) for e in self.entries])
        np.testing.assert_allclose(rgb.mean(axis=(1,2)),expected,atol=2.5,rtol=0)
        for path,data in self.originals.items(): self.assertEqual(Path(path).read_bytes(),data)
        self.assertEqual(list(self.folder.glob('.preview-incomplete-*')),[])
        self.assertEqual({p.name for p in Path(result['video']).parent.iterdir()},
                         {'preview.mp4','manifest.json','receipt.json'})

    def test_same_manifest_reuses_but_changed_recipe_never_overwrites_old_video(self):
        first = video.encode_preview(self.entries,self.folder)
        before = Path(first['video']).read_bytes()
        with patch.object(video,'_run_process',wraps=video._run_process) as processes:
            again = video.encode_preview(self.entries,self.folder)
        self.assertTrue(again['reused']); self.assertEqual(first['video'],again['video'])
        self.assertTrue(all(Path(call.args[0][0]) == self.ffprobe for call in processes.call_args_list))
        entries = copy.deepcopy(self.entries); entries[1]['recipe_sha256'] = 'b'*64
        changed = video.encode_preview(entries,self.folder)
        self.assertNotEqual(first['video'],changed['video'])
        self.assertNotEqual(first['manifest_sha256'],changed['manifest_sha256'])
        self.assertEqual(first['ordered_frame_sha256'],changed['ordered_frame_sha256'])
        self.assertNotEqual(first['ordered_recipe_sha256'],changed['ordered_recipe_sha256'])
        self.assertEqual(Path(first['video']).read_bytes(),before)

    def test_bad_source_hash_or_recipe_fails_before_process_start(self):
        for field,value in [('sha256','0'*64),('recipe_sha256','not-a-hash'),('jpg','relative.jpg')]:
            with self.subTest(field=field):
                entries = copy.deepcopy(self.entries); entries[0][field] = value
                with patch.object(video,'_run_process') as process:
                    with self.assertRaises(ValueError): video.encode_preview(entries,self.folder)
                    process.assert_not_called()

    def test_wrong_profile_or_dimensions_is_not_silently_converted(self):
        path = Path(self.entries[0]['jpg'])
        Image.new('RGB',(96,64),(80,80,80)).save(path)
        entries = copy.deepcopy(self.entries); entries[0]['sha256'] = video._file_hash(path)
        with self.assertRaisesRegex(ValueError,'sRGB'): video.encode_preview(entries,self.folder)
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGB',(98,64),(80,80,80)).save(path,icc_profile=profile)
        entries[0]['sha256'] = video._file_hash(path)
        with self.assertRaisesRegex(ValueError,'尺寸'): video.encode_preview(entries,self.folder)

    def test_cancel_removes_only_own_incomplete_output(self):
        self.folder.mkdir(); keep = self.folder/'existing-video.mp4'; keep.write_bytes(b'keep')
        with patch.object(video,'_run_process',side_effect=Cancelled('stop synthetic encode')):
            with self.assertRaises(Cancelled): video.encode_preview(self.entries,self.folder)
        self.assertEqual(keep.read_bytes(),b'keep')
        self.assertEqual(list(self.folder.glob('.preview-incomplete-*')),[])
        self.assertEqual(list(self.folder.glob('preview-*')),[])
        for path,data in self.originals.items(): self.assertEqual(Path(path).read_bytes(),data)

    def test_source_changed_after_encoding_is_detected_before_publication(self):
        probe = video._probe
        def changed(*args,**kwargs):
            result = probe(*args,**kwargs)
            with Path(self.entries[0]['jpg']).open('ab') as stream: stream.write(b'changed')
            return result
        with patch.object(video,'_probe',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'源文件.*改变'):
                video.encode_preview(self.entries,self.folder)
        self.assertEqual(list(self.folder.glob('preview-*')),[])
        self.assertEqual(list(self.folder.glob('.preview-incomplete-*')),[])

    def test_corrupt_cached_video_is_rejected_and_never_replaced(self):
        first = video.encode_preview(self.entries,self.folder)
        path = Path(first['video']); path.write_bytes(b'corrupt-cache')
        with patch.object(video,'_run_process') as process:
            with self.assertRaisesRegex(ValueError,'哈希不符'):
                video.encode_preview(self.entries,self.folder)
            process.assert_not_called()
        self.assertEqual(path.read_bytes(),b'corrupt-cache')

    def test_cancel_before_start_has_no_outputs(self):
        with self.assertRaises(Cancelled):
            video.encode_preview(self.entries,self.folder,cancelled=lambda:True)
        self.assertFalse(self.folder.exists())


if __name__ == '__main__': unittest.main()
