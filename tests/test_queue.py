import unittest,tempfile,json
from test_bridge import request,jpeg
from offline import decode_ipp,Attribute as A
from bridge import integer
from queued_bridge import QueuedBridge
class Peer:
 def __init__(self):self.responses=[];self.calls=[];self.jid=80;self.polls=0;self.busy=False;self.reject8006=False;self.lost_ack=False;self.disconnected=False;self.state=3;self.alert_code=''
 def write(self,data,timeout):
  if data.startswith(b'cmd json\n'):
   c=json.loads(data[9:]);m=c['method'];self.calls.append(m)
   if m=='get-job-id-list':r={'job-id-list':'external' if self.busy else ''}
   elif m=='get_prop':
    if c['params']==['printer-state-alerts']:r=['::'+(self.alert_code or '0')+'%']
    elif c['params']==['media-size']:r=[{'media-size':5012}]
    elif c['params']==['ribbon-info']:r=[{'ribbon-info':[1,1,38]}]
    else:r={'category':'processing' if self.busy else 'idle','error':0}
   elif m=='print-job':
    if self.reject8006:self.reject8006=False;r={'error-code':8006}
    else:self.jid+=1;self.polls=0;r={'job_id':self.jid}
   elif m=='get-job-info':
    self.polls+=1;r=[{'job-id':self.jid,'job-state':self.state if self.polls==1 else 9}]
   elif m=='cancel-job':r={'error-code':0};self.state=8
   self.responses.append(json.dumps({'id':c['id'],'result':r}).encode())
  else:
   self.calls.append('data')
   if not self.lost_ack:self.responses.append(data.split(b'\n',1)[0]+b' OK\n')
  return len(data)
 def read(self,n,timeout):
  if self.disconnected or not self.responses:raise TimeoutError('connection/ACK lost')
  return self.responses.pop(0)
 def close(self):pass
class QueueTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.peer=Peer();self.core=QueuedBridge('ipp://test.local:8632/ipp/print',lambda:self.peer,self.tmp.name,isolated_render=False)
 def tearDown(self):self.core.close();self.tmp.cleanup()
 def put(self,rid=1):return self.core.handle(request(2,rid,data=jpeg()))
 def test_fifo_multi_and_restart_completed_never_replays(self):
  raw=request(2,1,data=jpeg());self.core.handle(raw);self.put(2)
  self.assertEqual([j['phase'] for j in self.core.rows()],['pending','pending'])
  for _ in range(4):self.core.refresh()
  self.assertEqual([j['phase'] for j in self.core.rows()],['completed','completed'])
  count=self.peer.calls.count('print-job');new=QueuedBridge(self.core.uri,lambda:self.peer,self.tmp.name,isolated_render=False)
  self.assertEqual(decode_ipp(new.handle(raw)).values('job-state'),[integer(9)]);new.refresh();self.assertEqual(self.peer.calls.count('print-job'),count)
 def test_busy_and_explicit8006_wait_without_drop(self):
  self.put();self.peer.busy=True;self.core.refresh();self.assertNotIn('print-job',self.peer.calls)
  self.peer.busy=False;self.peer.reject8006=True;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'pending')
  self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'processing')
 def test_lost_ack_pauses_and_restart_never_reuploads(self):
  self.put();self.peer.lost_ack=True;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'unknown')
  count=len(self.peer.calls);new=QueuedBridge(self.core.uri,lambda:self.peer,self.tmp.name,isolated_render=False);new.refresh();self.assertEqual(len(self.peer.calls),count)
 def test_connection_failure_pending_preserved(self):
  self.put();self.core.transport_factory=lambda:(_ for _ in ()).throw(OSError('offline'));self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'pending')
  self.core.transport_factory=lambda:self.peer;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'processing')
 def test_pending_cancel_does_not_touch_device(self):
  self.put();self.put(2);self.core.handle(request(8,3,(A(1,0x21,'job-id',integer(2)),)));self.core.refresh()
  self.assertEqual(self.core.rows()[1]['phase'],'canceled');self.assertNotIn('cancel-job',self.peer.calls)
 def test_device_alert_waits_no_creation(self):
  self.put();self.peer.busy=True;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'pending')
 def test_get_jobs_groups_and_format_validation(self):
  self.put();self.put(2);r=decode_ipp(self.core.handle(request(10,3)));self.assertEqual(r.values('job-id'),[integer(1),integer(2)])
  self.assertEqual(decode_ipp(self.core.handle(request(4,4,(A(1,0x49,'document-format',b'image/png'),)))).code,0x040a)
 def test_restart_after_upload_polls_only_existing_device_id(self):
  self.put();self.core.refresh();count=self.peer.calls.count('print-job');data=self.peer.calls.count('data')
  new=QueuedBridge(self.core.uri,lambda:self.peer,self.tmp.name,isolated_render=False);new.refresh()
  self.assertEqual(new.rows()[0]['phase'],'completed');self.assertEqual(self.peer.calls.count('print-job'),count);self.assertEqual(self.peer.calls.count('data'),data)
 def test_uncertain_create_never_recreates(self):
  self.put();self.peer.disconnected=True;self.core.refresh()
  # This failure occurs at readiness, before creation, so remains safely pending.
  self.assertEqual(self.core.rows()[0]['phase'],'pending')
  self.peer.disconnected=False;self.peer.responses=[]
  original=self.peer.read
  def read(n,t):
   if self.peer.calls[-1]=='print-job':raise TimeoutError('create response lost')
   return original(n,t)
  self.peer.read=read;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'unknown')
  n=self.peer.calls.count('print-job');self.core.refresh();self.assertEqual(self.peer.calls.count('print-job'),n)
 def test_device_fault_pending_and_recovery(self):
  self.put();original=self.peer.write
  def write(data,t):
   n=original(data,t)
   if data.startswith(b'cmd json\n') and json.loads(data[9:])['method']=='get_prop':
    c=json.loads(data[9:]);self.peer.responses[-1]=json.dumps({'id':c['id'],'result':{'category':'error','error':8011}}).encode()
   return n
  self.peer.write=write;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'pending');self.assertNotIn('print-job',self.peer.calls)
  self.peer.write=original;self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'processing')
 def test_active_cancel_targets_only_own_job(self):
  self.put();self.core.refresh();device=self.core.rows()[0]['device_id']
  self.core.handle(request(8,2,(A(1,0x21,'job-id',integer(1)),)));self.core.refresh()
  self.assertEqual(self.peer.calls.count('cancel-job'),1);self.assertEqual(device,81)
 def test_six_inch_orientation_scaling(self):
  for orientation in (3,4,5,6):
   for scaling in (b'fill',b'fit'):
    r=self.core.handle(request(4,44,(A(2,0x23,'orientation-requested',integer(orientation)),A(2,0x44,'print-scaling',scaling))))
    self.assertEqual(decode_ipp(r).code,0)
  r=self.core.handle(request(4,45,(A(2,0x44,'media',b'na_index-3x5_3x5in'),)))
  self.assertEqual(decode_ipp(r).code,0x040b)
 def test_pdf_passes_fill_to_conservative_photo_recognizer(self):
  import queued_bridge
  called=[];original=queued_bridge.prepare_document
  queued_bridge.prepare_document=lambda *a,**k:(called.append(k) or original(jpeg(),'image/jpeg',**k))
  try:self.core.render(decode_ipp(request(2,data=b'pdf')),'application/pdf','fill',True)
  finally:queued_bridge.prepare_document=original
  self.assertEqual(called[0],{'scaling':'fill','rotate_180':False})

 def test_absent_device_job_aborts_without_replay(self):
  from protocol import ProtocolError
  self.put();self.core.refresh();calls=self.peer.calls.count('print-job')
  self.core.active_session.poll=lambda:(_ for _ in ()).throw(ProtocolError('matching job absent or duplicated; not proof of completion'))
  self.core.refresh()
  self.assertEqual(self.core.rows()[0]['phase'],'aborted')
  self.assertEqual(self.peer.calls.count('print-job'),calls)
  self.assertFalse(self.core.active_session)
 def test_preflight_alert_blocks_job_creation(self):
  self.peer.alert_code='5416';self.put();self.core.refresh()
  self.assertEqual(self.core.rows()[0]['phase'],'pending');self.assertNotIn('print-job',self.peer.calls)
  self.assertIn('5416',self.core.rows()[0]['message'])
  self.peer.alert_code='';self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'processing')
 def test_held_fatal_alert_cancels_without_replay(self):
  self.put();self.peer.state=4;self.core.refresh();calls=self.peer.calls.count('print-job')
  self.peer.alert_code='5416';self.peer.polls=0;self.core.refresh()
  self.assertEqual(self.core.rows()[0]['phase'],'aborted')
  self.assertIn('5416',self.core.rows()[0]['message'])
  self.assertEqual(self.peer.calls.count('print-job'),calls)
  self.assertEqual(self.peer.calls.count('cancel-job'),1)
 def test_device_held_maps_stopped_without_recreating(self):
  self.put();self.peer.state=4;self.core.refresh()
  self.assertEqual(self.core.rows()[0]['phase'],'held')
  r=decode_ipp(self.core.handle(request(9,20,(A(1,0x21,'job-id',integer(1)),))))
  self.assertEqual(r.values('job-state'),[integer(6)])
  self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'completed')
  self.assertEqual(self.peer.calls.count('print-job'),1)
 def test_unimplemented_scaling_and_alignment_rejected(self):
  for name,value in [('print-scaling',b'none'),('print-scaling',b'auto'),('x-image-position',b'left'),('media-type',b'stationery')]:
   r=decode_ipp(self.core.handle(request(4,90,(A(2,0x44,name,value),))))
   self.assertEqual(r.code,0x040b)
 def test_only_layout_metadata_persisted(self):
  self.put();meta=json.loads(self.core.rows()[0]['request_meta'])
  self.assertEqual(meta['document_format'],'image/jpeg')
  self.assertGreater(meta['document_bytes'],0)
  self.assertNotIn('requesting-user-name',str(meta))
 def test_create_send_document_is_durable_and_not_duplicated(self):
  created=decode_ipp(self.core.handle(request(5,101)))
  jid=created.values('job-id')[0];self.assertEqual(created.code,0)
  self.core.refresh();self.assertNotIn('print-job',self.peer.calls)
  raw=request(6,102,(A(1,0x21,'job-id',jid),A(1,0x22,'last-document',b'\x01'),A(1,0x49,'document-format',b'image/jpeg')),jpeg())
  self.assertEqual(decode_ipp(self.core.handle(raw)).code,0)
  self.assertEqual(decode_ipp(self.core.handle(raw)).code,0)
  self.assertEqual(len(self.core.rows()),1)
  self.core.refresh();self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'completed')
  self.assertEqual(self.peer.calls.count('print-job'),1)
  self.assertEqual(decode_ipp(self.core.handle(raw)).values('job-state'),[integer(9)])
 def test_non_final_send_document_rejected_without_usb(self):
  self.core.handle(request(5,201))
  raw=request(6,202,(A(1,0x21,'job-id',integer(1)),A(1,0x22,'last-document',b'\x00')),jpeg())
  self.assertEqual(decode_ipp(self.core.handle(raw)).code,0x040b)
  self.core.refresh();self.assertFalse(self.peer.calls)
 def test_ios_high_quality_pdf_validation(self):
  attrs=(A(1,0x49,'document-format',b'application/pdf'),A(2,0x23,'print-quality',integer(5)),A(2,0x21,'copies',integer(1)))
  self.assertEqual(decode_ipp(self.core.handle(request(4,1307,attrs))).code,0)
 def test_opt_in_content_retry_guard_handles_new_request_ids(self):
  self.core.dedupe_seconds=300
  self.put(1308);self.put(1313)
  self.assertEqual(len(self.core.rows()),1)
  self.core.refresh();self.core.refresh();self.put(1318)
  self.assertEqual(len(self.core.rows()),1);self.assertEqual(self.peer.calls.count('print-job'),1)
 def test_sleep_empty_error_free_is_vendor_ready_state(self):
  self.put();original=self.peer.write
  def write(data,t):
   n=original(data,t)
   if data.startswith(b'cmd json\n') and json.loads(data[9:])['method']=='get_prop':
    c=json.loads(data[9:]);self.peer.responses[-1]=json.dumps({'id':c['id'],'result':{'category':'sleep','sub-category':'idle','error':0}}).encode()
   return n
  self.peer.write=write;self.core.refresh();self.assertEqual(self.peer.calls.count('print-job'),1)
  self.assertEqual(self.core.rows()[0]['phase'],'processing')

class EvidenceRetentionTests(unittest.TestCase):
 def test_opt_in_retains_original_and_final_without_usb(self):
  import tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as folder:
   core=QueuedBridge('ipp://localhost/ipp/print',lambda:None,folder,isolated_render=False,retain_layout_evidence=True)
   req=decode_ipp(request(2,data=jpeg()));image=core.render(req,'image/jpeg','fill',False)
   files=list((Path(folder)/'layout-evidence').iterdir())
   self.assertEqual(len(files),2)
   self.assertTrue(any(p.read_bytes()==req.document for p in files));self.assertTrue(any(p.read_bytes()==image.jpeg for p in files))
   self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in files))
   core.db.close()
