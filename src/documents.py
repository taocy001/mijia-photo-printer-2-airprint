"""Offline document conversion. Loading this renderer never opens USB/network."""
import ctypes as c
from io import BytesIO
from pathlib import Path
from PIL import Image
from offline import prepare_jpeg, prepare_rgb, compose_splice, load_jpeg_pil, Unsupported

FORMATS = ("image/jpeg", "application/pdf", "image/urf", "image/pwg-raster")


def _render_lib():
    lib = c.CDLL(str(Path(__file__).resolve().parents[1]/"build"/"libmijia_render.dylib"))
    lib.mijia_render.argtypes = [c.c_void_p, c.c_size_t, c.c_char_p, c.POINTER(c.c_void_p),
                               c.POINTER(c.c_uint32), c.POINTER(c.c_uint32), c.c_void_p, c.c_size_t]
    lib.mijia_render.restype = c.c_int
    lib.mijia_render_free.argtypes = [c.c_void_p]
    lib.mijia_render_free.restype = None
    lib.mijia_pdf_photo_bounds.argtypes = [c.c_void_p, c.c_size_t]
    lib.mijia_pdf_photo_bounds.restype = c.c_int
    return lib


def _pdf_is_photo(lib, data):
    return bool(lib.mijia_pdf_photo_bounds(c.create_string_buffer(data), len(data)))


def _native_render(lib, data, native_mime):
    """Render one document/page to an sRGB pixel buffer; returns (rgb, width, height)."""
    buffer, err = c.create_string_buffer(data), c.create_string_buffer(256)
    output, width, height = c.c_void_p(), c.c_uint32(), c.c_uint32()
    ok = lib.mijia_render(buffer, len(data), native_mime.encode(), c.byref(output),
                          c.byref(width), c.byref(height), err, len(err))
    if not ok: raise Unsupported(err.value.decode())
    try:
        return c.string_at(output, width.value*height.value*3), width.value, height.value
    finally:
        lib.mijia_render_free(output)


def pdf_page_images(data, max_pages=2):
    """拆分多页 PDF (iOS 多选照片打印 = 每页一张照片), 每页经原生渲染器转为 PIL 图.

    照片页用 photo-pdf 模式整页铺满渲染; 普通页按页面光栅化. 仅取前 max_pages 页."""
    from pypdf import PdfReader, PdfWriter
    lib = _render_lib()
    imgs = []
    for page in list(PdfReader(BytesIO(data)).pages)[:max_pages]:
        writer = PdfWriter()
        writer.add_page(page)
        buf = BytesIO()
        writer.write(buf)
        pdf1 = buf.getvalue()
        native_mime = 'application/x-mijia-photo-pdf' if _pdf_is_photo(lib, pdf1) else 'application/pdf'
        rgb, w, h = _native_render(lib, pdf1, native_mime)
        imgs.append(Image.frombytes('RGB', (w, h), rgb))
    return imgs


def prepare_splice_document(data, mime, *, scaling="fill"):
    """5014 拼接版排版: 整版 1216x1824, 上下两半(1216x912)各一张.

    JPEG 单张 -> 两半相同; iOS 多选照片的 PDF 前两页 -> 上/下各一; 单页 PDF 同单张.
    URF/PWG 等不支持拆页的格式抛 Unsupported, 由调用方回退普通整版."""
    if mime == "image/jpeg":
        return compose_splice([load_jpeg_pil(data)], scaling=scaling)
    if mime == "application/pdf":
        try:
            pages = pdf_page_images(data, 2)
        except Unsupported:
            raise
        except Exception as e:   # 畸形 PDF / 拆页失败: 按不支持处理, 回退普通整版
            raise Unsupported(f"pdf split failed: {e}") from e
        return compose_splice(pages, scaling=scaling)
    raise Unsupported("splice supports JPEG/PDF only")


def prepare_document(data, mime, *, scaling="fill", rotate_180=False, centering=(0.5,0.5), orientation=None):
    if mime == "image/jpeg":
        return prepare_jpeg(data, scaling=scaling, rotate_180=rotate_180, centering=centering, orientation=orientation)
    if mime not in FORMATS:
        raise Unsupported("unsupported document format")
    lib = _render_lib()
    photo = mime == 'application/pdf' and scaling == 'fill' and _pdf_is_photo(lib, data)
    native_mime = 'application/x-mijia-photo-pdf' if photo else mime
    if mime == 'application/pdf' and not photo: scaling = 'fit'
    rgb, w, h = _native_render(lib, data, native_mime)
    # PDF renderer preserves page aspect; the requested fit/fill is applied here.
    return prepare_rgb(rgb, w, h, scaling=scaling, rotate_180=rotate_180, centering=centering)
