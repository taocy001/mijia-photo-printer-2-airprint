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
