"""IPP bridge core and aiohttp app factory. Nothing listens on import/call.

All device operations require an explicitly supplied transport factory. The
default core remains disconnected and rejects jobs. Network startup is not
provided here; registration/listening is a separate, approval-bound step.
"""
from dataclasses import dataclass
from hashlib import sha256
import struct
import threading
import time
import uuid
import json
import base64
import subprocess
import sys
import ipaddress
from pathlib import Path
from offline import (Attribute as A, Message, Unsupported, decode_ipp, encode_ipp,
                     collection_values, encode_collection, PreparedImage)
from documents import FORMATS, prepare_document
from protocol import PhotoSession, ProtocolError, UncertainOutcome, ipp_job_state

MEDIA = b"na_index-4x6_4x6in"
URF = (b"V1.4", b"CP1", b"RS300", b"SRGB24", b"W8")
OPS = (2, 4, 8, 9, 10, 11)

def integer(v): return struct.pack(">i", v)


class IppFailure(Exception):
    def __init__(self, status, message): self.status, self.message = status, message


class BridgeCore:
    def __init__(self, uri="ipp://localhost:8632/ipp/print", transport_factory=None, *, state_path=None, isolated_render=False):
        self.uri, self.transport_factory = uri, transport_factory
        self.state_path=Path(state_path) if state_path else None
        self.isolated_render=isolated_render
        self.journal={}
        if self.state_path and self.state_path.exists():
            self.journal=json.loads(self.state_path.read_text())
        self.lock = threading.Lock()
        self.session = None
        self.job_id = 0
        self.last_key = None
        self.last_response = None
        self.last_time = 0
        self.state_message = "Ready; single-page / one-copy / six-inch; default fill crops edges"
        self.blocked = any(v.get("phase") not in ("completed","aborted","canceled") for v in self.journal.values())
        self.started = time.monotonic()
        self.identity = "urn:uuid:"+str(uuid.uuid5(uuid.NAMESPACE_URL, uri))

    def printer_attributes(self):
        accepting = not self.blocked and self.transport_factory is not None and (self.session is None or
            self.session.job is not None and self.session.job.phase in ("completed", "aborted", "canceled"))
        attrs = [A(4,0x42,"printer-name","小米照片打印机 2".encode("utf-8")),
            A(4,0x41,"printer-info","小米照片打印机 2".encode("utf-8")),
            A(4,0x45,"printer-uri-supported",self.uri.encode()),
            A(4,0x45,"printer-uuid",self.identity.encode()),
            A(4,0x44,"uri-security-supported",b"none"),
            A(4,0x44,"uri-authentication-supported",b"none"),
            A(4,0x22,"printer-is-accepting-jobs",bytes([bool(accepting)])),
            A(4,0x23,"printer-state",integer(3 if accepting else 5)),
            A(4,0x41,"printer-state-message",self.state_message.encode()[:512]),
            A(4,0x44,"printer-state-reasons",b"none" if accepting else b"paused"),
            A(4,0x42,"printer-make-and-model",b"Mijia Instant Photo Printer 2 (experimental bridge)"),
            A(4,0x47,"charset-configured",b"utf-8"),
            A(4,0x47,"charset-supported",b"utf-8"),
            A(4,0x48,"natural-language-configured",b"en"),
            A(4,0x48,"generated-natural-language-supported",b"en"),
            A(4,0x49,"document-format-default",b"image/jpeg"),
            A(4,0x44,"media-default",MEDIA), A(4,0x44,"media-supported",MEDIA), A(4,0x44,"media-ready",MEDIA),
            A(4,0x22,"color-supported",b"\x01"),
            A(4,0x44,"print-color-mode-supported",b"color"),
            A(4,0x44,"print-color-mode-default",b"color"),
            A(4,0x44,"sides-supported",b"one-sided"), A(4,0x44,"sides-default",b"one-sided"),
            A(4,0x21,"copies-default",integer(1)), A(4,0x33,"copies-supported",integer(1)+integer(1)),
            A(4,0x22,"multiple-document-jobs-supported",b"\0"),
            A(4,0x23,"orientation-requested-default",integer(3)),
            *[A(4,0x23,"orientation-requested-supported",integer(x)) for x in (3,4,5,6)],
            A(4,0x21,"queued-job-count",integer(0 if accepting else 1)),
            A(4,0x33,"job-k-octets-supported",integer(1)+integer(32768)),
            A(4,0x44,"compression-supported",b"none"),
            *[A(4,0x44,"media-col-supported",x) for x in (b"media-size",b"media-type",b"media-left-margin",b"media-right-margin",b"media-top-margin",b"media-bottom-margin")],
            A(4,0x44,"media-type-default",b"photographic"),A(4,0x44,"media-type-supported",b"photographic"),
            *[A(4,0x44,n,b"center") for n in ("x-image-position-default","x-image-position-supported","y-image-position-default","y-image-position-supported")],
            A(4,0x22,"job-ids-supported",b"\x01"),
            A(4,0x21,"printer-up-time",integer(int(time.monotonic()-self.started))),
            A(4,0x44,"print-scaling-default",b"fill"),
            A(4,0x44,"print-scaling-supported",b"fit"), A(4,0x44,"print-scaling-supported",b"fill"),

            A(4,0x23,"print-quality-default",integer(5)), A(4,0x23,"print-quality-supported",integer(5)),
            # 300dpi describes accepted client raster rendering, not device DPI.
            A(4,0x32,"printer-resolution-default",integer(300)+integer(300)+b"\x03"),
            A(4,0x32,"printer-resolution-supported",integer(300)+integer(300)+b"\x03"),
            A(4,0x32,"pwg-raster-document-resolution-supported",integer(300)+integer(300)+b"\x03"),
            A(4,0x44,"pwg-raster-document-type-supported",b"srgb_8"),
            A(4,0x44,"pwg-raster-document-type-supported",b"sgray_8")]
        attrs += [A(4,0x49,"document-format-supported",v.encode()) for v in FORMATS]
        attrs += [A(4,0x23,"operations-supported",integer(v)) for v in OPS]
        attrs += [A(4,0x44,"ipp-versions-supported",v) for v in (b"1.1",b"2.0")]
        attrs += [A(4,0x44,"urf-supported",v) for v in URF]
        size = {"media-size": (0x34,{"x-dimension":(0x21,integer(10160)),"y-dimension":(0x21,integer(15240))})}
        size["media-type"]=(0x44,b"photographic")
        size.update({k:(0x21,integer(0)) for k in ('media-left-margin','media-right-margin','media-top-margin','media-bottom-margin')})
        attrs.extend(A(4,0x21,k,integer(0)) for k in ('media-left-margin-supported','media-right-margin-supported','media-top-margin-supported','media-bottom-margin-supported'))
        attrs.append(A(4,0x34,"media-col-default",encode_collection(size)))
        attrs.append(A(4,0x34,"media-col-database",encode_collection(size)))
        return attrs

    def _one(self, req, name, default=None):
        values = req.values(name)
        if len(values)>1: raise IppFailure(0x0400,"duplicate "+name)
        return values[0] if values else default

    def _validate(self, req):
        mime = self._one(req,"document-format",b"image/jpeg").decode("ascii")
        if mime not in FORMATS: raise IppFailure(0x040a,"document format not implemented")
        if self._one(req,"media",MEDIA) != MEDIA: raise IppFailure(0x040b,"six-inch media only")
        if self._one(req,"copies",integer(1)) != integer(1): raise IppFailure(0x040b,"test profile allows one copy")
        if self._one(req,"sides",b"one-sided") != b"one-sided": raise IppFailure(0x040b,"one-sided only")
        if self._one(req,"compression",b"none") != b"none": raise IppFailure(0x040b,"compression not implemented")
        orientation=self._one(req,"orientation-requested",integer(3))
        if orientation not in tuple(integer(x) for x in (3,4,5,6)):
            raise IppFailure(0x040b,"unsupported orientation")
        if self._one(req,"print-quality",integer(5)) not in (integer(4),integer(5)):raise IppFailure(0x040b,"unsupported quality; high quality is default")
        if self._one(req,"printer-resolution",integer(300)+integer(300)+b"\x03") != integer(300)+integer(300)+b"\x03":raise IppFailure(0x040b,"300 dpi client raster only")
        if self._one(req,"number-up",integer(1)) != integer(1): raise IppFailure(0x040b,"number-up not implemented")
        if self._one(req,"job-hold-until",b"no-hold") != b"no-hold": raise IppFailure(0x040b,"job holding not implemented")
        if self._one(req,"print-color-mode",b"color") != b"color": raise IppFailure(0x040b,"color profile only")
        scaling = self._one(req,"print-scaling",b"fill").decode("ascii")
        if scaling not in ("fit","fill"): raise IppFailure(0x040b,"only implemented fit/fill scaling supported")
        for name in ('x-image-position','y-image-position'):
            if self._one(req,name,b'center')!=b'center':raise IppFailure(0x040b,'only centered placement implemented')
        if self._one(req,'media-type',b'photographic')!=b'photographic':raise IppFailure(0x040b,'photographic paper only')
        media_col = self._one(req,"media-col")
        if media_col is not None:
            col = collection_values(media_col)
            if set(col)-{"media-size","media-type","media-left-margin","media-right-margin","media-top-margin","media-bottom-margin"}:
                raise IppFailure(0x040b,"unsupported media collection member")
            if col.get('media-type',(0x44,b'photographic'))!=(0x44,b'photographic'):raise IppFailure(0x040b,'photographic paper only')
            size = col.get("media-size")
            if size != (0x34,{"x-dimension":(0x21,integer(10160)),"y-dimension":(0x21,integer(15240))}):
                raise IppFailure(0x040b,"six-inch media size required")
            for name,(tag,val) in col.items():
                if name.endswith("margin") and (tag != 0x21 or val != integer(0)):
                    raise IppFailure(0x040b,"nonzero requested margins not implemented")
        if self.transport_factory is None: raise IppFailure(0x0502,"USB backend not enabled")
        return mime, scaling, orientation in (integer(5),integer(6))

    def job_attributes(self):
        if not self.session or not self.session.job: raise IppFailure(0x0406,"job not found")
        job = self.session.job
        state = ipp_job_state(job.device_state) if job.device_state is not None else 5
        if job.phase == "uncertain": state = 6
        return [A(2,0x21,"job-id",integer(self.job_id)),
                A(2,0x45,"job-uri",(self.uri+"/jobs/"+str(self.job_id)).encode()),
                A(2,0x23,"job-state",integer(state)),
                A(2,0x44,"job-state-reasons",b"job-completed-successfully" if state==9 else b"none"),
                A(2,0x41,"job-state-message",self.state_message.encode()[:512])]

    def handle(self, raw):
        req = decode_ipp(raw)
        opattrs = [A(1,0x47,"attributes-charset",b"utf-8"), A(1,0x48,"attributes-natural-language",b"en")]
        status, attrs = 0, []
        with self.lock:
            try:
                if req.code == 11:
                    attrs = self.printer_attributes()
                elif req.code == 4:
                    self._validate(req)
                elif req.code == 2:
                    mime,scaling,reverse = self._validate(req)
                    durable_key=sha256(raw).hexdigest()
                    old=self.journal.get(durable_key)
                    if old:
                        if old.get('response'):return base64.b64decode(old['response'])
                        raise IppFailure(0x0507,"persisted prior attempt; do not replay")
                    key = (req.request_id,sha256(raw).digest())
                    if key == self.last_key and time.monotonic()-self.last_time < 60:
                        return self.last_response
                    if self.blocked or self.session and self.session.job and self.session.job.phase not in ("completed","canceled","aborted"):
                        raise IppFailure(0x0507,"one active job allowed")
                    if self.isolated_render:
                        try:
                            run=subprocess.run([sys.executable,str(Path(__file__).with_name('render_worker.py'))],
                                input=json.dumps({'mime':mime,'scaling':scaling,'reverse':reverse}).encode()+b'\n'+req.document,
                                capture_output=True,timeout=30,check=True)
                            prepared=PreparedImage(run.stdout,0)
                        except (subprocess.SubprocessError,OSError) as exc:
                            raise IppFailure(0x040b,"single-page renderer rejected document or exceeded limit") from exc
                    else:
                        prepared = prepare_document(req.document,mime,scaling=scaling,rotate_180=reverse)
                    if self.session and hasattr(self.session.transport,"close"):
                        self.session.transport.close()
                    try:
                        transport = self.transport_factory()
                    except OSError as exc:
                        raise IppFailure(0x0502,"cannot open USB backend") from exc
                    self.session = PhotoSession(transport)
                    self.job_id += 1
                    self.blocked = True
                    self.state_message = "Creating device job"
                    self.journal[durable_key]={'phase':'creating','job_id':self.job_id}
                    self._persist()
                    try:
                        self.session.create(prepared)
                        self.journal[durable_key]['device_job_id']=self.session.job.device_id
                        self._persist()
                        self.session.upload()
                        self.state_message = "Upload acknowledged; waiting for device completion"
                    except (ProtocolError,OSError) as exc:
                        self.state_message = str(exc)
                        if self.session.job:
                            self.session.job.phase = "uncertain"
                        else:
                            # Retain the attempted session to prevent unsafe replay.
                            raise IppFailure(0x0502,"creation outcome uncertain; do not resubmit automatically") from exc
                    attrs = self.job_attributes()
                    self.last_key,self.last_time = key,time.monotonic()
                elif req.code in (8,9,10):
                    if req.code != 10 and self._one(req,"job-id",integer(self.job_id)) != integer(self.job_id):
                        raise IppFailure(0x0406,"job not found")
                    if not self.session or not self.session.job:
                        if req.code == 10: attrs = []
                        else: raise IppFailure(0x0406,"job not found")
                    else:
                        try:
                            if req.code == 8: self.session.cancel()
                            self.session.poll()
                            self.state_message = "Device state: "+self.session.job.device_state.name
                            if self.session.job.phase in ("completed","canceled","aborted"):
                                self.blocked = False
                            for entry in self.journal.values():
                                if entry.get('device_job_id')==self.session.job.device_id:
                                    entry['phase']=self.session.job.phase
                                    if entry.get('response'):
                                        cached=decode_ipp(base64.b64decode(entry['response']))
                                        entry['response']=base64.b64encode(encode_ipp(Message(cached.version,cached.code,cached.request_id,
                                            tuple(opattrs+self.job_attributes())))).decode()
                            self._persist()
                        except ProtocolError as exc:
                            self.state_message = str(exc)
                            if req.code == 8:
                                raise IppFailure(0x0500,"device cancellation not confirmed") from exc
                        attrs = self.job_attributes()
                else:
                    raise IppFailure(0x0501,"operation not implemented")
            except IppFailure as exc:
                status = exc.status
                opattrs.append(A(1,0x41,"status-message",exc.message.encode()))
            except (Unsupported,UnicodeDecodeError,ValueError) as exc:
                status = 0x040b
                opattrs.append(A(1,0x41,"status-message",str(exc).encode()[:512]))
            response = encode_ipp(Message(req.version,status,req.request_id,tuple(opattrs+attrs)))
            if req.code == 2 and status == 0:
                self.last_response = response
                entry=self.journal.get(sha256(raw).hexdigest())
                if entry is not None:
                    entry['response']=base64.b64encode(response).decode()
                    entry['phase']=self.session.job.phase
                    self._persist()
            return response

    def _persist(self):
        if self.state_path:
            self.state_path.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.state_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.journal,indent=2)+'\n')
            tmp.replace(self.state_path)

    def refresh(self):
        if self.session and self.session.job and self.session.job.phase not in ('completed','aborted','canceled'):
            attrs=(A(1,0x47,'attributes-charset',b'utf-8'),A(1,0x48,'attributes-natural-language',b'en'),
                   A(1,0x21,'job-id',integer(self.job_id)))
            self.handle(encode_ipp(Message((2,0),9,700000,attrs)))

    def close(self):
        with self.lock:
            if self.session and hasattr(self.session.transport,"close"):
                self.session.transport.close()


