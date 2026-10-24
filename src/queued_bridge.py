"""Persistent local FIFO; one USB worker; uncertain stages never replay."""
import sqlite3,json,time,threading,hashlib,subprocess,sys,struct,os
from pathlib import Path
from bridge import BridgeCore,IppFailure,integer
from offline import Attribute as A,Message,decode_ipp,encode_ipp,PreparedImage,recompose_to_canvas,rotate180_pi,MEDIA_CANVAS,CANVAS,Unsupported
import offline as _offline
from offline import _media_override
from documents import prepare_document,prepare_splice_document
from protocol import PhotoSession,Job,DeviceJobState,ProtocolError
TERMINAL=('completed','aborted','canceled')
STATE={'receiving':4,'pending':3,'creating':5,'uploading':5,'uploaded':5,'processing':5,'unknown':6,'held':6,'waiting_device':3,'completed':9,'aborted':8,'canceled':7}
SUBSTATE_CN={3002:'传输文件中',3017:'装纸中',3018:'打印黄色(Y)',3019:'打印品红(M)',3020:'打印青色(C)',3021:'覆膜中',3025:'回纸',3026:'出纸',3036:'打印黑色(K)',3005:'处理中',3001:'打印中',3014:'渲染中',3016:'解码中',2001:'初始化吐纸',2002:'低功耗'}
# 告警文本 -> IPP printer-state-reasons 关键词 (按包含子串匹配)
ALERT_MAP=[('paper','media-empty'),('纸','media-empty'),('tray','media-empty'),('cover','cover-open'),
 ('盖','cover-open'),('ribbon','marker-supply-empty'),('色带','marker-supply-empty'),
 ('jam','media-jam'),('卡','media-jam')]
# 新固件告警为 "信息:警告:错误" 数字码格式 (如 "::5402"); 码表逆向自 Mi.Model alerts_syrup.xml
ALERT_CODE_MAP={'5304':'media-empty','5306':'media-empty','5402':'media-empty','5403':'media-jam',
 '5414':'media-empty','5416':'media-empty',
 '8101':'marker-supply-empty','8102':'marker-supply-empty','8103':'marker-supply-empty',
 '8104':'marker-supply-empty','8199':'marker-supply-empty'}
ALERT_CODE_CN={'5304':'相纸尺寸不符','5306':'纸盒未安装','5402':'缺纸或装纸失败','5403':'卡纸',
 '5414':'相纸长度异常','5416':'相纸尺寸异常','8101':'色带用完','8102':'色带已空','8103':'未识别色带',
 '8104':'色带类型不符','8199':'色带错误'}
