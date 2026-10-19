"""Optional receive-only diagnosis: preserves input bytes, NEVER opens USB."""
import hashlib,json,time,os,struct
from pathlib import Path
from queued_bridge import QueuedBridge
from bridge import IppFailure,integer
from offline import PreparedImage,decode_ipp,encode_ipp,Message,Attribute as A
ALLOWED={'document-format','media','media-col','media-type','copies','orientation-requested','print-scaling','x-image-position','y-image-position','printer-resolution','print-quality','sides'}
class CaptureBridge(QueuedBridge):
 def __init__(self,uri,spool):
  def forbidden():raise AssertionError('receive-only service cannot open USB')
  super().__init__(uri,forbidden,spool,isolated_render=False)
 def render(self,req,mime,scaling,reverse):return PreparedImage(req.document,0)
 def refresh(self):return # No path into USB scheduler, including after restart.
 def printer_attributes(self):
  attrs=super().printer_attributes()
  return [A(a.group,a.tag,a.name,b'Mijia Layout Capture - NO PRINT' if a.name=='printer-name' else b'Receive-only diagnostic; no paper output' if a.name=='printer-info' else a.value) for a in attrs]
 def handle(self,raw):
  req=decode_ipp(raw)
  if req.code not in (2,8):return super().handle(raw)
  op=[A(1,0x47,'attributes-charset',b'utf-8'),A(1,0x48,'attributes-natural-language',b'en')];attrs=[];status=0
  try:
   if req.code==8:
    val=self._one(req,'job-id')
    if val is None:val=integer(int(self._one(req,'job-uri',b'').decode().rsplit('/',1)[-1]))
    jid=struct.unpack('>i',val)[0];rows=[x for x in self.rows() if x['id']==jid]
    if not rows:raise IppFailure(0x0406,'capture not found')
    self.change(jid,phase='canceled',message='Local diagnostic capture canceled');attrs=self.attrs({**rows[0],'phase':'canceled'})
   else:
    mime,_,_=self._validate(req);key=hashlib.sha256(raw).hexdigest()
    with self.lock:
     old=self.db.execute('SELECT * FROM jobs WHERE key=?',(key,)).fetchone()
     if old:attrs=self.attrs(dict(old))
     else:
      if len(self.rows())>=20 or sum(p.stat().st_size for p in self.spool.glob('*.capture'))+len(req.document)>256*1024*1024:raise IppFailure(0x0507,'capture storage limit')
      path=self.spool/(key+'.capture')
      with path.open('xb') as f:f.write(req.document);f.flush();os.fsync(f.fileno())
      meta={'document_format':mime,'document_bytes':len(req.document),'attributes':[{'name':a.name,'tag':a.tag,'value_hex':a.value.hex()} for a in req.attributes if a.name in ALLOWED]}
      cur=self.db.execute('INSERT INTO jobs(key,phase,path,bytes,message,created,request_meta) VALUES(?,?,?,?,?,?,?)',(key,'held',str(path),len(req.document),'Captured locally; NEVER sent to USB',time.time(),json.dumps(meta)));self.db.commit()
      attrs=self.attrs(dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(cur.lastrowid,)).fetchone()))
  except IppFailure as e:status=e.status;op.append(A(1,0x41,'status-message',e.message.encode()))
  except (ValueError,struct.error) as e:status=0x040b;op.append(A(1,0x41,'status-message',str(e).encode()))
  return encode_ipp(Message(req.version,status,req.request_id,tuple(op+attrs)))