def make_app(core=None, *, allowed_networks=None):
    """Construct routes only; never binds sockets or registers Bonjour."""
    from aiohttp import web
    import asyncio
    core = core or BridgeCore()
    gate=asyncio.Semaphore(1)
    from ipp_trace import record,describe,stack
    @web.middleware
    async def trace(request,handler):
        event={'method':request.method,'route':request.path if request.path in ('/ipp/print','/ipp/print/') or request.path.startswith('/ipp/print/jobs/') and request.path.rsplit('/',1)[-1].isdigit() else '<unmatched-path>','content_type':request.content_type,
               'content_length':request.content_length,'expect_continue':request.headers.get('Expect','').lower()=='100-continue',
               'chunked':'chunked' in request.headers.get('Transfer-Encoding','').lower()}
        request['diagnostic_event']=event
        try:
            response=await handler(request);event['http_status']=response.status;return response
        except Exception as exc:
            event['http_status']=getattr(exc,'status',500);event['exception']=stack(exc);raise
        finally:record(core,event)
    async def post(request):
        if allowed_networks:
            try:
                peer=ipaddress.ip_address(request.remote)
                if not peer.is_loopback and not any(peer in ipaddress.ip_network(n) for n in allowed_networks):
                    return web.Response(status=403,text="local network only")
            except ValueError:return web.Response(status=403)
        if gate.locked() and (request.content_length is None or request.content_length>65536):
            return web.Response(status=503,text="document processing busy; retry later")
        if request.content_type != "application/ipp":
            return web.Response(status=415,text="application/ipp required")
        try:
            raw = await asyncio.wait_for(request.read(),timeout=15)
            request['diagnostic_event']['ipp_request']=describe(raw)
            operation=decode_ipp(raw).code
            if operation in (2,6):
                async with gate:response=await asyncio.to_thread(core.handle,raw)
            else:response=await asyncio.to_thread(core.handle,raw)
            parsed=decode_ipp(response)
            request['diagnostic_event']['ipp_status']=parsed.code
            messages=[x.decode('utf-8',errors='replace') for x in parsed.values('status-message')]
            safe_reasons=('operation not supported','unsupported scaling','only implemented fit/fill scaling supported','six-inch media only','document format not implemented','single-page renderer rejected document','normal quality only','one final document required','document already supplied or task closed')
            request['diagnostic_event']['validation_reason']=[m if m in safe_reasons else '<server validation message omitted>' for m in messages]
            return web.Response(body=response,content_type="application/ipp")
        except Unsupported:
            return web.Response(status=400,text="invalid IPP request")
    app = web.Application(client_max_size=32*1024*1024,middlewares=[trace])
    app.router.add_post("/ipp/print",post)
    app.router.add_post("/ipp/print/",post)
    app.router.add_post("/ipp/print/jobs/{job_id}",post)
    async def poller(app):
        async def loop():
            while True:
                await asyncio.sleep(5)
                await asyncio.to_thread(core.refresh)
        app['poller']=asyncio.create_task(loop())
    async def cleanup(app):
        app['poller'].cancel()
        try:await app['poller']
        except asyncio.CancelledError:pass
        core.close()
    app.on_startup.append(poller)
    app.on_cleanup.append(cleanup)
    return app
