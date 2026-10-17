"""Explicitly opened native USB transport. No CLI, no access on import.

Not used by any offline tests. Opening or transferring requires a separately
authorized device validation step; this source is supplied for review only.
"""
import ctypes as c
from pathlib import Path


class NativeUSBTransport:
    def __init__(self, *, serial: str, library: Path):
        self.serial = serial
        self.path = library
        self.handle = None
        self.lib = None

    def open(self):
        if self.handle:
            raise RuntimeError("already open")
        self.lib = lib = c.CDLL(str(self.path.resolve()))
        lib.mijia_usb_open.argtypes = [c.c_char_p, c.c_void_p, c.c_size_t]
        lib.mijia_usb_open.restype = c.c_void_p
        lib.mijia_usb_close.argtypes = [c.c_void_p]
        lib.mijia_usb_close.restype = None
        for name in ("write", "read"):
            function = getattr(lib, "mijia_usb_"+name)
            function.argtypes = [c.c_void_p, c.c_void_p, c.c_uint32, c.c_uint32, c.c_void_p, c.c_size_t]
            function.restype = c.c_int64
        err = c.create_string_buffer(256)
        self.handle = lib.mijia_usb_open(self.serial.encode("ascii"), err, len(err))
        if not self.handle:
            raise OSError(err.value.decode())
        return self

    def write(self, data: bytes, timeout_ms: int):
        if not self.handle: raise RuntimeError("not explicitly opened")
        err = c.create_string_buffer(256)
        buffer = c.create_string_buffer(data)
        count = self.lib.mijia_usb_write(self.handle, buffer, len(data), timeout_ms, err, len(err))
        if count < 0: raise OSError(err.value.decode())
        return count

    def read(self, max_bytes: int, timeout_ms: int):
        if not self.handle: raise RuntimeError("not explicitly opened")
        if not 1 <= max_bytes <= 65536: raise ValueError("invalid read limit")
        err, buffer = c.create_string_buffer(256), c.create_string_buffer(max_bytes)
        count = self.lib.mijia_usb_read(self.handle, buffer, max_bytes, timeout_ms, err, len(err))
        if count < 0: raise OSError(err.value.decode())
        return buffer.raw[:count]

    def close(self):
        if self.handle:
            self.lib.mijia_usb_close(self.handle)
            self.handle = None
