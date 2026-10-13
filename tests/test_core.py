import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from timelapse.xmp import parse, CRS, RDF
from timelapse.analysis import infer, correct, smooth, measure
from timelapse.project import Project, digest, verify_manifest, safe_read
from timelapse.demo import create


def xmp(contents, extra=''):
    return f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><r:RDF xmlns:r="{RDF}"><r:Description xmlns:c="{CRS}" {extra}>{contents}</r:Description></r:RDF></x:xmpmeta>'.encode()

class XMPTests(unittest.TestCase):
    def test_attribute_preserves_all_other_bytes(self):
        data = xmp('<!-- c:Exposure2012="9" --><c:Temperature>5200</c:Temperature>', 'c:Exposure2012=" +0.5 " c:Tint="8"')
        original = parse(data)
        out = original.updated(1.25)
        a,b = original.span
        self.assertEqual(out, data[:a]+b'+1.250000'+data[b:])
        self.assertEqual(parse(out).exposure, 1.25)

    def test_child_prefix_bom_and_snapshot(self):
        data = b'\xef\xbb\xbf' + xmp('<c:Exposure2012>-.25</c:Exposure2012><c:Snapshots><r:Seq><r:li c:Exposure2012="2"/></r:Seq></c:Snapshots>')
        self.assertEqual(parse(data).exposure, -.25)
        self.assertIn(b'c:Exposure2012="2"', parse(data).updated(.1))

    def test_other_namespace_exposure_untouched(self):
        data=xmp('', 'xmlns:z="other" z:Exposure2012="99" c:Exposure2012="0"')
        self.assertIn(b'z:Exposure2012="99"', parse(data).updated(.2))

    def test_reject_ambiguous_or_complex(self):
        for data in [xmp('<c:Exposure2012>1</c:Exposure2012>', 'c:Exposure2012="0"'), xmp('<c:Exposure2012><![CDATA[1]]></c:Exposure2012>'), xmp('<c:Exposure2012>1<!--x--></c:Exposure2012>'), xmp('', 'c:Exposure2012="NaN"'), xmp('', 'c:Exposure2012="&#49;"'), xmp('', 'c:Exposure2012="6"'), b'<!DOCTYPE a [<!ENTITY x "1">]>'+xmp('', 'c:Exposure2012="0"'), xmp('', 'c:Exposure2012="0"').decode().encode('utf-16')]:
            with self.subTest(data=data), self.assertRaises(ValueError): parse(data)

    def test_exif_child_and_sequence(self):
        data=xmp('<e:ExposureTime>1/30</e:ExposureTime><e:ISOSpeedRatings><r:Seq><r:li>100</r:li></r:Seq></e:ISOSpeedRatings>', 'xmlns:e="http://ns.adobe.com/exif/1.0/" c:Exposure2012="0"')
        self.assertEqual(parse(data).metadata['ExposureTime'], '1/30')
        self.assertEqual(parse(data).metadata['ISOSpeedRatings'], '100')


def report(y, cameras=None, exposures=None):
    n=len(y)
    measures=[{'tiles':[float(v)]*64,'valid':[True]*64} for v in y]
    return infer(measures, exposures or [0]*n, cameras or [0]*n, list(range(n)), 'manual')

