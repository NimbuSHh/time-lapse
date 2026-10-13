"""Synthetic metadata receipts: no user files and no Adobe render claims."""
import hashlib
from io import BytesIO
import unittest

from PIL import Image

from timelapse.provenance import audit_frame, jpeg_evidence, xmp_evidence, STANDARD_XMP
from timelapse.xmp import parse


def recipe(exposure='0', contrast='10', frame='frame-17', extra='', date='2026:10:12 06:07:08'):
    return f'''<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:c="http://ns.adobe.com/camera-raw-settings/1.0/" xmlns:e="http://ns.adobe.com/exif/1.0/">
      <r:RDF><r:Description r:about="" c:Exposure2012="{exposure}" c:ProcessVersion="15.4"
        c:Contrast2012="{contrast}" c:Version="18.4" c:HasSettings="True" c:AlreadyApplied="False"
        e:ImageUniqueID="{frame}" e:DateTimeOriginal="{date}" e:ExposureTime="1/250"
        e:FNumber="1.8" e:ISOSpeedRatings="100">
        <c:Look><r:Description c:Name="Adobe Color" c:Version="1"><c:Parameters>
          <r:Description c:Exposure2012="0.17" c:Contrast2012="5" />
        </c:Parameters></r:Description></c:Look>{extra}
      </r:Description></r:RDF></x:xmpmeta>'''.encode()


def jpeg(packet=None, exif=None):
    out = BytesIO()
    kwargs = {'exif': exif} if exif is not None else {}
    Image.new('RGB', (40, 40), (90, 100, 110)).save(out, format='JPEG', **kwargs)
    data = out.getvalue()
    if packet is not None:
        payload = STANDARD_XMP + packet
        data = data[:2] + b'\xff\xe1' + (len(payload) + 2).to_bytes(2, 'big') + payload + data[2:]
    return data


def fixture(value=.532345):
    original = recipe()
    corrected = parse(original).updated(value)
    return {'original_xmp': original, 'corrected_xmp': corrected, 'current_xmp': corrected,
            'original_jpeg': jpeg(original), 'corrected_jpeg': jpeg(corrected)}


