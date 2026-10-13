"""Synthetic demonstration. These files are NOT camera RAWs."""
from pathlib import Path
import json
import numpy as np
from PIL import Image, ImageCms


def create(root, count=120):
    root = Path(root)
    from .workspace import output_path
    root = output_path(root)
    root.mkdir(parents=True, exist_ok=False)
    raw, preview = root / 'raw', root / 'previews'
    raw.mkdir()
    preview.mkdir()
    (raw / 'SYNTHETIC.txt').write_text('测试夹具：.ARW 文件仅是占位符，不是真实 RAW，不能送入 ACR。\n')
    yy, xx = np.mgrid[0:128, 0:192]
    texture = .85 + .2 * xx / 192 + .05 * np.sin(xx / 12) * np.cos(yy / 10)
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    split = count // 2
    truth = []
    for i in range(count):
        trend = -4.2 + i / (count - 1) * 1.4
        camera = 0 if i < split else -1
        flicker = .04 * (-1) ** i
        y = 2 ** (trend + camera + flicker) * texture
        linear = np.stack([y * 1.05, y, y * .92], axis=-1)
        srgb = np.where(linear <= .0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - .055)
        stem = f'DSC_{i + 1:04d}'
        (raw / (stem + '.ARW')).write_bytes(b'SYNTHETIC RAW PLACEHOLDER\n')
        second = i * 5
        stamp = f'2026:10:{11 + second // 86400:02d} {second // 3600 % 24:02d}:{second // 60 % 60:02d}:{second % 60:02d}'
        (raw / (stem + '.xmp')).write_text(f'''<?xpacket begin="﻿"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" xmlns:exif="http://ns.adobe.com/exif/1.0/" crs:Exposure2012="+0.000000" crs:ProcessVersion="11.0" crs:Temperature="5200" crs:Tint="+8" exif:ExposureTime="1/{30 if i < split else 60}" exif:ISOSpeedRatings="100" exif:FNumber="8" exif:DateTimeOriginal="{stamp}" /></rdf:RDF></x:xmpmeta><?xpacket end="w"?>''', encoding='utf-8')
        Image.fromarray(np.uint8(np.clip(srgb * 255, 0, 255))).save(preview / (stem + '.jpg'), quality=96, icc_profile=icc)
        truth.append({'trend': trend, 'camera': camera, 'flicker': flicker})
    (root / 'truth.json').write_text(json.dumps(truth))
    return {'raw_dir': str(raw.absolute()), 'preview_dir': str(preview.absolute())}