class AlgorithmTests(unittest.TestCase):
    def test_linear_sunrise_untouched(self):
        y=np.linspace(-5,-1,90)
        r=report(y)
        c=correct(r,[0]*90,[], strength=1)
        self.assertLess(max(abs(v) for v in c['delta']),1e-10)
        self.assertEqual(r['events'],[])

    def test_step_plus_flicker_recovered(self):
        n=120; trend=np.linspace(-4,-2.5,n); cameras=np.r_[np.zeros(60),np.ones(60)*-1]
        y=trend+cameras+np.array([.025*(-1)**i for i in range(n)])
        r=report(y,cameras.tolist()); self.assertTrue(r['events'][0]['selectable'])
        c=correct(r,[0]*n,[60],strength=1)
        # Preserve the rendered endpoints, not the unbounded physical-light rise.
        target=trend+np.linspace(y[0]-trend[0], y[-1]-trend[-1], n)
        self.assertLess(float(np.sqrt(np.mean((np.array(c['target'])-target)**2))),.03)
        self.assertEqual(c['delta'][0],0)
        self.assertEqual(c['delta'][-1],0)

    def test_repeated_sunrise_reductions_do_not_accumulate(self):
        n=810; indices=[148,206,262,417,514,627,730]
        camera=np.zeros(n); rendered=np.linspace(-7.6,-4.0,n)
        # Seven camera reductions in an otherwise steadily brightening scene.
        for i in indices: camera[i:]-=1
        trend=rendered+np.linspace(0,10.71,n)
        y=trend+1.53*camera
        r=report(y,camera.tolist())
        c=correct(r,[0]*n,indices,strength=1)
        np.testing.assert_allclose(c['target'],rendered,atol=1e-10)
        self.assertEqual(c['delta'][0],0)
        self.assertEqual(c['delta'][-1],0)
        self.assertEqual(c['blocked'],[])
        self.assertLess(max(c['target']),0)

    def test_endpoint_offsets_are_relative_to_original_frames(self):
        y=np.r_[np.linspace(-5,-4,40),np.linspace(-5,-4,40)]
        c=correct(report(y,[0]*40+[-1]*40),[.3]*80,[40],start=.2,end=-.4)
        self.assertAlmostEqual(c['target'][0],y[0]+.2)
        self.assertAlmostEqual(c['target'][-1],y[-1]-.4)
        # The endpoint is specified in rendered brightness EV, not XMP EV.
        self.assertAlmostEqual(c['final'][-1], .3 - .4 / c['response']['gain'])

    def test_anchor_does_not_remove_unconfirmed_jump(self):
        y=np.linspace(-6,-3,120); camera=np.zeros(120)
        camera[30:]-=1; camera[80:]-=1; y+=camera
        c=correct(report(y,camera.tolist()),[0]*120,[30],strength=1)
        difference=np.diff(c['target'])
        self.assertAlmostEqual(difference[29],difference[28])
        self.assertAlmostEqual(difference[79]-difference[78],-1)

    def test_upward_and_mixed_events_keep_endpoints(self):
        y=np.linspace(-5,-4,120); camera=np.zeros(120)
        camera[30:]+=1; camera[80:]-=.5; y+=camera
        c=correct(report(y,camera.tolist()),[0]*120,[30,80],strength=1)
        np.testing.assert_allclose(c['target'],np.linspace(y[0],y[-1],120),atol=1e-10)

    def test_unconfirmed_step_not_smoothed(self):
        y=np.r_[np.linspace(-4,-3.5,60),np.linspace(-4.5,-4,60)]
        r=report(y, [0]*60+[-1]*60)
        c=correct(r,[0]*120,[],strength=1)
        self.assertLess(max(abs(v) for v in c['delta']),1e-10)

    def test_existing_compensation_not_duplicated(self):
        y=np.linspace(-4,-2,120); r=report(y,[0]*60+[-1]*60,[0]*60+[1]*60)
        self.assertTrue(r['events'][0]['selectable'])
        self.assertFalse(r['events'][0]['recommended'])
        c=correct(r,[0]*60+[1]*60,[],strength=1)
        self.assertLess(max(abs(v) for v in c['delta']),1e-10)

    def test_natural_change_not_classified_as_camera_event(self):
        y=np.r_[np.ones(30)*-4,np.ones(30)*-5]
        self.assertEqual(report(y)['events'],[])

    def test_dense_events_refused(self):
        y=np.linspace(-4,-2,20); cameras=[i%2 for i in range(20)]
        self.assertTrue(all(not e['selectable'] for e in report(y,cameras)['events']))

    def test_alternating_flicker_reduced_for_all_windows(self):
        trend=np.arange(160)*.01-4
        y=trend+.04*(-1.)**np.arange(160)
        for window in range(5,62,2):
            with self.subTest(window=window):
                out=smooth(y,window,[])
                self.assertLess(float(np.sqrt(np.mean((out-trend)**2))),.013)

    def test_invalid_limits(self):
        r=report(np.linspace(-4,-3,30))
        with self.assertRaises(ValueError): correct(r,[0]*30,[],strength=float('nan'))
        self.assertEqual(len(correct(r,[4.9]*30,[],start=1,end=1)['blocked']),30)

    def test_common_support_rejects_clipping(self):
        measures=[{'tiles':[-4]*64,'valid':[False]*60+[True]*4}]*10
        with self.assertRaises(ValueError): infer(measures,[0]*10,[0]*10,[None]*10,'manual')

