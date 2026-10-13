import base64
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from timelapse.preview import region_preview


class RegionPreviewTests(unittest.TestCase):
    def test_before_analysis_and_exif_orientation(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d).resolve()
            image = Image.new('RGB', (80, 40), 'white')  # Full-frame analysis has no valid tiles.
            exif = Image.Exif(); exif[274] = 6
            image.save(folder / 'photo10.jpg', exif=exif)
            image.save(folder / 'photo2.jpg', exif=exif)
            result = region_preview(folder)
            self.assertEqual(result['name'], 'photo2.jpg')
            self.assertEqual((result['width'], result['height'], result['count']), (40, 80, 2))
            with Image.open(BytesIO(base64.b64decode(result['image'].split(',')[1]))) as preview:
                self.assertEqual(preview.size, (40, 80))
                self.assertNotIn(274, preview.getexif())
            self.assertEqual(region_preview(folder, 1)['name'], 'photo10.jpg')
            with self.assertRaises(ValueError): region_preview(folder, -1)
            with self.assertRaises(ValueError): region_preview(folder, 2)
            with self.assertRaises(ValueError): region_preview(folder, True)

    def test_reject_wrong_format_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d).resolve()
            path = folder / 'image.jpg'
            Image.new('RGB', (64, 64)).save(path, format='PNG')
            with self.assertRaisesRegex(ValueError, 'JPEG'): region_preview(folder)
            path.unlink(); path.symlink_to(folder / 'elsewhere.jpg')
            with self.assertRaisesRegex(ValueError, '符号链接'): region_preview(folder)

if __name__ == '__main__': unittest.main()
