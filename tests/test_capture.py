import tempfile,unittest,json
from capture_bridge import CaptureBridge
from test_bridge import request,jpeg
from offline import decode_ipp
class CaptureTests(unittest.TestCase):
 def test_receives_original_without_usb_even_after_restart(self):
  with tempfile.TemporaryDirectory() as d:
   raw=jpeg();c=CaptureBridge('ipp://test.local:8633/ipp/print',d)
   self.assertEqual(decode_ipp(c.handle(request(2,data=raw))).code,0)
   row=c.rows()[0]
   from pathlib import Path
   self.assertEqual(Path(row['path']).read_bytes(),raw);self.assertEqual(row['phase'],'held')
   c.refresh();self.assertIsNone(row['device_id'])
   restarted=CaptureBridge(c.uri,d);restarted.refresh();self.assertIsNone(restarted.rows()[0]['device_id'])
   self.assertEqual(json.loads(row['request_meta'])['document_format'],'image/jpeg')
 def test_two_stage_capture_preserves_original_without_usb(self):
  from offline import Attribute as A
  from bridge import integer
  with tempfile.TemporaryDirectory() as d:
   c=CaptureBridge('ipp://test.local:8633/ipp/print',d);c.handle(request(5,10))
   raw=jpeg();r=c.handle(request(6,11,(A(1,0x21,'job-id',integer(1)),A(1,0x22,'last-document',b'\x01')),raw))
   self.assertEqual(decode_ipp(r).code,0)
   from pathlib import Path
   self.assertEqual(Path(c.rows()[0]['path']).read_bytes(),raw)
   c.refresh();self.assertIsNone(c.rows()[0]['device_id'])
