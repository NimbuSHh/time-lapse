"""Adobe reply contract and cross-workspace serialization, no Adobe calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from timelapse import automatic
from timelapse.photoshop import PhotoshopRenderer


class NativeEvidenceTests(unittest.TestCase):
    def test_render_records_native_and_output_dimensions_and_rejects_scaled_full_output(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t).resolve();raw=p/'frame.ARW';raw.write_bytes(b'private-test-copy')
            r=PhotoshopRenderer.__new__(PhotoshopRenderer);r.root=p;r.cancelled=lambda:False
            dest=p/'frame.jpg'
            def response(*args):
                dest.write_bytes(b'test-response-file');return 'TIMELAPSE_RENDERED|6000|4000|6000|4000'
            r._call=Mock(side_effect=response)
            self.assertEqual(r.render(raw,dest,0),dest)
            self.assertEqual(r.last_render,{'raw':str(raw),'native_size':[6000,4000],'output_size':[6000,4000],'edge':0})
            dest.unlink()
            def bad(*args):
                dest.write_bytes(b'bad');return 'TIMELAPSE_RENDERED|6000|4000|1600|1067'
            r._call.side_effect=bad
            with self.assertRaisesRegex(ValueError,'原生显影尺寸'):r.render(raw,dest,0)
            self.assertFalse((p/'render.lease').exists())

    def test_preview_compatibility_is_exact_and_bound_to_current_adobe_version(self):
        from timelapse import photoshop
        script=photoshop.SCRIPT.read_bytes()
        ids=photoshop.compatible_identities('27.8.0',script,photoshop.APPLESCRIPT)
        self.assertEqual(ids,['photoshop-27.8.0-'+photoshop.LEGACY_RENDER_DIGEST])
        self.assertEqual(photoshop.compatible_identities('27.8.0',script+b'//changed',photoshop.APPLESCRIPT),[])
        self.assertEqual(photoshop.compatible_identities('27.8.0',script,photoshop.APPLESCRIPT+'changed'),[])
        self.assertNotIn(ids[0],photoshop.compatible_identities('28.0',script,photoshop.APPLESCRIPT))

    def test_distinct_workspaces_share_one_adobe_lock(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            with automatic.render_lock(root/'one'):
                with self.assertRaisesRegex(ValueError,'仍在运行'):
                    with automatic.render_lock(root/'two'):self.fail('must not acquire')
            with automatic.render_lock(root/'two'):pass
