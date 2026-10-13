"""810-frame synthetic pipeline. The verification render is NOT Adobe ACR."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from PIL import Image, ImageCms
from timelapse.demo import create
from timelapse.project import Project


def simulate_linear_render(project, destination):
    destination=Path(destination); destination.mkdir()
    icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    for f,delta in zip(project.frames,project.result['delta']):
        with Image.open(f.jpeg) as im:
            srgb=np.asarray(im,dtype=float)/255
        linear=np.where(srgb<=.04045,srgb/12.92,((srgb+.055)/1.055)**2.4)
        linear*=2**delta
        srgb=np.where(linear<=.0031308,linear*12.92,1.055*linear**(1/2.4)-.055)
        Image.fromarray(np.uint8(np.clip(np.round(srgb*255),0,255))).save(destination/f.jpeg.name,quality=98,icc_profile=icc)
    return destination

class SequenceTests(unittest.TestCase):
    def test_810_frames_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); paths=create(root/'synthetic',810)
            p=Project(**paths,srgb=True)
            self.assertEqual([e['index'] for e in p.report['events'] if e['selectable']],[405])
            r=p.calculate({'selected':[405], 'strength':1.0})
            self.assertEqual(r['blocked'],[])
            out=p.export(root/'output',r['revision'],p.id)
            verified=simulate_linear_render(p,root/'simulated-render')
            check=p.verify(verified,out['package_id'])
            self.assertLess(check['rmse_ev'],.025)
            self.assertLess(abs(check['trend_error_ev']),.025)
            self.assertLess(abs(check['events'][0]['remaining_jump']),.03)
            self.assertEqual(check['unchanged_previews'],0)
            print('\n810-frame synthetic result: '+json.dumps({k:v for k,v in check.items() if k not in ('actual','note')},ensure_ascii=False))

if __name__=='__main__': unittest.main()
