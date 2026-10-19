"""Bounded, metadata-only HTTP/IPP diagnostics; never stores document bytes."""
import json,time,threading,traceback
from pathlib import Path
from offline import decode_ipp
SAFE={'document-format','media','media-col','media-type','copies','sides','orientation-requested','print-scaling','x-image-position','y-image-position','printer-resolution','print-quality','last-document','job-id','compression'}
lock=threading.Lock()
def describe(raw):
 try:
  q=decode_ipp(raw)
  return {'operation':q.code,'request_id':q.request_id,'document_bytes':len(q.document),'attributes':[{'name':a.name,'tag':a.tag,'hex':a.value.hex()[:8192]} for a in q.attributes if a.name in SAFE]}
 except Exception as e:return {'decode_error':type(e).__name__,'bytes':len(raw)}
def record(core,item):
 if not hasattr(core,'spool'):return
 p=Path(core.spool)/'ipp-metadata.jsonl'
 with lock:
  if p.exists() and p.stat().st_size>2*1024*1024:p.replace(p.with_name('ipp-metadata.previous.jsonl'))
  with p.open('a') as f:f.write(json.dumps({'utc':time.time(),**item})+'\n')
def stack(exc):return {'type':type(exc).__name__,'stack':[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name} for x in traceback.extract_tb(exc.__traceback__)]}
