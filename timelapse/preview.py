"""Read-only, orientation-normalized previews available before ROI analysis."""
import base64
from io import BytesIO
import warnings
from PIL import Image, ImageOps
from .project import directory, scan, natural, safe_read


def region_preview(folder, index=0):
    files = scan(directory(folder), {'.jpg', '.jpeg'})
    ordered = sorted(files, key=natural)
    if not ordered:
        raise ValueError('小图目录中没有 JPEG 图片')
    if type(index) is not int or not 0 <= index < len(ordered):
        raise ValueError('预览帧序号无效')
    path = files[ordered[index]]
    data = safe_read(path, 25 * 1024 * 1024)
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as source:
            if source.format != 'JPEG' or source.width * source.height > 24_000_000 or max(source.size) > 12000:
                raise ValueError('预览必须是最多 2400 万像素的 JPEG')
            image = ImageOps.exif_transpose(source).convert('RGB')
            width, height = image.size
            image.thumbnail((1400, 1400))
            stream = BytesIO()
            image.save(stream, format='JPEG', quality=90, icc_profile=source.info.get('icc_profile'))
    return {'name': path.name, 'index': index, 'count': len(ordered), 'width': width, 'height': height,
            'image': 'data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')}