class QueuedBridge(BridgeCore):
 def __init__(self,uri,transport_factory,spool,*,isolated_render=True,dedupe_seconds=0,retain_layout_evidence=False,idle_status_seconds=0):
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
  self.isolated_render=isolated_render;self.usb_lock=threading.Lock();self.active_session=None;self.active_id=None;self.stopping=False;self.stuck={}
  # 设备状态缓存: 空闲时定期查询 (默认关闭, 由 --idle-status-refresh 开启), 供 IPP 属性应答
  self.device_cache={'media':None,'alerts':None,'ribbon':None,'ts':0}
  self.idle_status_seconds=idle_status_seconds
 def _query_device_cache(self,s):
  """在已建立的会话上读取状态并写入缓存 (best-effort, 失败不中断打印)"""
  try:
   ms=s.command('get_prop',['media-size'])
   m0=ms[0] if isinstance(ms,list) and ms else {}
   self.device_cache['media']=int(m0.get('media-size',5012)) if isinstance(m0,dict) else 5012
   _offline.LAST_DEVICE_MEDIA=self.device_cache['media']   # 让渲染期的拼接覆盖守卫也用新鲜值
  except Exception:pass
  try:
   al=s.command('get_prop',['printer-state-alerts'])
   self.device_cache['alerts']=al[0] if isinstance(al,list) and al else None
  except Exception:pass
  try:
   rb=s.command('get_prop',['ribbon-info'])
   ri=rb[0].get('ribbon-info') if isinstance(rb,list) and rb and isinstance(rb[0],dict) else None
   self.device_cache['ribbon']=ri[2] if ri and len(ri)>2 else None
  except Exception:pass
  self.device_cache['ts']=time.time()
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
  # 设备告警 -> IPP printer-state-reasons
  reasons=[];alert_cn=''
  if self.device_cache['alerts']:
   txt=str(self.device_cache['alerts'])
   for pat,kw in ALERT_MAP:
    if pat in txt and kw not in reasons:reasons.append(kw)
   for fld in txt.split(':')[1:]:
    for code in fld.replace(',',' ').split():
     kw=ALERT_CODE_MAP.get(code)
     if kw and kw not in reasons:reasons.append(kw)
     if not alert_cn and code in ALERT_CODE_CN:alert_cn=ALERT_CODE_CN[code]+'('+code+')'
  if unknown and 'paused' not in reasons:reasons.append('paused')
  # 有告警时 printer-state-message 直接显示中文原因 (Mac/iOS 打印对话框可见); 平时显示当前纸型
  media=self.device_cache['media'] or 5012
  ov=_media_override()
  PAPER_CN={5009:'3寸方形背胶(即贴)',5012:'6寸',5014:'6寸双拼'}
  paper_txt=('6寸托盘·按4寸拼接版打印' if ov==5014 and media==5012 else PAPER_CN.get(media,str(media)))
  msg=('⚠ '+alert_cn) if alert_cn else f'就绪 · 当前相纸: {paper_txt}'
  changes={'queued-job-count':integer(n),'printer-state':integer(5 if unknown else 4 if busy else 3),'printer-is-accepting-jobs':bytes([n<20 and not unknown]),'printer-state-message':msg.encode(),'printer-state-reasons':b','.join(x.encode() for x in reasons) if reasons else b'none'}
  out=[A(a.group,a.tag,a.name,changes.get(a.name,a.value)) for a in base]+[A(4,0x23,"operations-supported",integer(x)) for x in (5,6)]
  # 色带余量 -> marker-supplies (Mac/iOS 打印对话框显示剩余色带)
  if self.device_cache['ribbon'] is not None:
   try:pct=max(0,min(100,round(int(self.device_cache['ribbon'])/40*100)))
   except (TypeError,ValueError):pct=-1
   out+=[A(4,0x44,'marker-types',b'ribbon'),A(4,0x42,'marker-names','色带剩余(张数)'.encode()),
    A(4,0x21,'marker-levels',integer(pct)),A(4,0x42,'marker-colors',b'#A0A0A0')]
  # 当前装载相纸提示
  if self.device_cache['media']:
   mn={5009:'3寸方形背胶(即贴)',5012:'6寸',5014:'6寸双拼'}.get(self.device_cache['media'])
   if mn:out.append(A(4,0x41,'printer-more-info',f'当前相纸: {paper_txt}'.encode()))
  return out
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
  # 5014 拼接版(纸盒恒报5012, 网页切纸型写覆盖文件): 1张->上下两半相同; 2页PDF->上下各一
  if _media_override()==5014:
   try:
    if not self.isolated_render:
     image=prepare_splice_document(req.document,mime,scaling=scaling)
    else:
     r=subprocess.run([sys.executable,str(Path(__file__).with_name('render_worker.py'))],input=json.dumps({'mime':mime,'scaling':scaling,'splice':True}).encode()+b'\n'+req.document,capture_output=True,timeout=30,check=True)
     image=PreparedImage(r.stdout,0)
    self.retain_evidence(req,mime,image);return image
   except Unsupported:pass   # URF/PWG 等不可拆页: 回退普通整版渲染
   except subprocess.CalledProcessError as e:
    if e.returncode!=2:raise IppFailure(0x040b,'document renderer rejected input') from e
   except (subprocess.SubprocessError,OSError) as e:raise IppFailure(0x040b,'document renderer rejected input') from e
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
  if not jobs:
   # 空闲时低速刷新设备状态缓存 (可选, 供 IPP 属性应答: 余量/告警/相纸)
   if self.idle_status_seconds>0 and self.transport_factory is not None and time.time()-self.device_cache['ts']>self.idle_status_seconds:
    s=None
    try:
     s=PhotoSession(self.audited(self.transport_factory(),None));self._query_device_cache(s)
    except Exception:pass
    finally:
     if s is not None and hasattr(s.transport,'close'):s.transport.close()
   return
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
    # 按设备实际装载的相纸尺寸重排版 (相纸自适应: 避免6寸排版打到4寸纸上)
    self._query_device_cache(s)
    media=self.device_cache['media'] or 5012
    _offline.LAST_DEVICE_MEDIA=media   # 供 _media_override 守卫: 非 6 寸托盘时拼接覆盖自动失效
    canvas=MEDIA_CANVAS.get(media)
    image=PreparedImage(Path(j['path']).read_bytes(),0)
    if canvas and canvas!=CANVAS:
     image=recompose_to_canvas(image.jpeg,canvas)
     self.change(jid,message=f'Re-composed for loaded media-size {media}')
    if media==5009:
     image=rotate180_pi(image)   # 方形背胶纸: 固件顶部留物理白边, 旋转 180° 落到底部
     self.change(jid,message='Rotated 180° for square sticky media')
    with self.lock:
     current=self.db.execute('SELECT phase FROM jobs WHERE id=?',(jid,)).fetchone()
     if current['phase']!='pending':return
     self.change(jid,phase='creating',message='Creating one device job')
    try:s.create(image)
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
   # 透传子状态 (打印Y/M/C/K/覆膜/装纸/出纸) 让 iOS 进度提示更真实
   sub=info.get('job-sub-state');sub_cn=SUBSTATE_CN.get(sub) if isinstance(sub,int) else None
   self.change(jid,phase=phase,message='Device state: '+s.job.device_state.name+(' · '+sub_cn if sub_cn else ''),acked=info.get('transfer-size',s.job.uploaded_bytes))
   # 卡死看门狗: 设备侧任务状态/子状态长时间无进展 -> 取消设备任务并中止本地任务, 释放 USB
   now=time.time();sig=(phase,info.get('job-sub-state'))
   tr=self.stuck.setdefault(jid,[now,None,now])
   if sig!=tr[1]:tr[1]=sig;tr[2]=now
   lim=180 if phase=='waiting_device' else 600
   if now-tr[2]>lim or now-tr[0]>900:
    alert=''
    try:
     al=s.command('get_prop',['printer-state-alerts']);code=str(al[0]).split(':')[-1] if al else ''
     if code and code!='0':alert='; device alert '+code
    except Exception:pass
    try:s.cancel()
    except Exception:pass
    self.change(jid,phase='aborted',message='Device job stalled'+alert+'; canceled and USB released');self.stuck.pop(jid,None)
    Path(j['path']).unlink(missing_ok=True);self._disconnect();return
   if phase in TERMINAL:
    self.stuck.pop(jid,None);Path(j['path']).unlink(missing_ok=True);self._disconnect()
  except Exception as e:
   current=next(x for x in self.rows() if x['id']==jid)
   if current['phase'] in ('creating','uploading'):
    self.change(jid,phase='unknown',message='Uncertain irreversible outcome; stopped, no replay: '+str(e))
   elif 'integer job state required' in str(e):
    self.change(jid,phase='aborted',message='Device job absent on device; aborted locally');self.stuck.pop(jid,None)
   else:self.change(jid,message='Connection/status unavailable; retained for read-only retry: '+str(e))
   self._disconnect()
 def _disconnect(self):
  if self.active_session and hasattr(self.active_session.transport,'close'):self.active_session.transport.close()
  self.active_session=None;self.active_id=None
 def close(self):
  self.stopping=True
  with self.usb_lock:self._disconnect()
