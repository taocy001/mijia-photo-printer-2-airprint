"""Persistent local FIFO; one USB worker; uncertain stages never replay."""
import sqlite3,json,time,threading,hashlib,subprocess,sys,struct,os
from pathlib import Path
from bridge import BridgeCore,IppFailure,integer
from offline import Attribute as A,Message,decode_ipp,encode_ipp,PreparedImage
from documents import prepare_document
from protocol import PhotoSession,Job,DeviceJobState,ProtocolError
TERMINAL=('completed','aborted','canceled')
STATE={'receiving':4,'pending':3,'creating':5,'uploading':5,'uploaded':5,'processing':5,'unknown':6,'held':6,'waiting_device':3,'completed':9,'aborted':8,'canceled':7}
class QueuedBridge(BridgeCore):
 def __init__(self,uri,transport_factory,spool,*,isolated_render=True,dedupe_seconds=0,retain_layout_evidence=False):
  super().__init__(uri,transport_factory)
  self.lock=threading.RLock()
  self.dedupe_seconds=dedupe_seconds
  self.retain_layout_evidence=retain_layout_evidence
  self.spool=Path(spool);self.spool.mkdir(parents=True,exist_ok=True);os.chmod(self.spool,0o700)
  self.db=sqlite3.connect(self.spool/'queue.sqlite',check_same_thread=False)
  self.db.row_factory=sqlite3.Row;self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
  self.db.execute('CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,key TEXT UNIQUE,phase TEXT,device_id INTEGER,path TEXT,bytes INTEGER,acked INTEGER DEFAULT 0,message TEXT,cancel INTEGER DEFAULT 0,created REAL)')
  if 'request_meta' not in [r[1] for r in self.db.execute('PRAGMA table_info(jobs)')]:self.db.execute('ALTER TABLE jobs ADD COLUMN request_meta TEXT')
  self.db.execute("UPDATE jobs SET phase='unknown',message='Restart during irreversible stage; read-only reconciliation required' WHERE phase IN ('creating','uploading')");self.db.commit()
  for col in ('template','document_key','content_key'):
   if col not in [r[1] for r in self.db.execute('PRAGMA table_info(jobs)')]:self.db.execute('ALTER TABLE jobs ADD COLUMN '+col+' TEXT')
  self.db.commit()
  self.isolated_render=isolated_render;self.usb_lock=threading.Lock();self.active_session=None;self.active_id=None;self.stopping=False
 def rows(self):
  with self.lock:return [dict(x) for x in self.db.execute('SELECT * FROM jobs ORDER BY id')]
 def change(self,jid,**fields):
  with self.lock:
   self.db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),jid));self.db.commit()
   if fields.get('phase') in TERMINAL:self.db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time(),jid));self.db.commit()
 def attrs(self,j):
  state=STATE[j['phase']];reason={'receiving':'job-data-insufficient','pending':'job-queued','unknown':'job-hold-until-specified','held':'printer-stopped','waiting_device':'job-queued','completed':'job-completed-successfully','aborted':'aborted-by-system','canceled':'job-canceled-by-user'}.get(j['phase'],'job-printing')
  return [A(2,0x21,'job-id',integer(j['id'])),A(2,0x45,'job-uri',(self.uri+'/jobs/'+str(j['id'])).encode()),A(2,0x23,'job-state',integer(state)),A(2,0x44,'job-state-reasons',reason.encode()),A(2,0x41,'job-state-message',j['message'].encode()[:512])]
 def printer_attributes(self):
  base=super().printer_attributes();rows=self.rows();n=sum(x['phase'] not in TERMINAL for x in rows);unknown=any(x['phase']=='unknown' for x in rows);busy=any(x['phase'] in ('creating','uploading','uploaded','processing') for x in rows)
  changes={'queued-job-count':integer(n),'printer-state':integer(5 if unknown else 4 if busy else 3),'printer-is-accepting-jobs':bytes([n<20 and not unknown]),'printer-state-message':b'Six inch only; persistent FIFO; one copy per job','printer-state-reasons':b'paused' if unknown else b'none'}
  return [A(a.group,a.tag,a.name,changes.get(a.name,a.value)) for a in base]+[A(4,0x23,"operations-supported",integer(x)) for x in (5,6)]
 def retain_evidence(self,req,mime,image=None):
  if not self.retain_layout_evidence:return
  root=self.spool/'layout-evidence';root.mkdir(mode=0o700,exist_ok=True)
  key=hashlib.sha256(req.document).hexdigest();target=root/(key+('.final.jpg' if image is not None else '.input'))
  if target.exists():return
  data=image.jpeg if image is not None else req.document
  if sum(p.stat().st_size for p in root.iterdir() if p.is_file())+len(data)>256*1024*1024 or (image is None and len(list(root.glob('*.input')))>=20):raise IppFailure(0x0507,'layout evidence limit reached; review retained local diagnostics')
  with target.open('xb') as f:
   os.chmod(target,0o600);f.write(data);f.flush();os.fsync(f.fileno())
 def render(self,req,mime,scaling,reverse):
  self.retain_evidence(req,mime)
  # Recognized single-photo PDF may fill; ambiguous document pages preserve layout.
  orientation=None
  if mime!='image/jpeg':
   if mime!='application/pdf':scaling='fit'
   reverse=False
  else:
   values=req.values('orientation-requested')
   orientation=struct.unpack('>i',values[0])[0] if values else None
   if orientation is not None:reverse=False
  if not self.isolated_render:
   kwargs={'scaling':scaling,'rotate_180':reverse}
   if mime=='image/jpeg':kwargs['orientation']=orientation
   image=prepare_document(req.document,mime,**kwargs);self.retain_evidence(req,mime,image);return image
  try:
   r=subprocess.run([sys.executable,str(Path(__file__).with_name('render_worker.py'))],input=json.dumps({'mime':mime,'scaling':scaling,'reverse':reverse,'orientation':orientation}).encode()+b'\n'+req.document,capture_output=True,timeout=30,check=True)
   image=PreparedImage(r.stdout,0);self.retain_evidence(req,mime,image);return image
  except (subprocess.SubprocessError,OSError) as e:raise IppFailure(0x040b,'document renderer rejected input') from e
 def handle(self,raw):
  req=decode_ipp(raw);op=[A(1,0x47,'attributes-charset',b'utf-8'),A(1,0x48,'attributes-natural-language',b'en')];attrs=[];status=0;groups=[]
  try:
   if req.code==11:attrs=self.printer_attributes()
   elif req.code==4:self._validate(req)
   elif req.code==5:
    self._validate(req);key=hashlib.sha256(raw).hexdigest()
    with self.lock:
     old=self.db.execute('SELECT * FROM jobs WHERE key=?',(key,)).fetchone()
     if old:attrs=self.attrs(dict(old))
     else:
      if sum(j['phase'] not in TERMINAL for j in self.rows())>=20:raise IppFailure(0x0507,'queue limit 20')
      allowed={'document-format','media','media-col','media-type','copies','sides','orientation-requested','print-scaling','x-image-position','y-image-position','printer-resolution','print-quality','compression'}
      template=[{'group':a.group,'tag':a.tag,'name':a.name,'hex':a.value.hex()} for a in req.attributes if a.name in allowed]
      cur=self.db.execute('INSERT INTO jobs(key,phase,path,bytes,message,created,template) VALUES(?,?,?,?,?,?,?)',(key,'receiving','',0,'Awaiting one final Send-Document',time.time(),json.dumps(template)));self.db.commit()
      attrs=self.attrs(dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(cur.lastrowid,)).fetchone()))
   elif req.code==6:
    val=self._one(req,'job-id')
    if val is None:val=integer(int(self._one(req,'job-uri',b'').decode().rsplit('/',1)[-1]))
    if len(val)!=4:raise IppFailure(0x0400,'invalid job id')
    jid=struct.unpack('>i',val)[0]
    if self._one(req,'last-document')!=b'\x01':raise IppFailure(0x040b,'one final document required')
    rows=[j for j in self.rows() if j['id']==jid]
    if not rows:raise IppFailure(0x0406,'job not found')
    j=rows[0];dk=hashlib.sha256(req.document).hexdigest()
    if j['phase']!='receiving':
     if j.get('document_key')==dk:attrs=self.attrs(j)
     else:raise IppFailure(0x0405,'document already supplied or task closed')
    else:
     saved=[A(x['group'],x['tag'],x['name'],bytes.fromhex(x['hex'])) for x in json.loads(j['template'])]
     # Send-Document carries the final format; other job attributes come from Create-Job.
     formats=req.values('document-format')
     if formats:saved=[a for a in saved if a.name!='document-format']+[A(1,0x49,'document-format',formats[0])]
     merged=Message(req.version,2,req.request_id,tuple(saved),req.document)
     mime,scaling,reverse=self._validate(merged)
     if sum(p.stat().st_size for p in self.spool.glob('*.jpg'))>256*1024*1024:raise IppFailure(0x0507,'spool disk limit')
     image=self.render(merged,mime,scaling,reverse);path=self.spool/(j['key']+'.jpg');tmp=path.with_suffix('.tmp')
     with tmp.open('wb') as f:f.write(image.jpeg);f.flush();os.fsync(f.fileno())
     tmp.replace(path)
     meta={'document_format':mime,'document_bytes':len(req.document),'attributes':[{'name':a.name,'tag':a.tag,'value_hex':a.value.hex()} for a in saved]}
     self.change(jid,phase='pending',path=str(path),bytes=len(image.jpeg),document_key=dk,request_meta=json.dumps(meta),message='Document accepted; waiting for device idle')
     attrs=self.attrs(next(x for x in self.rows() if x['id']==jid))
   elif req.code==2:
    mime,scaling,reverse=self._validate(req);key=hashlib.sha256(raw).hexdigest()
    relevant={'document-format','copies','media','media-col','media-type','print-quality','print-scaling','orientation-requested'}
    options=sorted((a.name,a.value.hex()) for a in req.attributes if a.name in relevant)
    content_key=hashlib.sha256(req.document+json.dumps(options).encode()).hexdigest()
    with self.lock:
     old=self.db.execute('SELECT * FROM jobs WHERE key=?',(key,)).fetchone()
     if old is None and self.dedupe_seconds:
      old=self.db.execute("SELECT * FROM jobs WHERE content_key=? AND (phase NOT IN ('completed','aborted','canceled') OR created>?) ORDER BY id DESC LIMIT 1",(content_key,time.time()-self.dedupe_seconds)).fetchone()
     if old:attrs=self.attrs(dict(old))
    if not attrs:
     rows=self.rows()
     if sum(x['phase'] not in TERMINAL for x in rows)>=20:raise IppFailure(0x0507,'queue limit 20')
     if sum(p.stat().st_size for p in self.spool.glob('*.jpg'))>256*1024*1024:raise IppFailure(0x0507,'spool disk limit')
     image=self.render(req,mime,scaling,reverse)
     with self.lock:
      path=self.spool/(key+'.jpg');tmp=path.with_suffix('.tmp')
      with tmp.open('wb') as f:f.write(image.jpeg);f.flush();os.fsync(f.fileno())
      tmp.replace(path)
      cur=self.db.execute('INSERT INTO jobs(key,phase,path,bytes,message,created) VALUES(?,?,?,?,?,?)',(key,'pending',str(path),len(image.jpeg),'Queued; waiting for device idle',time.time()));self.db.commit()
      self.db.execute('UPDATE jobs SET content_key=? WHERE id=?',(content_key,cur.lastrowid));self.db.commit()
      allowed={'document-format','media','media-col','media-type','copies','orientation-requested','print-scaling','x-image-position','y-image-position','printer-resolution','print-quality','sides'}
      meta={'document_format':mime,'document_bytes':len(req.document),'attributes':[{'name':a.name,'tag':a.tag,'value_hex':a.value.hex()} for a in req.attributes if a.name in allowed]}
      self.db.execute('UPDATE jobs SET request_meta=? WHERE id=?',(json.dumps(meta),cur.lastrowid));self.db.commit()
      attrs=self.attrs(dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(cur.lastrowid,)).fetchone()))
   elif req.code in (8,9,10):
    rows=self.rows()
    if req.code==10:
     which=self._one(req,'which-jobs',b'not-completed')
     if which not in (b'all',b'completed',b'not-completed'):raise IppFailure(0x040b,'unsupported which-jobs')
     for j in rows:
      if which==b'completed' and j['phase'] not in TERMINAL:continue
      if which==b'not-completed' and j['phase'] in TERMINAL:continue
      groups.append(self.attrs(j))
    else:
     val=self._one(req,'job-id')
     if val is None:
      uri=self._one(req,'job-uri',b'').decode();val=integer(int(uri.rsplit('/',1)[-1]))
     if len(val)!=4:raise IppFailure(0x0400,'invalid job id')
     jid=struct.unpack('>i',val)[0];match=[j for j in rows if j['id']==jid]
     if not match:raise IppFailure(0x0406,'job not found')
     j=match[0]
     if req.code==8:
      if j['phase'] in ('pending','receiving'):self.change(jid,phase='canceled',message='Canceled before device submission');j['phase']='canceled'
      elif j['phase'] not in TERMINAL:
       if j['phase']=='unknown':raise IppFailure(0x0500,'uncertain task; cancellation not confirmed')
       self.change(jid,cancel=1,message='Client requested cancellation; awaiting device confirmation')
     attrs=self.attrs(j)
   else:raise IppFailure(0x0501,'operation not supported')
  except IppFailure as e:status=e.status;op.append(A(1,0x41,'status-message',e.message.encode()))
  except (ValueError,UnicodeError) as e:status=0x040b;op.append(A(1,0x41,'status-message',str(e).encode()[:256]))
  response=encode_ipp(Message(req.version,status,req.request_id,tuple(op+attrs)))
  if groups and status==0:response=response[:-1]+b''.join(encode_ipp(Message(req.version,0,req.request_id,tuple(g)))[8:-1] for g in groups)+b'\x03'
  return response
 def refresh(self):
  if not self.usb_lock.acquire(blocking=False):return
  try:self._tick()
  finally:self.usb_lock.release()
 def audit(self,item):
  p=self.spool/'usb-audit.jsonl'
  if p.exists() and p.stat().st_size>16*1024*1024:p.replace(self.spool/'usb-audit.previous.jsonl')
  with p.open('a') as f:f.write(json.dumps({'utc':time.time(),**item})+'\n');f.flush();os.fsync(f.fileno())
 def audited(self,native,jid):
  owner=self
  class Audited:
   def write(self,data,timeout):
    meta={'job':jid,'operation':'write','bytes':len(data),'kind':'control' if data.startswith(b'cmd json\n') else 'data'}
    if meta['kind']=='control':meta['request']=json.loads(data[9:])
    owner.audit({**meta,'outcome':'attempting'})
    n=native.write(data,timeout);owner.audit({**meta,'outcome':'written','count':n});return n
   def read(self,size,timeout):
    data=native.read(size,timeout);owner.audit({'job':jid,'operation':'read','raw_hex':data.hex()});return data
   def close(self):
    if hasattr(native,'close'):native.close()
  return Audited()
 def _tick(self):
  rows=self.rows()
  if any(j['phase']=='unknown' for j in rows):return
  jobs=[j for j in rows if j['phase'] not in TERMINAL and j['phase']!='receiving']
  if not jobs:return
  j=jobs[0];jid=j['id'];s=self.active_session
  try:
   if s is None or self.active_id!=jid:
    if s and hasattr(s.transport,'close'):s.transport.close()
    s=PhotoSession(self.audited(self.transport_factory(),jid));self.active_session=s;self.active_id=jid
    if j['device_id']:
     s.job=Job(j['device_id'],phase='uploaded',uploaded_bytes=j['acked']);s.creation_attempted=True
   if j['phase']=='pending':
    ids=s.command('get-job-id-list',{});status=s.command('get_prop','status')
    if not isinstance(ids,dict) or not isinstance(status,dict):raise ProtocolError('invalid readiness response')
    if ids.get('job-id-list') not in ('',[]) or status.get('category') not in ('idle','sleep') or status.get('error')!=0:
     self.change(jid,message='Waiting for device idle; external job or device alert');return
    with self.lock:
     current=self.db.execute('SELECT phase FROM jobs WHERE id=?',(jid,)).fetchone()
     if current['phase']!='pending':return
     self.change(jid,phase='creating',message='Creating one device job')
    try:s.create(PreparedImage(Path(j['path']).read_bytes(),0))
    except ProtocolError as e:
     if str(e)=='print-job rejected: 8006':
      self.change(jid,phase='pending',message='Device queue full; safe wait before next attempt');self._disconnect();return
     if str(e).startswith('print-job rejected:'):
      self.change(jid,phase='aborted',message=str(e));self._disconnect();return
     raise
    self.change(jid,device_id=s.job.device_id,phase='uploading',message='Transmitting photo; no automatic replay')
    s.upload()
    self.change(jid,phase='uploaded',acked=s.job.uploaded_bytes,message='Upload acknowledged; awaiting device completion')
   if j['cancel']:
    # This device id came from this local client job only; never external jobs.
    self.change(jid,cancel=0,message='Cancellation command attempted once')
    s.cancel()
   info=s.poll();phase=s.job.phase
   if phase not in TERMINAL:phase='held' if s.job.device_state==DeviceJobState.PROCESSING_HELD else 'waiting_device' if s.job.device_state in (DeviceJobState.WAITING,DeviceJobState.PENDING) else 'processing'
   self.change(jid,phase=phase,message='Device state: '+s.job.device_state.name,acked=info.get('transfer-size',s.job.uploaded_bytes))
   if phase in TERMINAL:
    Path(j['path']).unlink(missing_ok=True);self._disconnect()
  except Exception as e:
   current=next(x for x in self.rows() if x['id']==jid)
   if current['phase'] in ('creating','uploading'):
    self.change(jid,phase='unknown',message='Uncertain irreversible outcome; stopped, no replay: '+str(e))
   else:self.change(jid,message='Connection/status unavailable; retained for read-only retry: '+str(e))
   self._disconnect()
 def _disconnect(self):
  if self.active_session and hasattr(self.active_session.transport,'close'):self.active_session.transport.close()
  self.active_session=None;self.active_id=None
 def close(self):
  self.stopping=True
  with self.usb_lock:self._disconnect()
