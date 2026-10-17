"""Statically reconstructed Syrup/302c:3008 protocol, with injected transport.

No device access occurs on import. This module does not open a device or listen
on a network. Tests use scripted byte fixtures, never a physical printer.
"""
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
import json
import re
import time
from typing import Protocol
from offline import PreparedImage, job_model, encode_inner_upload_chunk


class ProtocolError(RuntimeError):
    pass


class UncertainOutcome(ProtocolError):
    """A write may have reached hardware: never automatically replay a job."""


class Transport(Protocol):
    def write(self, data: bytes, timeout_ms: int) -> int: ...
    def read(self, max_bytes: int, timeout_ms: int) -> bytes: ...


class DeviceJobState(IntEnum):
    NONE = 0
    WAITING = 1
    START = 2
    PROCESSING = 3
    PROCESSING_HELD = 4
    PENDING = 5
    TERMINATING = 6
    ABORTED = 7
    CANCELED = 8
    COMPLETED = 9


def ipp_job_state(state: DeviceJobState) -> int:
    # Vendor 7/8 meanings are reversed relative to IPP canceled/aborted.
    return {DeviceJobState.ABORTED: 8, DeviceJobState.CANCELED: 7,
            DeviceJobState.COMPLETED: 9,
            DeviceJobState.PROCESSING_HELD: 6,
            DeviceJobState.WAITING: 3, DeviceJobState.PENDING: 3,
            DeviceJobState.NONE: 4}.get(state, 5)


def encode_command(request_id: int, method: str, params: dict, *, new_spec=False) -> bytes:
    if not 1 <= request_id < 1_000_000_000:
        raise ProtocolError("invalid request id")
    if method not in ("print-job", "get-job-info", "cancel-job", "get-job-id-list", "get_prop"):
        raise ProtocolError("command is outside reconstructed photo path")
    body = {"id": request_id, "method": method, "params": params}
    if new_spec:
        body["isNewSpec"] = 1
    # The original library is Hjson (confirmed by RTTI). We emit standard compact
    # JSON, not byte-identical Hjson serialization. BIDIToPrinter prepends this
    # 9-byte prefix for WINUSB; the direct branch sends strlen, without NUL.
    return b"cmd json\n"+json.dumps(body, separators=(",", ":"), sort_keys=True,
                                    ensure_ascii=False).encode("utf-8")


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


class ReplyStream:
    """Handles complete/fragmented/adjacent raw JSON and data ACK lines.

    The native code handles adjacent JSON via a regex and indexes replies by
    id. Our decoder preserves that intent without splitting braces in strings.
    Fragment assembly is defensive client behavior, not a device guarantee.
    """
    def __init__(self):
        self.buffer = bytearray()
        self.json_replies = deque()
        self.acks = deque()
        self.events = deque(maxlen=64)
        self.decoder = json.JSONDecoder(object_pairs_hook=reject_duplicate_keys)

    def feed(self, data: bytes):
        self.buffer.extend(data)
        if len(self.buffer) > 65536:
            raise ProtocolError("response buffer limit exceeded")
        while self.buffer:
            # Some native paths provide NUL padding; whitespace/NUL is ignored.
            while self.buffer and self.buffer[0] in b" \t\r\n\0":
                del self.buffer[0]
            if not self.buffer:
                return
            prefix = b"cmd data EXTLEN="
            if self.buffer.startswith(prefix):
                end = self.buffer.find(b"\n")
                if end < 0:
                    if len(self.buffer) > 256:
                        raise ProtocolError("unterminated data ACK")
                    return
                number = bytes(self.buffer[len(prefix):end])
                match = re.fullmatch(rb"([0-9]{1,10})(?: OK)?\r?", number)
                if not match:
                    raise ProtocolError("invalid data ACK length/status")
                self.acks.append(int(match.group(1)))
                del self.buffer[:end+1]
                continue
            if prefix.startswith(self.buffer):
                return
            if self.buffer[0] != ord("{"):
                raise ProtocolError("unexpected response prefix")
            try:
                text = self.buffer.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    return
                raise ProtocolError("invalid response UTF-8") from exc
            try:
                obj, end = self.decoder.raw_decode(text)
            except json.JSONDecodeError:
                return  # bounded by buffer limit and caller deadline
            if not isinstance(obj, dict):
                raise ProtocolError("response must be a JSON object")
            consumed = len(text[:end].encode("utf-8"))
            del self.buffer[:consumed]
            if str(obj.get("method", "")).startswith("event."):
                self.events.append(obj)
            else:
                if type(obj.get("id")) is not int:
                    raise ProtocolError("response is missing integer id")
                self.json_replies.append(obj)
            if len(self.json_replies) > 64 or len(self.acks) > 64:
                raise ProtocolError("unmatched response limit exceeded")


@dataclass
class Job:
    device_id: int
    phase: str = "created"
    uploaded_bytes: int = 0
    device_state: DeviceJobState | None = None


