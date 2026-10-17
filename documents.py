"""Offline document conversion. Loading this renderer never opens USB/network."""
import ctypes as c
from pathlib import Path
from offline import prepare_jpeg, prepare_rgb, Unsupported

FORMATS = ("image/jpeg", "application/pdf", "image/urf", "image/pwg-raster")


def prepare_document(data, mime, *, scaling="fill", rotate_180=False, centering=(0.5,0.5)):
    if mime == "image/jpeg":
        return prepare_jpeg(data, scaling=scaling, rotate_180=rotate_180, centering=centering)
    if mime not in FORMATS:
        raise Unsupported("unsupported document format")
    lib = c.CDLL(str(Path(__file__).with_name("libmijia_render.dylib")))
    lib.mijia_render.argtypes = [c.c_void_p, c.c_size_t, c.c_char_p, c.POINTER(c.c_void_p),
                               c.POINTER(c.c_uint32), c.POINTER(c.c_uint32), c.c_void_p, c.c_size_t]
    lib.mijia_render.restype = c.c_int
    lib.mijia_render_free.argtypes = [c.c_void_p]
    lib.mijia_render_free.restype = None
    buffer, err = c.create_string_buffer(data), c.create_string_buffer(256)
    output, width, height = c.c_void_p(), c.c_uint32(), c.c_uint32()
    ok = lib.mijia_render(buffer, len(data), mime.encode(), c.byref(output),
                          c.byref(width), c.byref(height), err, len(err))
    if not ok: raise Unsupported(err.value.decode())
    try:
        rgb = c.string_at(output, width.value*height.value*3)
        # PDF renderer preserves page aspect; the requested fit/fill is applied here.
        return prepare_rgb(rgb, width.value, height.value,
                           scaling=scaling, rotate_180=rotate_180, centering=centering)
    finally:
        lib.mijia_render_free(output)
