import unittest,tempfile,json
from test_bridge import request,jpeg
from offline import decode_ipp,Attribute as A
from bridge import integer
from queued_bridge import QueuedBridge
class Peer:
 def __init__(self):self.responses=[];self.calls=[];self.jid=80;self.polls=0;self.busy=False;self.reject8006=False;self.lost_ack=False;self.disconnected=False;self.state=3
 def write(self,data,timeout):
  if data.startswith(b'cmd json\n'):
   c=json.loads(data[9:]);m=c['method'];self.calls.append(m)
   if m=='get-job-id-list':r={'job-id-list':'external' if self.busy else ''}
   elif m=='get_prop':r={'category':'processing' if self.busy else 'idle','error':0}
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
   for scaling in (b'fill',b'fit',b'none',b'auto',b'auto-fit'):
    r=self.core.handle(request(4,44,(A(2,0x23,'orientation-requested',integer(orientation)),A(2,0x44,'print-scaling',scaling))))
    self.assertEqual(decode_ipp(r).code,0)
  r=self.core.handle(request(4,45,(A(2,0x44,'media',b'na_index-3x5_3x5in'),)))
  self.assertEqual(decode_ipp(r).code,0x040b)
 def test_already_laid_out_document_forces_fit(self):
  import queued_bridge
  called=[];original=queued_bridge.prepare_document
  queued_bridge.prepare_document=lambda *a,**k:(called.append(k) or original(jpeg(),'image/jpeg',**k))
  try:self.core.render(decode_ipp(request(2,data=b'pdf')),'application/pdf','fill',True)
  finally:queued_bridge.prepare_document=original
  self.assertEqual(called[0],{'scaling':'fit','rotate_180':False})

 def test_device_held_maps_stopped_without_recreating(self):
  self.put();self.peer.state=4;self.core.refresh()
  self.assertEqual(self.core.rows()[0]['phase'],'held')
  r=decode_ipp(self.core.handle(request(9,20,(A(1,0x21,'job-id',integer(1)),))))
  self.assertEqual(r.values('job-state'),[integer(6)])
  self.core.refresh();self.assertEqual(self.core.rows()[0]['phase'],'completed')
  self.assertEqual(self.peer.calls.count('print-job'),1)
