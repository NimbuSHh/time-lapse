"""Read-only metadata evidence, not authentication of image contents or an ACR run.

No paths are accepted or followed. A matching embedded recipe is evidence of
recorded settings only; absent settings or identity cannot establish provenance.
"""
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
from fractions import Fraction
import hashlib
from io import BytesIO
import json
import math
import re
import xml.etree.ElementTree as ET

from PIL import Image, JpegImagePlugin

from .xmp import CRS, RDF, parse

MAX_XMP = 4 * 1024 * 1024
STANDARD_XMP = b'http://ns.adobe.com/xap/1.0/\x00'
EXTENDED_XMP = b'http://ns.adobe.com/xmp/extension/\x00'
# Only top-level workflow/software fields, never similarly named Look fields.
IGNORED_CRS = {'Version', 'AlreadyApplied', 'HasSettings'}
IDENTITY_NAMES = {
    'ImageUniqueID': 'ImageUniqueID', 'BodySerialNumber': 'BodySerialNumber',
    'SerialNumber': 'BodySerialNumber', 'DateTimeOriginal': 'DateTimeOriginal',
    'ExposureTime': 'ExposureTime', 'FNumber': 'FNumber',
    'ISOSpeedRatings': 'ISOSpeedRatings', 'PhotographicSensitivity': 'ISOSpeedRatings',
    'Make': 'Make', 'Model': 'Model', 'RawFileName': 'RawFileName',
}
IDENTITY_NS = {
    'http://ns.adobe.com/exif/1.0/', 'http://ns.adobe.com/exif/1.0/aux/',
    'http://cipa.jp/exif/1.0/', 'http://ns.adobe.com/tiff/1.0/',
}
NUMERIC_IDENTITY = {'ExposureTime', 'FNumber', 'ISOSpeedRatings'}
NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _scalar(value):
    value = str(value).strip()
    if value.lower() in ('true', 'false'):
        return value.lower()
    if NUMBER.fullmatch(value):
        try:
            with localcontext() as ctx:
                ctx.prec = max(32, len(value))
                number = Decimal(value).normalize()
            return '0' if number == 0 else str(number)
        except (DecimalException, ValueError):
            pass
    return value


def _canonical(node, depth=0):
    if depth > 64:
        raise ValueError('XMP 嵌套过深')
    # RDF Description properties may be attributes or simple child elements.
    if node.tag == '{' + RDF + '}Description':
        props = [(key, _scalar(value)) for key, value in node.attrib.items()]
        for child in node:
            value = _scalar(child.text or '') if not child.attrib and not len(child) else _canonical(child, depth + 1)
            props.append((child.tag, value))
        return {'tag': node.tag, 'properties': sorted(props, key=lambda p: (p[0], json.dumps(p[1], sort_keys=True)))}
    children = [_canonical(child, depth + 1) for child in node]
    if node.tag in ('{' + RDF + '}Bag', '{' + RDF + '}Alt'):
        children.sort(key=lambda child: json.dumps(child, sort_keys=True))
    return {'tag': node.tag, 'attributes': sorted((k, _scalar(v)) for k, v in node.attrib.items()),
            'text': _scalar(node.text or ''), 'children': children}


