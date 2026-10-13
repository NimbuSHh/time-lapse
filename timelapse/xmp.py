"""Namespace-aware, byte-preserving update of one current ACR exposure value."""
from dataclasses import dataclass
import math
import re
from xml.parsers import expat
import xml.etree.ElementTree as ET

CRS = 'http://ns.adobe.com/camera-raw-settings/1.0/'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
EXIF = 'http://ns.adobe.com/exif/1.0/'
NUMBER = re.compile(rb'\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*\Z')
ATTR = re.compile(rb'([^\s=<>/]+)\s*=\s*([\x22\x27])(.*?)\2', re.S)

@dataclass
class XMP:
    data: bytes
    exposure: float
    span: tuple[int, int]
    metadata: dict

    def updated(self, exposure: float) -> bytes:
        if not math.isfinite(exposure) or not -5 <= exposure <= 5:
            raise ValueError('最终曝光必须位于 -5 至 +5 EV')
        a, b = self.span
        result = self.data[:a] + f'{exposure:+.6f}'.encode() + self.data[b:]
        parsed = parse(result)
        if abs(parsed.exposure - exposure) > 1e-6:
            raise ValueError('XMP 写入复核失败')
        return result


def tag_end(data, start):
    quote = None
    for i in range(start, len(data)):
        c = data[i]
        if quote:
            if c == quote:
                quote = None
        elif c in (34, 39):
            quote = c
        elif c == 62:
            return i + 1
    raise ValueError('XMP 标签不完整')


def parse(data: bytes) -> XMP:
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('XMP 超过 4 MB')
    try:
        data.decode('utf-8-sig')
    except UnicodeError as e:
        raise ValueError('首版仅支持 UTF-8 XMP') from e
    if b'\x00' in data or re.search(rb'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('不支持 DTD、实体声明或非 UTF-8 XMP')
    declaration = re.search(rb'<\?xml[^>]*encoding\s*=\s*[\x22\x27]([^\x22\x27]+)', data, re.I)
    if declaration and declaration[1].lower() not in (b'utf-8', b'utf8'):
        raise ValueError('首版仅支持 UTF-8 XMP')
    parser = expat.ParserCreate(namespace_separator='|')
    stack, ns_stack, pending_ns, spans = [], [], {}, []
    metadata = {}
    child = None
    descriptions = 0

    def ns(prefix, uri):
        pending_ns[prefix or ''] = uri

    def start(name, attrs):
        nonlocal child, descriptions
        mapping = dict(ns_stack[-1]) if ns_stack else {}
        mapping.update(pending_ns)
        pending_ns.clear()
        ns_stack.append(mapping)
        is_description = name == RDF + '|Description' and stack == ['adobe:ns:meta/|xmpmeta', RDF + '|RDF']
        # Some valid sidecars have rdf:RDF as the document root.
        is_description |= name == RDF + '|Description' and stack == [RDF + '|RDF']
        if is_description:
            descriptions += 1
            for key, value in attrs.items():
                if key.startswith(EXIF + '|') or key.startswith(CRS + '|'):
                    metadata[key.split('|')[-1]] = value
            a = parser.CurrentByteIndex
            b = tag_end(data, a)
            for match in ATTR.finditer(data[a:b]):
                literal = match[1].decode('utf-8')
                prefix, sep, local = literal.partition(':')
                if sep and mapping.get(prefix) == CRS and local == 'Exposure2012':
                    spans.append((a + match.start(3), a + match.end(3)))
        parent_description = stack and stack[-1] == RDF + '|Description' and (stack[:-1] == [RDF + '|RDF'] or stack[:-1] == ['adobe:ns:meta/|xmpmeta', RDF + '|RDF'])
        if parent_description and name == CRS + '|Exposure2012':
            if attrs:
                raise ValueError('曝光子节点不能包含属性')
            child = (len(stack), tag_end(data, parser.CurrentByteIndex))
        elif child is not None:
            raise ValueError('曝光子节点必须为纯数字文本')
        stack.append(name)

    def end(name):
        nonlocal child
        if child and len(stack) == child[0] + 1 and name == CRS + '|Exposure2012':
            spans.append((child[1], parser.CurrentByteIndex))
            child = None
        stack.pop()
        ns_stack.pop()

    parser.StartNamespaceDeclHandler = ns
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, UnicodeError) as e:
        raise ValueError(f'XMP XML 无效: {e}') from e
    if descriptions != 1 or len(spans) != 1:
        raise ValueError('需要唯一的顶层 rdf:Description 和 Exposure2012；请先由 ACR 保存现代处理版本的独立 XMP')
    a, b = spans[0]
    if not NUMBER.fullmatch(data[a:b]):
        raise ValueError('Exposure2012 必须为简单有限数字，不支持实体、CDATA 或注释')
    value = float(data[a:b])
    if not math.isfinite(value) or not -5 <= value <= 5:
        raise ValueError('现有 Exposure2012 超出支持范围')
    # EXIF values are often child properties; ISO is commonly an RDF sequence.
    root = ET.fromstring(data)
    descriptions_nodes = root.findall(f'./{{{RDF}}}RDF/{{{RDF}}}Description') if root.tag != f'{{{RDF}}}RDF' else root.findall(f'./{{{RDF}}}Description')
    for node in descriptions_nodes[0]:
        if node.tag.startswith('{' + EXIF + '}') or node.tag.startswith('{' + CRS + '}'):
            key = node.tag.split('}')[-1]
            if key in ('ExposureTime', 'FNumber', 'DateTimeOriginal', 'ProcessVersion') or key.startswith(('Crop', 'Perspective')):
                if len(node) == 0 and node.text:
                    metadata[key] = node.text.strip()
            elif key == 'ISOSpeedRatings':
                items = node.findall(f'./{{{RDF}}}Seq/{{{RDF}}}li')
                if len(items) == 1:
                    metadata[key] = items[0].text
                elif len(node) == 0 and node.text:
                    metadata[key] = node.text.strip()
    return XMP(data, value, (a, b), metadata)