class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name).resolve()
        self.paths=create(self.root/'demo',count=30)
    def tearDown(self): self.tmp.cleanup()
    def project(self): return Project(**self.paths,srgb=True)
    def export(self,p):
        result=p.calculate({'selected':[15]})
        return p.export(self.root/'out',result['revision'],p.id)

    def test_export_byte_preservation_and_originals(self):
        p=self.project(); before=[(digest(f.source.data),digest(f.raw.read_bytes())) for f in p.frames]
        out=self.export(p); folder=Path(out['path'])
        manifest=json.loads((folder/'manifest.json').read_text())
        self.assertTrue(manifest['synthetic'])
        for f, value in zip(p.frames,p.result['final']):
            self.assertEqual((folder/'originals'/f.xmp.name).read_bytes(),f.source.data)
            self.assertEqual((folder/'corrected'/f.xmp.name).read_bytes(),f.source.updated(value))
        self.assertEqual(before,[(digest(f.xmp.read_bytes()),digest(f.raw.read_bytes())) for f in p.frames])
        second=self.export(p); self.assertNotEqual(second['path'],out['path'])

    def test_input_changed_rejects(self):
        p=self.project(); p.frames[2].xmp.write_bytes(p.frames[2].source.updated(.2))
        with self.assertRaisesRegex(ValueError,'已改变'): self.export(p)

    def test_preview_changed_rejects(self):
        p=self.project(); p.frames[2].jpeg.write_bytes(p.frames[3].jpeg.read_bytes())
        with self.assertRaisesRegex(ValueError,'已改变'): self.export(p)

    def test_export_without_manual_attestations_keeps_snapshot_checks(self):
        p = Project(**self.paths)
        first = p.calculate({'selected': [15]})
        second = p.calculate({'selected': [15], 'strength': .3})
        with self.assertRaisesRegex(ValueError, '预览已失效'):
            p.export(self.root/'out', first['revision'])
        out = p.export(self.root/'out', second['revision'])
        self.assertTrue((Path(out['path'])/'manifest.json').is_file())
        p.frames[2].xmp.write_bytes(p.frames[2].source.updated(.2))
        with self.assertRaisesRegex(ValueError, '已改变'):
            p.export(self.root/'out', second['revision'])

    def test_order_confirmation_bound(self):
        p=self.project(); r=p.calculate({'selected':[]})
        with self.assertRaises(ValueError): p.export(self.root/'out',r['revision'],'wrong')

    def test_failed_write_leaves_no_package(self):
        p=self.project()
        with patch('timelapse.project.os.rename',side_effect=OSError('injected disk failure')):
            with self.assertRaises(OSError): self.export(p)
        self.assertEqual(list((self.root/'out').iterdir()),[])

    def test_pairing_missing_duplicate_symlink(self):
        preview=Path(self.paths['preview_dir']); source=preview/'DSC_0001.jpg'
        copy=preview/'DSC_0001.JPEG'; copy.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(ValueError,'冲突'): self.project()
        copy.unlink(); source.unlink()
        with self.assertRaisesRegex(ValueError,'不匹配'): self.project()
        source.symlink_to(preview/'DSC_0002.jpg')
        with self.assertRaisesRegex(ValueError,'符号链接'): self.project()

    def test_non_srgb_rejected(self):
        from PIL import Image
        from io import BytesIO
        im=Image.new('RGB',(64,64),(60,60,60)); exif=Image.Exif(); exif[40961]=65535
        stream=BytesIO(); im.save(stream,format='JPEG',exif=exif)
        with self.assertRaisesRegex(ValueError,'sRGB'): measure(stream.getvalue(),[0,0,1,1])

    def test_verify_same_inputs_reports_unchanged(self):
        p=self.project(); out=self.export(p)
        result=p.verify(self.paths['preview_dir'],out['package_id'])
        self.assertEqual(result['unchanged_previews'],30)
        self.assertGreater(result['rmse_ev'],.2)
        self.assertGreater(abs(result['events'][0]['remaining_jump']),.9)

    def test_verify_wrong_dimensions_rejected(self):
        from PIL import Image
        p=self.project(); out=self.export(p)
        folder=self.root/'verify'; folder.mkdir()
        for f in p.frames:
            with Image.open(f.jpeg) as im: im.resize((96,64)).save(folder/f.jpeg.name)
        with self.assertRaisesRegex(ValueError,'尺寸'): p.verify(folder,out['package_id'])

    def test_xmp_crop_mismatch_rejects(self):
        path=Path(self.paths['raw_dir'])/'DSC_0002.xmp'; path.write_bytes(path.read_bytes().replace(b'crs:Tint="+8"',b'crs:Tint="+8" crs:CropLeft="0.1"'))
        with self.assertRaisesRegex(ValueError,'裁切'): self.project()

    def test_fifo_rejected_without_blocking(self):
        import os
        fifo=self.root/'pipe'; os.mkfifo(fifo)
        with self.assertRaises(ValueError): safe_read(fifo,100)

    def test_block_export_inside_source(self):
        p=self.project(); r=p.calculate({'selected':[]})
        with self.assertRaisesRegex(ValueError,'素材目录'): p.export(p.raw_dir/'out',r['revision'],p.id)
        self.assertFalse((p.raw_dir/'out').exists())

if __name__=='__main__': unittest.main()