def _xml(data):
    if not isinstance(data, bytes) or len(data) > MAX_XMP:
        raise ValueError('XMP 必须为最多 4 MB 的字节内容')
    try:
        data.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('仅支持 UTF-8 XMP') from exc
    if b'\x00' in data or re.search(rb'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('XMP 不允许 DTD、实体声明或非 UTF-8 编码')
    encoding = re.search(rb'<\?xml[^>]*encoding\s*=\s*[\x22\x27]([^\x22\x27]+)', data, re.I)
    if encoding and encoding[1].lower() not in (b'utf-8', b'utf8'):
        raise ValueError('仅支持 UTF-8 XMP')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError('XMP XML 无效') from exc
    if sum(1 for _ in root.iter()) > 50000:
        raise ValueError('XMP 节点过多')
    return root


def _identity_value(key, value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace').rstrip('\x00')
    if key in NUMERIC_IDENTITY:
        try:
            number = float(Fraction(str(value).strip()))
            if math.isfinite(number) and number > 0:
                return number
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            return None
        return None
    value = str(value).strip().rstrip('\x00')
    if key == 'RawFileName' and (value in ('.', '..') or any(c in value for c in '/\\\x00') or any(ord(c) < 32 for c in value)):
        raise ValueError('RawFileName 必须是单一文件名，不能含路径')
    if key == 'DateTimeOriginal':
        value = re.sub(r'^(\d{4}):(\d{2}):(\d{2}) ', r'\1-\2-\3T', value)
    return value or None


def _identity_equal(key, left, right):
    if key in NUMERIC_IDENTITY:
        return math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-9)
    if key != 'DateTimeOriginal' or left == right:
        return left == right
    try:
        a, b = datetime.fromisoformat(left), datetime.fromisoformat(right)
    except ValueError:
        return False
    # JPEG EXIF often drops both subsecond precision and zone. Compare only
    # recorded common precision; retain originals and report the limitation.
    if a.tzinfo is None or b.tzinfo is None:
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    if '.' not in left or '.' not in right:
        a, b = a.replace(microsecond=0), b.replace(microsecond=0)
    return a == b


def _record_identity(identity, key, value):
    value = _identity_value(key, value)
    if value is None:
        return
    if key in identity and not _identity_equal(key, identity[key], value):
        raise ValueError(f'同一文件含冲突身份字段: {key}')
    identity.setdefault(key, value)


def xmp_evidence(data):
    """Extract top-level exposure and a namespace-aware CRS semantic signature.

    Nested Look/mask/sequence content is retained, including nested Exposure2012.
    Only the three documented top-level workflow fields are excluded and logged.
    """
    root = _xml(data)
    descriptions = (root.findall('./{' + RDF + '}Description') if root.tag == '{' + RDF + '}RDF'
                    else root.findall('./{' + RDF + '}RDF/{' + RDF + '}Description'))
    if not descriptions:
        raise ValueError('XMP 缺少顶层 RDF Description')
    crs, ignored, identity = {}, {}, {}
    exposure = None
    for description in descriptions:
        props = list(description.attrib.items())
        for node in description:
            value = _scalar(node.text or '') if not node.attrib and not len(node) else _canonical(node)
            props.append((node.tag, value))
            if node.tag.startswith('{'):
                ns, local = node.tag[1:].split('}', 1)
                if ns in IDENTITY_NS and local in IDENTITY_NAMES:
                    leaves = [n.text.strip() for n in node.iter() if not len(n) and n.text and n.text.strip()]
                    if len(leaves) == 1:
                        _record_identity(identity, IDENTITY_NAMES[local], leaves[0])
        for name, value in props:
            if not name.startswith('{'):
                continue
            ns, local = name[1:].split('}', 1)
            if ns in IDENTITY_NS and local in IDENTITY_NAMES and isinstance(value, str):
                _record_identity(identity, IDENTITY_NAMES[local], value)
            if ns != CRS:
                continue
            if local == 'RawFileName':
                _record_identity(identity, 'RawFileName', value)
            if local == 'Exposure2012':
                if exposure is not None or not isinstance(value, str):
                    raise ValueError('顶层 Exposure2012 重复或不是纯数值')
                try:
                    exposure = float(value)
                except ValueError as exc:
                    raise ValueError('顶层 Exposure2012 不是数值') from exc
                if not math.isfinite(exposure) or not -5 <= exposure <= 5:
                    raise ValueError('顶层 Exposure2012 超出有效范围')
            else:
                bucket = ignored if local in IGNORED_CRS else crs
                if local in bucket:
                    raise ValueError(f'重复 CRS 字段: {local}')
                bucket[local] = _scalar(value) if isinstance(value, str) else value
    signature = _sha(json.dumps(crs, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
    return {'sha256': _sha(data), 'exposure': exposure, 'crs': crs, 'crs_sha256': signature,
            'identity': identity, 'ignored_fields': ignored}


def _jpeg_packets(data, max_bytes=25 * 1024 * 1024):
    if not isinstance(data, bytes) or len(data) > max_bytes or not data.startswith(b'\xff\xd8'):
        raise ValueError(f'需要最多 {max_bytes // (1024*1024)} MB 的 JPEG 字节内容')
    position, packets, extended = 2, [], False
    while position < len(data):
        if data[position] != 0xff:
            raise ValueError('JPEG 标记无效')
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position >= len(data):
            raise ValueError('JPEG 标记被截断')
        marker = data[position]
        position += 1
        if marker in (0xda, 0xd9):
            break
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            continue
        if position + 2 > len(data):
            raise ValueError('JPEG 段长度被截断')
        length = int.from_bytes(data[position:position + 2], 'big')
        if length < 2 or position + length > len(data):
            raise ValueError('JPEG 段长度无效')
        payload = data[position + 2:position + length]
        if marker == 0xe1:
            if payload.startswith(STANDARD_XMP):
                packets.append(payload[len(STANDARD_XMP):])
            elif payload.startswith(EXTENDED_XMP):
                extended = True
        position += length
    if len(packets) > 1:
        raise ValueError('JPEG 包含多个标准 XMP，来源不明确')
    return packets, extended


def jpeg_evidence(data, *, full_size=False):
    """Read standard APP1 XMP and EXIF without loading image pixels or paths."""
    packets, extended = _jpeg_packets(data, 128*1024*1024 if full_size else 25*1024*1024)
    result = xmp_evidence(packets[0]) if packets else {
        'exposure': None, 'crs': {}, 'crs_sha256': None, 'identity': {}, 'ignored_fields': {}}
    result['sha256'] = _sha(data)
    result['missing'], result['warnings'] = [], []
    if not packets:
        result['missing'].append('embedded_xmp')
    if not result['crs']:
        result['missing'].append('embedded_crs')
    if result['exposure'] is None:
        result['missing'].append('embedded_exposure')
    if extended:
        result['missing'].append('extended_xmp_not_supported')
    tags = {271: 'Make', 272: 'Model', 33434: 'ExposureTime', 33437: 'FNumber', 34855: 'ISOSpeedRatings',
            36867: 'DateTimeOriginal', 42016: 'ImageUniqueID', 42033: 'BodySerialNumber'}
    # Full-size Adobe output is inspected for metadata only, never decoded.
    # Keep the stricter preview byte/pixel limits for all existing callers.
    opener = JpegImagePlugin.JpegImageFile if full_size else Image.open
    with opener(BytesIO(data)) as source:
        if source.format != 'JPEG':
            raise ValueError('输入不是 JPEG')
        exif = dict(source.getexif())
        try:
            exif.update(source.getexif().get_ifd(34665))
        except (KeyError, TypeError, ValueError):
            result['warnings'].append('exif_subifd_unreadable')
        for tag, key in tags.items():
            if tag in exif:
                _record_identity(result['identity'], key, exif[tag])
    return result


def audit_frame(original_xmp, corrected_xmp, current_xmp=None, original_jpeg=None, corrected_jpeg=None,
                expected_sha256=None, expected_identity=None, exposure_tolerance=1e-5,
                jpeg_exposure_tolerance=0.0051):
    """Compare supplied evidence; 'verified' means these metadata checks passed.

    Both JPEG recipes and identity comparisons are required for full evidence.
    Missing metadata never proves that settings differed or that a render ran.
    """
    for tolerance in (exposure_tolerance, jpeg_exposure_tolerance):
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError('曝光比较容差无效')
    verified, mismatches, missing, evidence = [], [], [], {}
    inputs = {'original_xmp': original_xmp, 'corrected_xmp': corrected_xmp, 'current_xmp': current_xmp,
              'original_jpeg': original_jpeg, 'corrected_jpeg': corrected_jpeg}
    for key, data in inputs.items():
        if data is None:
            missing.append({'check': key, 'field': 'file'})
            continue
        try:
            evidence[key] = (jpeg_evidence if key.endswith('jpeg') else xmp_evidence)(data)
        except (ValueError, OSError) as exc:
            mismatches.append({'check': key, 'field': 'metadata', 'reason': str(exc)})
    for key, expected in (expected_sha256 or {}).items():
        if key not in inputs:
            raise ValueError('未知 SHA 检查对象: ' + str(key))
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-fA-F]{64}', expected):
            raise ValueError('SHA256 格式无效')
        if inputs[key] is None:
            missing.append({'check': key, 'field': 'sha256', 'expected': expected.lower()})
        else:
            actual = _sha(inputs[key])
            item = {'check': key, 'field': 'sha256', 'expected': expected.lower(), 'actual': actual}
            (verified if actual == expected.lower() else mismatches).append(item)
    # Our generated corrected file must change only the exposure numeric span.
    try:
        source, corrected = parse(original_xmp), parse(corrected_xmp)
        original_rest = source.data[:source.span[0]] + source.data[source.span[1]:]
        corrected_rest = corrected.data[:corrected.span[0]] + corrected.data[corrected.span[1]:]
        item = {'check': 'exposure_only_change', 'field': 'original_xmp->corrected_xmp'}
        (verified if original_rest == corrected_rest else mismatches).append(item)
    except (ValueError, TypeError) as exc:
        mismatches.append({'check': 'exposure_only_change', 'reason': str(exc)})

    def compare(expected_key, actual_key, jpeg=False, compare_exposure=True):
        if expected_key not in evidence or actual_key not in evidence:
            return
        expected, actual = evidence[expected_key], evidence[actual_key]
        check = expected_key + '->' + actual_key
        for key in actual.get('missing', []):
            missing.append({'check': check, 'field': key})
        for field, value in expected['crs'].items():
            if field not in actual['crs']:
                missing.append({'check': check, 'field': 'crs.' + field})
            else:
                item = {'check': check, 'field': 'crs.' + field, 'expected': value, 'actual': actual['crs'][field]}
                (verified if value == actual['crs'][field] else mismatches).append(item)
        for field in actual['crs'].keys() - expected['crs'].keys():
            # An added rendering field has unknown baseline meaning, not proof of equality.
            pair = evidence.get('original_jpeg', {}), evidence.get('corrected_jpeg', {})
            paired = jpeg and all(field in e.get('crs', {}) for e in pair) and pair[0]['crs'][field] == pair[1]['crs'][field]
            item = {'check': check, 'field': 'baseline.crs.' + field, 'actual': actual['crs'][field]}
            if paired:
                item['basis'] = 'unchanged_in_original_and_corrected_embedded_recipe'
            (verified if paired else missing).append(item)
        if not expected['crs']:
            missing.append({'check': check, 'field': 'baseline.crs'})
        if not compare_exposure:
            pass
        elif actual['exposure'] is None or expected['exposure'] is None:
            missing.append({'check': check, 'field': 'exposure'})
        else:
            tolerance = jpeg_exposure_tolerance if jpeg else exposure_tolerance
            difference = actual['exposure'] - expected['exposure']
            item = {'check': check, 'field': 'exposure', 'expected': expected['exposure'], 'actual': actual['exposure'],
                    'difference': difference, 'tolerance': tolerance, 'rounded': jpeg and abs(difference) > exposure_tolerance}
            (verified if abs(difference) <= tolerance + 1e-12 else mismatches).append(item)
        identity = dict(expected['identity'])
        for field, value in (expected_identity or {}).items():
            if field not in set(IDENTITY_NAMES.values()):
                raise ValueError('未知身份字段: ' + str(field))
            normalized = _identity_value(field, value)
            if normalized is None:
                raise ValueError('身份字段无效: ' + str(field))
            if field in identity and not _identity_equal(field, identity[field], normalized):
                mismatches.append({'check': check, 'field': 'expected_identity.' + field,
                                   'expected': normalized, 'actual': identity[field]})
            identity[field] = normalized
        if not identity:
            missing.append({'check': check, 'field': 'baseline.identity'})
        for field, value in identity.items():
            if field not in actual['identity']:
                missing.append({'check': check, 'field': 'identity.' + field})
                continue
            found = actual['identity'][field]
            equal = _identity_equal(field, value, found)
            item = {'check': check, 'field': 'identity.' + field, 'expected': value, 'actual': found}
            if field == 'DateTimeOriginal' and equal and value != found:
                item['precision'] = 'common_recorded_precision_only'
            (verified if equal else mismatches).append(item)
        if not any(field in identity and field in actual['identity'] for field in ('ImageUniqueID', 'DateTimeOriginal', 'RawFileName')):
            missing.append({'check': check, 'field': 'frame_identity', 'reason': '仅设备或曝光参数不能区分同一序列的帧'})

    compare('original_xmp', 'corrected_xmp', compare_exposure=False)
    compare('corrected_xmp', 'current_xmp')
    compare('original_xmp', 'original_jpeg', jpeg=True)
    compare('corrected_xmp', 'corrected_jpeg', jpeg=True)
    compare('original_jpeg', 'corrected_jpeg', jpeg=True, compare_exposure=False)
    return {'status': 'mismatch' if mismatches else ('incomplete' if missing else 'verified'),
            'verified': verified, 'mismatches': mismatches, 'missing': missing, 'evidence': evidence,
            'scope': 'recorded_metadata_only',
            'note': '比对记录的设置、身份及提供的哈希；不能单独证明 ACR 实际使用了这些设置或证明图像内容真实性。'}