class ProvenanceTests(unittest.TestCase):
    def test_matching_recipes_and_expected_hashes_are_evidence_not_authentication(self):
        args = fixture()
        hashes = {key: hashlib.sha256(data).hexdigest() for key, data in args.items()}
        result = audit_frame(**args, expected_sha256=hashes)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['scope'], 'recorded_metadata_only')
        self.assertFalse(result['mismatches'])
        self.assertFalse(result['missing'])
        self.assertEqual(len([v for v in result['verified'] if v.get('field') == 'sha256']), 5)

    def test_generated_non_exposure_edit_fails_even_when_render_matches_it(self):
        args = fixture()
        args['corrected_xmp'] = args['corrected_xmp'].replace(b'c:Contrast2012="10"', b'c:Contrast2012="11"')
        args['current_xmp'] = args['corrected_xmp']
        args['corrected_jpeg'] = jpeg(args['corrected_xmp'])
        result = audit_frame(**args)
        self.assertEqual(result['status'], 'mismatch')
        self.assertTrue(any(v['check'] == 'exposure_only_change' for v in result['mismatches']))
        self.assertTrue(any(v.get('field') == 'crs.Contrast2012' for v in result['mismatches']))

    def test_missing_and_partial_jpeg_recipe_never_verify(self):
        for packet in (None, recipe().replace(b'c:ProcessVersion="15.4"', b'')):
            with self.subTest(packet=packet is None):
                args = fixture()
                args['original_jpeg'] = jpeg(packet)
                result = audit_frame(**args)
                self.assertEqual(result['status'], 'incomplete')
                self.assertTrue(result['missing'])

    def test_two_decimal_jpeg_exposure_is_reported_and_bounded(self):
        args = fixture()
        args['corrected_jpeg'] = jpeg(recipe(exposure='.53'))
        result = audit_frame(**args)
        self.assertEqual(result['status'], 'verified')
        item = next(v for v in result['verified'] if v['check'] == 'corrected_xmp->corrected_jpeg' and v['field'] == 'exposure')
        self.assertTrue(item['rounded'])
        self.assertAlmostEqual(item['difference'], -.002345)
        args['corrected_jpeg'] = jpeg(recipe(exposure='.54'))
        self.assertEqual(audit_frame(**args)['status'], 'mismatch')

    def test_current_sidecar_uses_strict_exposure_tolerance(self):
        args = fixture()
        args['current_xmp'] = recipe(exposure='.53')
        result = audit_frame(**args)
        self.assertEqual(result['status'], 'mismatch')
        self.assertTrue(any(v['check'] == 'corrected_xmp->current_xmp' and v.get('field') == 'exposure' for v in result['mismatches']))

    def test_wrong_frame_identity_and_hash_fail(self):
        args = fixture()
        args['corrected_jpeg'] = jpeg(recipe(exposure='.53', frame='frame-18'))
        result = audit_frame(**args, expected_sha256={'original_xmp': '0' * 64})
        self.assertEqual(result['status'], 'mismatch')
        fields = {v.get('field') for v in result['mismatches']}
        self.assertIn('sha256', fields)
        self.assertIn('identity.ImageUniqueID', fields)

    def test_fractional_numeric_identity_and_timestamp_precision_are_explicit(self):
        args = fixture()
        original = recipe(date='2026-10-12T06:07:08.468+02:00')
        args['original_xmp'] = original
        args['corrected_xmp'] = args['current_xmp'] = parse(original).updated(.532345)
        args['original_jpeg'] = jpeg(recipe().replace(b'e:ExposureTime="1/250"', b'e:ExposureTime=".004"'))
        result = audit_frame(**args)
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(any(v.get('precision') == 'common_recorded_precision_only' for v in result['verified']))
        args['corrected_jpeg'] = jpeg(recipe(exposure='.53', date='2026:10:12 06:07:09'))
        self.assertEqual(audit_frame(**args)['status'], 'mismatch')

    def test_nested_exposure_is_preserved_and_nested_changes_fail(self):
        original = recipe()
        self.assertEqual(xmp_evidence(original)['exposure'], 0)
        args = fixture()
        args['corrected_jpeg'] = jpeg(args['corrected_xmp'].replace(b'c:Exposure2012="0.17"', b'c:Exposure2012="0.18"'))
        result = audit_frame(**args)
        self.assertTrue(any(v.get('field') == 'crs.Look' for v in result['mismatches']))

    def test_ignored_workflow_fields_are_recorded_but_process_version_is_not_ignored(self):
        args = fixture()
        altered = args['corrected_xmp'].replace(b'c:AlreadyApplied="False"', b'c:AlreadyApplied="True"').replace(b'c:Version="18.4"', b'c:Version="18.5"')
        args['corrected_jpeg'] = jpeg(altered)
        result = audit_frame(**args)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['evidence']['corrected_jpeg']['ignored_fields']['AlreadyApplied'], 'true')
        args['corrected_jpeg'] = jpeg(altered.replace(b'c:ProcessVersion="15.4"', b'c:ProcessVersion="16"'))
        self.assertEqual(audit_frame(**args)['status'], 'mismatch')

    def test_extra_render_fields_require_unchanged_paired_recipe(self):
        args = fixture()
        extra = '<c:CropTop>0.0</c:CropTop><c:CropBottom>1.0</c:CropBottom>'
        args['original_jpeg'] = jpeg(recipe(extra=extra))
        args['corrected_jpeg'] = jpeg(recipe(exposure='.53', extra=extra))
        result = audit_frame(**args)
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(any(v.get('basis') == 'unchanged_in_original_and_corrected_embedded_recipe' for v in result['verified']))
        args['corrected_jpeg'] = jpeg(recipe(exposure='.53', extra=extra.replace('1.0', '.9')))
        self.assertEqual(audit_frame(**args)['status'], 'mismatch')

    def test_namespace_prefix_and_simple_property_serialization_are_semantic(self):
        original = recipe()
        changed = original.replace(b'xmlns:c=', b'xmlns:crs=').replace(b'c:', b'crs:')
        changed = changed.replace(b'crs:Contrast2012="10"', b'').replace(b'<crs:Look>', b'<crs:Contrast2012>10.000</crs:Contrast2012><crs:Look>')
        self.assertEqual(xmp_evidence(original)['crs_sha256'], xmp_evidence(changed)['crs_sha256'])

    def test_external_entities_duplicate_exposure_and_path_identity_are_rejected(self):
        malicious = [b'<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]>' + recipe(),
                     recipe(extra='<c:Exposure2012>1</c:Exposure2012>'),
                     recipe(extra='<c:RawFileName>../different.ARW</c:RawFileName>')]
        for packet in malicious:
            with self.subTest(packet=packet[:60]):
                with self.assertRaises(ValueError):
                    xmp_evidence(packet)
                args = fixture()
                args['corrected_jpeg'] = jpeg(packet)
                self.assertEqual(audit_frame(**args)['status'], 'mismatch')

    def test_jpeg_duplicate_packets_and_truncated_segments_fail(self):
        data = jpeg(recipe())
        payload = STANDARD_XMP + recipe()
        doubled = data[:2] + b'\xff\xe1' + (len(payload) + 2).to_bytes(2, 'big') + payload + data[2:]
        for data in (doubled, b'\xff\xd8\xff\xe1\xff\xffoops'):
            with self.assertRaises(ValueError):
                jpeg_evidence(data)

    def test_missing_frame_identity_cannot_be_replaced_by_camera_settings(self):
        original = recipe().replace(b'e:ImageUniqueID="frame-17"', b'').replace(b'e:DateTimeOriginal="2026:10:12 06:07:08"', b'')
        corrected = parse(original).updated(.5)
        result = audit_frame(original, corrected, corrected, jpeg(original), jpeg(corrected))
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(any(v.get('field') == 'frame_identity' for v in result['missing']))

    def test_conflicting_exif_and_xmp_identity_fails(self):
        exif = Image.Exif()
        exif[42016] = 'another-frame'
        with self.assertRaisesRegex(ValueError, '冲突身份字段'):
            jpeg_evidence(jpeg(recipe(), exif=exif))


if __name__ == '__main__':
    unittest.main()
