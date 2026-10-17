import unittest,tempfile,json
from aiohttp.test_utils import TestClient,TestServer
from bridge import make_app
from queued_bridge import QueuedBridge
from test_bridge import request
from offline import decode_ipp,Attribute as A
class HttpTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.core=QueuedBridge('ipp://test.local:8632/ipp/print',lambda:(_ for _ in ()).throw(AssertionError('no USB')),self.tmp.name)
  self.client=TestClient(TestServer(make_app(self.core)));await self.client.start_server()
 async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()
 async def test_expect_and_chunked_capability_requests_no_print(self):
  raw=request(11,1)
  r=await self.client.post('/ipp/print',data=raw,headers={'Content-Type':'application/ipp','Expect':'100-continue'})
  self.assertEqual(r.status,200);self.assertEqual(decode_ipp(await r.read()).code,0)
  async def chunks():
   yield raw[:10];yield raw[10:]
  r=await self.client.post('/ipp/print',data=chunks(),headers={'Content-Type':'application/ipp'});self.assertEqual(r.status,200)
  from pathlib import Path
  logs=[json.loads(x) for x in (Path(self.tmp.name)/'ipp-metadata.jsonl').read_text().splitlines()]
  self.assertTrue(any(x['expect_continue'] for x in logs));self.assertTrue(any(x['chunked'] for x in logs));self.assertEqual(self.core.rows(),[])
 async def test_rejections_logged_without_identity_or_document(self):
  raw=request(0x7777,9,(A(1,0x42,'requesting-user-name',b'PRIVATE_USERNAME'),A(1,0x42,'job-name',b'PRIVATE_TITLE')),b'PRIVATE_DOCUMENT')
  r=await self.client.post('/ipp/print',data=raw,headers={'Content-Type':'application/ipp'});self.assertEqual(decode_ipp(await r.read()).code,0x0501)
  r=await self.client.post('/bad-path',data=b'PRIVATE_DOCUMENT');self.assertEqual(r.status,404)
  from pathlib import Path
  log=(Path(self.tmp.name)/'ipp-metadata.jsonl').read_text()
  self.assertNotIn('PRIVATE_',log);self.assertNotIn('PRIVATE_DOCUMENT'.encode().hex(),log)
  self.assertIn('ipp_status',log);self.assertIn('404',log)
 async def test_status_requests_work_while_document_is_preparing(self):
  import asyncio,time
  from test_bridge import jpeg
  original=self.core.render
  def slow(*args):time.sleep(0.15);return original(*args)
  self.core.render=slow
  pending=asyncio.create_task(self.client.post('/ipp/print',data=request(2,21,data=jpeg()),headers={'Content-Type':'application/ipp'}))
  await asyncio.sleep(0.05)
  status=await self.client.post('/ipp/print',data=request(11,22),headers={'Content-Type':'application/ipp'})
  self.assertEqual(status.status,200)
  self.assertEqual(decode_ipp(await status.read()).code,0)
  submit=await pending;self.assertEqual(decode_ipp(await submit.read()).code,0)
