"""Three top-level ACR controls; preserve all other sidecar bytes."""
import math
from xml.parsers import expat
import xml.etree.ElementTree as ET

from .xmp import CRS, RDF, ATTR, NUMBER, parse, tag_end
from .provenance import _jpeg_packets, jpeg_evidence

FIELDS = ('Exposure2012', 'Highlights2012', 'Whites2012')
LIMITS = ((-5., 5.), (-100., 100.), (-100., 100.))


def scalar_spans(data, fields=FIELDS):
    parse(data)  # UTF-8, XML/entity restrictions and unique top-level description.
    parser = expat.ParserCreate(namespace_separator='|')
    stack, mappings, pending, found = [], [], {}, {k: [] for k in fields}
    child = None

    def ns(prefix, uri):
        pending[prefix or ''] = uri

    def description(path):
        return path in ([RDF + '|RDF', RDF + '|Description'],
                        ['adobe:ns:meta/|xmpmeta', RDF + '|RDF', RDF + '|Description'])

    def start(name, attrs):
        nonlocal child
        mapping = dict(mappings[-1]) if mappings else {}
        mapping.update(pending); pending.clear(); mappings.append(mapping)
        if child is not None:
            raise ValueError('联合反馈参数必须为简单文本')
        if description(stack + [name]):
            a = parser.CurrentByteIndex
            b = tag_end(data, a)
            for match in ATTR.finditer(data[a:b]):
                prefix, sep, local = match[1].decode().partition(':')
                if sep and mapping.get(prefix) == CRS and local in fields:
                    found[local].append((a + match.start(3), a + match.end(3)))
        if description(stack) and name.startswith(CRS + '|') and name.split('|')[-1] in fields:
            if attrs:
                raise ValueError('联合反馈参数不能包含属性')
            child = (name.split('|')[-1], tag_end(data, parser.CurrentByteIndex))
        stack.append(name)

    def end(name):
        nonlocal child
        if child is not None:
            found[child[0]].append((child[1], parser.CurrentByteIndex)); child = None
        stack.pop(); mappings.pop()

    parser.StartNamespaceDeclHandler = ns
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(data, True)
    if any(len(v) != 1 for v in found.values()):
        raise ValueError('需要唯一、显式的顶层参数：' + ', '.join(fields))
    return {k: v[0] for k, v in found.items()}


def values(data):
    spans = scalar_spans(data)
    result = []
    for key, (lo, hi) in zip(FIELDS, LIMITS):
        a, b = spans[key]
        if not NUMBER.fullmatch(data[a:b]):
            raise ValueError(key + ' 必须为简单有限数字')
        value = float(data[a:b])
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(key + ' 超出范围')
        if key != FIELDS[0] and value != round(value):
            raise ValueError(key + ' 必须为整数')
        result.append(value)
    return result


def update(data, controls):
    if len(controls) != 3:
        raise ValueError('需要曝光、高光、白色色阶三个值')
    spans = scalar_spans(data)
    replacements = []
    for i, (key, value, (lo, hi)) in enumerate(zip(FIELDS, controls, LIMITS)):
        if not math.isfinite(value) or not lo <= value <= hi or (i and value != round(value)):
            raise ValueError(key + ' 超出范围或不是整数')
        replacements.append((*spans[key], (f'{value:+.6f}' if not i else str(int(value))).encode()))
    result = data
    for a, b, value in sorted(replacements, reverse=True):
        result = result[:a] + value + result[b:]
    if any(abs(a-b) > 1e-6 for a, b in zip(values(result), controls)):
        raise ValueError('联合参数写入复核失败')
    return result


def recipe_from_jpeg(data):
    """Explicit reconstruction, not a claim that the on-disk sidecar matches."""
    evidence = jpeg_evidence(data)
    if evidence['missing'] or 'ProcessVersion' not in evidence['crs']:
        raise ValueError('联合反馈需要 JPEG 内嵌的完整显影设置')
    packets, extended = _jpeg_packets(data)
    if extended or len(packets) != 1:
        raise ValueError('不支持缺失或扩展 XMP')
    result = packets[0]
    # Camera capture identity often lives only in the JPEG EXIF segment. Carry
    # it into this newly reconstructed sidecar so later probes can be paired.
    from .provenance import xmp_evidence
    existing = xmp_evidence(result)['identity']
    missing = {k: v for k, v in evidence['identity'].items() if k not in existing and k != 'RawFileName'}
    if missing:
        root = ET.fromstring(result)
        description = root.find(f'./{{{RDF}}}Description') if root.tag == f'{{{RDF}}}RDF' else root.find(f'./{{{RDF}}}RDF/{{{RDF}}}Description')
        for key, value in missing.items():
            description.set('{http://ns.adobe.com/exif/1.0/}'+key, str(value))
        result = ET.tostring(root, encoding='utf-8')
    values(result)
    # A JPEG says its recipe has already been rendered. A RAW sidecar must not.
    if 'AlreadyApplied' in evidence['ignored_fields']:
        a, b = scalar_spans(result, ('AlreadyApplied',))['AlreadyApplied']
        result = result[:a] + b'False' + result[b:]
    return result, evidence
