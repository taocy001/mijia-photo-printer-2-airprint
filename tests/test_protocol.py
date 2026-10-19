"""Synthetic replies are fixtures, not captures from a real printer."""
from collections import deque
import json
import unittest
from io import BytesIO
from PIL import Image
from offline import prepare_jpeg
from protocol import (ReplyStream, PhotoSession, ProtocolError, UncertainOutcome,
                      encode_command, DeviceJobState, ipp_job_state)


class ScriptedTransport:
    def __init__(self, reads=(), short=False):
        self.reads = deque(reads)
        self.writes = []
        self.short = short

    def write(self, data, timeout_ms):
        self.writes.append(data)
        return len(data)-1 if self.short else len(data)

    def read(self, max_bytes, timeout_ms):
        if not self.reads:
            raise TimeoutError("fixture exhausted")
        return self.reads.popleft()


def prepared():
    buf = BytesIO(); Image.new("RGB", (1216, 1824), "white").save(buf, "JPEG")
    return prepare_jpeg(buf.getvalue())


class ProtocolTests(unittest.TestCase):
    def test_command_bytes_prefix_order_no_nul_or_suffix(self):
        self.assertEqual(encode_command(101, "get-job-info", {"job-id": 7}, new_spec=True),
                         b'cmd json\n{"id":101,"isNewSpec":1,"method":"get-job-info","params":{"job-id":7}}')

    def test_physical_success_reply_omits_error_code(self):
        transport = ScriptedTransport([b'{"result":{"job_id":79},"id":101}'])
        session = PhotoSession(transport)
        self.assertEqual(session.create(prepared()).device_id, 79)
        self.assertEqual(len(transport.writes), 1)
        self.assertEqual(session.job.uploaded_bytes, 0)

    def test_omitted_code_requires_valid_job_and_rejects_explicit_error(self):
        for result in [{}, {"job_id":0}, {"job_id":79,"error-code":"0"},
                       {"job_id":79,"error-code":8006}]:
            transport=ScriptedTransport([json.dumps({"id":101,"result":result}).encode()])
            session=PhotoSession(transport)
            with self.assertRaises(ProtocolError):session.create(prepared())
            self.assertEqual(len(transport.writes),1)

    def test_captured_creation_reply_and_terminal_abort(self):
        from pathlib import Path
        # Sanitized protocol-only fixture; contains no image, serial, host or user.
        captured=b'{"result":{"job_id":79},"id":101}'
        transport=ScriptedTransport([captured,b'{"id":102,"result":[{"job-id":79,"job-state":7,"transfer-size":0}]}'])
        session=PhotoSession(transport)
        self.assertEqual(session.create(prepared()).device_id,79)
        session.poll()
        self.assertEqual(session.job.phase,'aborted')
        self.assertEqual(session.job.uploaded_bytes,0)

    def test_fragmented_ack_and_event_during_upload(self):
        image=prepared();chunks=[image.jpeg[i:i+8192] for i in range(0,len(image.jpeg),8192)]
        replies=[b'{"id":101,"result":{"job_id":80}}']
        for chunk in chunks:
            ack=b'cmd data EXTLEN='+str(len(chunk)+4).encode()+b'\n'
            replies += [b'{"method":"event.progress","params":{"code":0}}'+ack[:12],ack[12:]]
        replies += [b'{"id":102,"result":[{"job-id":80,"job-state":9}]}']
        t=ScriptedTransport(replies);session=PhotoSession(t);session.create(image);session.upload()
        self.assertEqual(session.job.uploaded_bytes,len(image.jpeg))
        session.poll();self.assertEqual(session.job.phase,'completed')

    def test_captured_ack_ok_suffix(self):
        from pathlib import Path
        raw=b'cmd data EXTLEN=8196 OK\n'
        stream=ReplyStream()
        for chunk in [raw[:20],raw[20:]]:stream.feed(chunk)
        self.assertEqual(list(stream.acks),[8196])
        for bad in [b'cmd data EXTLEN=8196 ERROR\n',b'cmd data EXTLEN=8196 OK extra\n']:
            with self.assertRaises(ProtocolError):ReplyStream().feed(bad)

    def test_fragmented_json_utf8_and_adjacent_objects(self):
        stream = ReplyStream()
        raw = '{"id":1,"result":"照片}{"}{"id":2,"result":[]}'.encode()
        for b in raw:
            stream.feed(bytes([b]))
        self.assertEqual(list(stream.json_replies), [{"id":1,"result":"照片}{"}, {"id":2,"result":[]}])

    def test_event_and_ack_fragmentation(self):
        stream = ReplyStream()
        for chunk in [b'{"method":"event.ready"}', b'cmd data EXT', b'LEN=8196\n\0']:
            stream.feed(chunk)
        self.assertEqual(list(stream.acks), [8196])
        self.assertEqual(len(stream.events), 1)
        self.assertFalse(stream.json_replies)

    def test_invalid_ack_duplicate_key_and_unbounded_buffer_rejected(self):
        for data in [b'cmd data EXTLEN=-1\n', b'{"id":1,"id":2}', b'garbage', b'{' + b'x'*65536]:
            with self.assertRaises(ProtocolError): ReplyStream().feed(data)

    def test_create_upload_poll_never_confuses_ack_with_completion(self):
        image = prepared()
        chunks = [image.jpeg[i:i+8192] for i in range(0,len(image.jpeg),8192)]
        replies = [b'{"id":101,"result":{"job_id":7,"error-code":0}}']
        replies += [b'cmd data EXTLEN='+str(len(c)+4).encode()+b'\n' for c in chunks]
        replies += [b'{"id":102,"result":[{"job-id":7,"job-state":3}]}',
                    b'{"id":103,"result":[{"job-id":7,"job-state":9}]}']
        transport = ScriptedTransport(replies)
        session = PhotoSession(transport)
        job = session.create(image)
        self.assertIn(b'"file-type":"jpg"', transport.writes[0])
        session.upload()
        self.assertEqual(job.phase, "uploaded")
        self.assertEqual(job.uploaded_bytes, len(image.jpeg))
        self.assertIsNone(job.device_state)
        session.poll(); self.assertEqual(job.device_state, DeviceJobState.PROCESSING)
        self.assertEqual(job.phase, "uploaded")
        session.poll(); self.assertEqual(job.phase, "completed")
        self.assertFalse(any(b'confirm-job' in x for x in transport.writes))
        self.assertEqual(len(transport.writes), len(chunks)+3)

    def test_missing_create_reply_prevents_recreation(self):
        transport = ScriptedTransport()
        session = PhotoSession(transport)
        image = prepared()
        with self.assertRaises(UncertainOutcome): session.create(image)
        with self.assertRaises(ProtocolError): session.create(image)
        self.assertEqual(len(transport.writes), 1)

    def test_short_write_is_not_replayed(self):
        transport = ScriptedTransport(short=True)
        session = PhotoSession(transport)
        with self.assertRaises(UncertainOutcome): session.create(prepared())
        self.assertEqual(len(transport.writes), 1)

    def test_bad_ack_stops_upload_and_no_second_upload(self):
        transport = ScriptedTransport([b'{"id":101,"result":{"job_id":7,"error-code":0}}',
                                       b'cmd data EXTLEN=5\n'])
        session = PhotoSession(transport)
        session.create(prepared())
        with self.assertRaises(ProtocolError): session.upload()
        self.assertEqual(session.job.phase, "uncertain")
        with self.assertRaises(ProtocolError): session.upload()
        self.assertEqual(len(transport.writes), 2)

    def test_out_of_order_reply_matching_and_missing_job_not_completed(self):
        transport = ScriptedTransport([b'{"id":999,"result":[]}'+
            b'{"id":101,"result":{"job_id":7,"error-code":0}}',
            b'{"id":102,"result":[]}'])
        session = PhotoSession(transport)
        session.create(prepared())
        with self.assertRaises(ProtocolError): session.poll()
        self.assertEqual(session.job.phase, "created")
        self.assertEqual(session.stream.json_replies[0]["id"], 999)

    def test_vendor_state_mapping_to_ipp(self):
        self.assertEqual(ipp_job_state(DeviceJobState.ABORTED), 8)
        self.assertEqual(ipp_job_state(DeviceJobState.CANCELED), 7)
        self.assertEqual(ipp_job_state(DeviceJobState.COMPLETED), 9)
        self.assertNotEqual(ipp_job_state(DeviceJobState.TERMINATING), 9)


if __name__ == "__main__": unittest.main()