class PhotoSession:
    def __init__(self, transport: Transport, *, timeout_ms=5000):
        self.transport = transport
        self.timeout_ms = timeout_ms
        self.sequence = 100
        self.stream = ReplyStream()
        self.job: Job | None = None
        self.creation_attempted = False

    def _write_once(self, data):
        try:
            count = self.transport.write(data, self.timeout_ms)
        except Exception as exc:
            raise UncertainOutcome("write failed; may have reached device, no automatic replay") from exc
        if count != len(data):
            raise UncertainOutcome("short write; no automatic replay")

    def _receive_until(self, predicate):
        deadline = time.monotonic()+self.timeout_ms/1000
        while True:
            result = predicate()
            if result is not None:
                return result
            remaining = int((deadline-time.monotonic())*1000)
            if remaining <= 0:
                raise UncertainOutcome("response timeout; no automatic replay")
            try:
                data = self.transport.read(16384, remaining)
            except Exception as exc:
                raise UncertainOutcome("response unavailable after write") from exc
            if not data:
                raise UncertainOutcome("empty response after write")
            self.stream.feed(data)

    def command(self, method, params, *, new_spec=False):
        self.sequence += 1
        request_id = self.sequence
        self._write_once(encode_command(request_id, method, params, new_spec=new_spec))
        def matching():
            for i, response in enumerate(self.stream.json_replies):
                if response["id"] == request_id:
                    del self.stream.json_replies[i]
                    return response
            return None
        response = self._receive_until(matching)
        if "error" in response:
            raise ProtocolError(f"device returned error: {response['error']!r}")
        if "result" not in response:
            raise ProtocolError("response is missing result")
        return response["result"]

    def create(self, image: PreparedImage, *, copies=1):
        if self.creation_attempted:
            raise ProtocolError("session already has a job; no automatic recreation")
        model = job_model(image, copies=copies)
        self.creation_attempted = True
        result = self.command("print-job", model["params"])
        if not isinstance(result, dict):
            raise ProtocolError("invalid print-job result")
        # Physical success may omit error-code; original managed int defaults 0.
        code = result.get("error-code", 0)
        if type(code) is not int:
            raise ProtocolError("invalid print-job error-code")
        if code != 0:
            raise ProtocolError(f"print-job rejected: {code}")
        jid = result.get("job_id")  # underscore confirmed by JsonPropertyName
        if type(jid) is not int or not 1 <= jid <= 0x7fffffff:
            raise ProtocolError("invalid job_id")
        self.job = Job(jid)
        self._document = image.jpeg
        return self.job

    def upload(self):
        if self.job is None or self.job.phase != "created":
            raise ProtocolError("upload requires a freshly created job")
        self.job.phase = "uploading"
        try:
            for start in range(0, len(self._document), 8192):
                payload = self._document[start:start+8192]
                if self.stream.acks:
                    raise ProtocolError("unsolicited ACK before chunk")
                self._write_once(encode_inner_upload_chunk(payload, self.job.device_id))
                ack = self._receive_until(lambda: self.stream.acks.popleft() if self.stream.acks else None)
                if ack != len(payload)+4:
                    raise ProtocolError("data ACK length mismatch")
                self.job.uploaded_bytes += len(payload)
            self.job.phase = "uploaded"  # NEVER equivalent to printed
        except Exception:
            self.job.phase = "uncertain"
            raise

    def poll(self):
        if self.job is None:
            raise ProtocolError("no job")
        result = self.command("get-job-info", {"job-id": self.job.device_id}, new_spec=True)
        if not isinstance(result, list):
            raise ProtocolError("get-job-info result must be a list")
        matches = [r for r in result if isinstance(r, dict) and type(r.get("job-id")) is int
                   and r.get("job-id") == self.job.device_id]
        if len(matches) != 1:
            raise ProtocolError("matching job absent or duplicated; not proof of completion")
        try:
            if type(matches[0].get("job-state")) is not int:
                raise ValueError("integer job state required")
            state = DeviceJobState(matches[0]["job-state"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ProtocolError("unknown device job state") from exc
        self.job.device_state = state
        if state in (DeviceJobState.COMPLETED, DeviceJobState.CANCELED, DeviceJobState.ABORTED):
            self.job.phase = state.name.lower()
        return matches[0]

    def query_jobs(self):
        """Read-only list query. Empty is NOT evidence of idle/safe-to-create.

        Physical 2026-10-17 capture: params={} returned [] while explicit
        job-id=67 returned Processing. Check known IDs/history separately.
        """
        return self.command("get-job-info", {}, new_spec=True)

    def cancel(self):
        if self.job is None:
            raise ProtocolError("no job")
        # Sending cancel does not prove cancellation; the next poll must confirm.
        result = self.command("cancel-job", {"job-id": self.job.device_id})
        if not isinstance(result, dict) or type(result.get("error-code")) is not int or result.get("error-code") != 0:
            raise ProtocolError("cancel-job was not acknowledged successfully")
        return result
