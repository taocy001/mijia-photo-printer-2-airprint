from io import BytesIO
import json
import unittest
from PIL import Image
from offline import Attribute as A, Message, encode_ipp, decode_ipp, encode_collection, collection_values
from bridge import BridgeCore, integer, MEDIA, make_app


class SimulatedPrinter:
    """Synthetic protocol peer; never uses hardware."""
    def __init__(self): self.writes,self.responses,self.polls = [],[],0
    def write(self,data,timeout_ms):
        self.writes.append(data)
        if data.startswith(b'cmd json\n'):
            command = json.loads(data[9:])
            if command['method']=='print-job': result={'job_id':7,'error-code':0}
            elif command['method']=='get-job-info':
                self.polls+=1; result=[{'job-id':7,'job-state':3 if self.polls==1 else 9}]
            else: result={'error-code':0}
            self.responses.append(json.dumps({'id':command['id'],'result':result}).encode())
        else:
            line = data.split(b'\n',1)[0]
            self.responses.append(line+b'\n')
        return len(data)
    def read(self,max_bytes,timeout_ms): return self.responses.pop(0)


def request(op,rid=1,attrs=(),data=b''):
    base=(A(1,0x47,'attributes-charset',b'utf-8'),A(1,0x48,'attributes-natural-language',b'en'))
    return encode_ipp(Message((2,0),op,rid,base+tuple(attrs),data))


def jpeg():
    b=BytesIO();Image.new('RGB',(800,1200),'white').save(b,'JPEG');return b.getvalue()


class BridgeTests(unittest.TestCase):
    def test_default_core_rejects_jobs_and_advertises_disconnected(self):
        core=BridgeCore()
        attrs=decode_ipp(core.handle(request(11)))
        self.assertEqual(attrs.values('printer-is-accepting-jobs'),[b'\0'])
        self.assertNotIn(b'two-sided-long-edge',attrs.values('sides-supported'))
        self.assertEqual(decode_ipp(core.handle(request(2,data=jpeg()))).code,0x0502)

    def test_collection_wire_roundtrip_and_rejection_of_wrong_size(self):
        col={'media-size':(0x34,{'x-dimension':(0x21,integer(10160)),
                                'y-dimension':(0x21,integer(15240))})}
        wire=request(4,attrs=(A(2,0x34,'media-col',encode_collection(col)),))
        parsed=decode_ipp(wire)
        self.assertEqual(collection_values(parsed.values('media-col')[0]),col)
        self.assertEqual(encode_ipp(parsed),wire)
        core=BridgeCore(transport_factory=SimulatedPrinter)
        self.assertEqual(decode_ipp(core.handle(wire)).code,0)
        col['media-size'][1]['x-dimension']=(0x21,integer(21000))
        bad=request(4,attrs=(A(2,0x34,'media-col',encode_collection(col)),))
        self.assertEqual(decode_ipp(core.handle(bad)).code,0x040b)

    def test_ipp_to_usb_model_upload_status_and_retry_deduplication(self):
        printer=SimulatedPrinter();core=BridgeCore(transport_factory=lambda:printer)
        raw=request(2,42,(A(1,0x49,'document-format',b'image/jpeg'),),jpeg())
        reply=decode_ipp(core.handle(raw))
        self.assertEqual(reply.code,0)
        self.assertEqual(reply.values('job-state'),[integer(5)])
        writes=len(printer.writes)
        self.assertEqual(core.handle(raw),encode_ipp(reply))
        self.assertEqual(len(printer.writes),writes)
        jobid=A(1,0x21,'job-id',integer(1))
        state1=decode_ipp(core.handle(request(9,43,(jobid,))))
        state2=decode_ipp(core.handle(request(9,44,(jobid,))))
        self.assertEqual(state1.values('job-state'),[integer(5)])
        self.assertEqual(state2.values('job-state'),[integer(9)])
        self.assertFalse(core.blocked)

    def test_one_copy_and_single_page_profile(self):
        core=BridgeCore(transport_factory=SimulatedPrinter)
        raw=request(4,attrs=(A(2,0x21,'copies',integer(2)),))
        self.assertEqual(decode_ipp(core.handle(raw)).code,0x040b)

    def test_uncertain_creation_blocks_all_replays(self):
        class LostReply(SimulatedPrinter):
            def read(self,max_bytes,timeout_ms): raise TimeoutError('lost')
        printer=LostReply();core=BridgeCore(transport_factory=lambda:printer)
        raw=request(2,attrs=(A(1,0x49,'document-format',b'image/jpeg'),),data=jpeg())
        self.assertEqual(decode_ipp(core.handle(raw)).code,0x0502)
        self.assertTrue(core.blocked)
        self.assertEqual(decode_ipp(core.handle(raw)).code,0x0507)
        self.assertEqual(len(printer.writes),1)

    def test_rejected_cancellation_is_not_reported_successful(self):
        class RejectCancel(SimulatedPrinter):
            def write(self,data,timeout_ms):
                count=super().write(data,timeout_ms)
                if data.startswith(b'cmd json\n'):
                    cmd=json.loads(data[9:])
                    if cmd['method']=='cancel-job':
                        self.responses[-1]=json.dumps({'id':cmd['id'],'result':{'error-code':-1}}).encode()
                return count
        printer=RejectCancel();core=BridgeCore(transport_factory=lambda:printer)
        core.handle(request(2,data=jpeg()))
        cancel=decode_ipp(core.handle(request(8,2,(A(1,0x21,'job-id',integer(1)),))))
        self.assertEqual(cancel.code,0x0500)

    def test_app_factory_does_not_open_transport_or_network(self):
        calls=[]
        def factory(): calls.append(1); raise AssertionError('must not open')
        app=make_app(BridgeCore(transport_factory=factory))
        self.assertFalse(calls)
        self.assertTrue(list(app.router.routes()))


if __name__=='__main__':unittest.main()
