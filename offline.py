"""Offline preparation only: no USB, socket, queue, daemon, or discovery code.

Windows evidence fixes the canvas and job parameters; Pillow rendering is a
portable implementation, not a byte-identical ImageMagick reproduction.
"""
from dataclasses import dataclass
from io import BytesIO
import struct
from PIL import Image, ImageOps, ImageCms

CANVAS = (1216, 1824)
TARGET_BYTES = 512 * 1024
MEDIA_SIZE = 5012
CHANNEL = 0xFB00


class Unsupported(ValueError):
    pass


def encode_inner_upload_chunk(payload: bytes, job_id: int) -> bytes:
    """Static reconstruction of the buffer at hlmonitor RVA A9640.

    A96A9 adds 4 to the fread result, A9759 writes LF, A9762 writes a
    32-bit job id, A9775 copies the file bytes. A9798 passes this buffer to
    another routine. This is NOT proven to be the complete USB wire frame;
    the outer transport and reply state machine are still missing.
    """
    if not payload or len(payload) > 8192:
        raise Unsupported("inner chunk must fit the observed 8192-byte buffer")
    if not isinstance(job_id, int) or isinstance(job_id, bool) or not 1 <= job_id <= 0x7fffffff:
        raise Unsupported("job id must be a positive signed 32-bit result")
    return (b"cmd data EXTLEN="+str(len(payload)+4).encode("ascii")+b"\n"
            +struct.pack("<i", job_id)+payload)


@dataclass(frozen=True)
class PreparedImage:
    jpeg: bytes
    quality: int
    size: tuple[int, int] = CANVAS


def prepare_jpeg(data: bytes, *, scaling="fill", rotate_180=False, centering=(0.5,0.5)) -> PreparedImage:
    """EXIF orient, fit/fill portrait canvas, flatten white, JPEG quality 60..100.

    fill: centered crop; fit: preserve whole image with white bars. These are
    bridge policies for the vendor's full-frame/adaptive choices, not proof of
    its precise resampling kernel or crop anchor. 180-degree rotation is opt-in
    because the Windows caller applies it conditionally.
    """
    if scaling not in ("fill", "fit"):
        raise Unsupported("only fill and fit are implemented")
    if len(data) > 32 * 1024 * 1024:
        raise Unsupported("image exceeds offline input limit")
    try:
        src = Image.open(BytesIO(data))
    except OSError as exc:
        raise Unsupported("input is not a supported JPEG") from exc
    with src:
        if src.format != "JPEG":
            raise Unsupported("only JPEG input is implemented; PDF/URF/PWG need renderers")
        if src.width * src.height > 40_000_000:
            raise Unsupported("image exceeds pixel limit")
        image = ImageOps.exif_transpose(src).convert("RGB")
        if src.info.get("icc_profile"):
            try:
                image = ImageCms.profileToProfile(
                    image, ImageCms.ImageCmsProfile(BytesIO(src.info["icc_profile"])),
                    ImageCms.createProfile("sRGB"), outputMode="RGB")
            except (OSError, ValueError, ImageCms.PyCMSError) as exc:
                raise Unsupported("invalid or unsupported embedded ICC profile") from exc
    return prepare_rgb(image.tobytes(), image.width, image.height,
                       scaling=scaling, rotate_180=rotate_180, centering=centering)


def prepare_rgb(pixels: bytes, width: int, height: int, *, scaling="fill", rotate_180=False, centering=(0.5,0.5)):
    if scaling not in ("fill", "fit"):
        raise Unsupported("only fill and fit are implemented")
    if width < 1 or height < 1 or width*height > 40_000_000 or len(pixels) != width*height*3:
        raise Unsupported("invalid sRGB pixel buffer")
    if len(centering)!=2 or any(not 0<=v<=1 for v in centering):
        raise Unsupported("crop center must be within 0..1")
    image = Image.frombytes("RGB", (width, height), pixels)
    # Device buffer remains portrait. Rotate landscape content to use full paper.
    if width>height:
        image=image.transpose(Image.Transpose.ROTATE_90)
    if scaling == "fill":
        image = ImageOps.fit(image, CANVAS, Image.Resampling.LANCZOS, centering=centering)
    else:
        inner = ImageOps.contain(image, CANVAS, Image.Resampling.LANCZOS)
        image = Image.new("RGB", CANVAS, "white")
        image.paste(inner, ((CANVAS[0]-inner.width)//2, (CANVAS[1]-inner.height)//2))
    if rotate_180:
        image = image.transpose(Image.Transpose.ROTATE_180)

    def encode(q):
        buf = BytesIO()
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        image.save(buf, "JPEG", quality=q, progressive=False, optimize=True, icc_profile=srgb)
        return buf.getvalue()

    top = encode(100)
    if len(top) <= TARGET_BYTES:
        return PreparedImage(top, 100)
    best = encode(60)
    if len(best) > TARGET_BYTES:
        raise Unsupported("JPEG exceeds 512 KiB even at quality 60; refusing silent quality loss")
    quality, lo, hi = 60, 61, 99
    while lo <= hi:
        mid = (lo + hi)//2
        candidate = encode(mid)
        if len(candidate) <= TARGET_BYTES:
            quality, best, lo = mid, candidate, mid+1
        else:
            hi = mid-1
    return PreparedImage(best, quality)


def job_model(image: PreparedImage, *, copies=1, sharpen_mode=0, oc: bytes | None=None):
    """Confirmed logical fields ONLY. Not a serialized USB frame.

    OC bytes must come from an independently validated encoder; this module
    implements ordinary photographs without OC. Never infers OC from a JPEG.
    """
    if not isinstance(copies, int) or isinstance(copies, bool) or not 1 <= copies <= 99:
        raise Unsupported("copies outside offline policy 1..99 (not a device limit claim)")
    if oc is not None:
        raise Unsupported("OC encoder is not verified; use an ordinary photograph")
    if sharpen_mode != 0:
        raise Unsupported("only explicitly selected sharpen mode 0 is modeled")
    with Image.open(BytesIO(image.jpeg)) as check:
        if check.format != "JPEG" or check.size != CANVAS:
            raise Unsupported("prepared data is not a 1216x1824 JPEG")
    return {
        "method": "print-job",
        "params": {"copies": copies, "file-size": len(image.jpeg),
                   "job-type": 0, "file-type": "jpg", "media-size": MEDIA_SIZE,
                   "channel": CHANNEL, "sharpenmode": sharpen_mode},
        "upload-order": ["photo.jpg"],
        "usb-frame": None,
        "ready-to-send": False,
        "blockers": ["hardware acceptance of reconstructed protocol",
                     "native macOS interface opening and endpoint compatibility",
                     "physical image orientation and usable boundary",
                     "Apple client AirPrint interoperability"],
    }


# IPP subset for offline binary fixtures. No network transport is implemented.
@dataclass(frozen=True)
class Attribute:
    group: int
    tag: int
    name: str
    value: bytes


@dataclass(frozen=True)
class Message:
    version: tuple[int, int]
    code: int
    request_id: int
    attributes: tuple[Attribute, ...]
    document: bytes = b""

    def values(self, name):
        return [a.value for a in self.attributes if a.name == name]


def decode_ipp(raw: bytes) -> Message:
    if len(raw) < 9:
        raise Unsupported("truncated IPP header")
    major, minor, code, rid = struct.unpack_from(">BBHI", raw)
    if (major, minor) not in [(1, 1), (2, 0)]:
        raise Unsupported("IPP version outside implemented subset")
    pos, group, previous, attrs = 8, None, None, []
    def take(n):
        nonlocal pos
        if pos+n > len(raw):
            raise Unsupported("truncated IPP attribute")
        result = raw[pos:pos+n]
        pos += n
        return result
    def collection(depth=0):
        if depth > 4:
            raise Unsupported("collection nesting limit")
        start, have_member = pos, False
        while True:
            nested_tag = take(1)[0]
            name_len = struct.unpack(">H", take(2))[0]
            if name_len:
                raise Unsupported("collection member has nonzero name-length")
            length = struct.unpack(">H", take(2))[0]
            value = take(length)
            if nested_tag == 0x37:
                if length:
                    raise Unsupported("invalid endCollection")
                return raw[start:pos]
            if nested_tag == 0x4a:
                if not value:
                    raise Unsupported("empty collection member name")
                have_member = True
            elif not have_member or nested_tag < 0x10:
                raise Unsupported("invalid collection member")
            elif nested_tag == 0x34:
                if length:
                    raise Unsupported("invalid beginCollection")
                collection(depth+1)
    while pos < len(raw):
        tag = take(1)[0]
        if tag == 3:
            return Message((major, minor), code, rid, tuple(attrs), raw[pos:])
        if tag <= 15:
            if tag not in (1, 2, 4, 5):
                raise Unsupported("unsupported delimiter group")
            group, previous = tag, None
            continue
        if group is None or tag in (0x37, 0x4a):
            raise Unsupported("missing group or unimplemented collection syntax")
        name = take(struct.unpack(">H", take(2))[0])
        value = take(struct.unpack(">H", take(2))[0])
        if name:
            try:
                previous = name.decode("ascii")
            except UnicodeDecodeError as exc:
                raise Unsupported("invalid attribute name") from exc
        elif previous is None:
            raise Unsupported("continuation without attribute name")
        if tag == 0x34:
            if value:
                raise Unsupported("invalid collection value-length")
            value = collection()
        attrs.append(Attribute(group, tag, previous, value))
    raise Unsupported("missing end-of-attributes")


def encode_ipp(message: Message) -> bytes:
    out = bytearray(struct.pack(">BBHI", *message.version, message.code, message.request_id))
    group, previous = None, None
    for a in message.attributes:
        if a.group != group:
            out.append(a.group)
            group, previous = a.group, None
        name = b"" if a.name == previous else a.name.encode("ascii")
        out.extend(struct.pack(">BH", a.tag, len(name))+name)
        out.extend(struct.pack(">H", 0 if a.tag == 0x34 else len(a.value))+a.value)
        previous = a.name
    return bytes(out)+b"\x03"+message.document


def collection_values(data):
    """Decode validated collection bytes into named (tag, value) pairs."""
    pos, member, result = 0, None, {}
    def parse():
        nonlocal pos
        values, name = {}, None
        while True:
            tag, name_len = struct.unpack_from(">BH", data, pos); pos += 3
            if name_len: raise Unsupported("invalid collection")
            length = struct.unpack_from(">H", data, pos)[0]; pos += 2
            value = data[pos:pos+length]; pos += length
            if tag == 0x37: return values
            if tag == 0x4a:
                name = value.decode("ascii")
            else:
                if name is None or name in values: raise Unsupported("ambiguous collection")
                values[name] = (tag, parse() if tag == 0x34 else value)
    try:
        result = parse()
    except (struct.error, UnicodeDecodeError, IndexError) as exc:
        raise Unsupported("invalid collection") from exc
    if pos != len(data): raise Unsupported("trailing collection bytes")
    return result


def encode_collection(values):
    out = bytearray()
    for name, (tag, value) in values.items():
        name = name.encode("ascii")
        out.extend(struct.pack(">BHH", 0x4a, 0, len(name))+name)
        value = encode_collection(value) if tag == 0x34 else value
        out.extend(struct.pack(">BHH", tag, 0, 0 if tag == 0x34 else len(value))+value)
    return bytes(out)+b"\x37\x00\x00\x00\x00"


def offline_response(raw: bytes) -> bytes:
    """Get-Printer-Attributes is inspectable; never accepts or fabricates a job."""
    req = decode_ipp(raw)
    attrs = [Attribute(1, 0x47, "attributes-charset", b"utf-8"),
             Attribute(1, 0x48, "attributes-natural-language", b"en")]
    if req.code == 0x000b:  # Get-Printer-Attributes
        status = 0
        attrs.extend([
            Attribute(4, 0x42, "printer-name", b"Mijia offline protocol model"),
            Attribute(4, 0x49, "document-format-supported", b"image/jpeg"),
            Attribute(4, 0x22, "printer-is-accepting-jobs", b"\x00"),
            Attribute(4, 0x23, "printer-state", struct.pack(">i", 5)),
            Attribute(4, 0x44, "printer-state-reasons", b"paused"),
        ])
    elif req.code in (2, 4, 5, 6):
        status = 0x0502  # server-error-service-unavailable
        attrs.append(Attribute(1, 0x41, "status-message", b"USB protocol not validated; no job accepted"))
    else:
        status = 0x0501  # server-error-operation-not-supported
    return encode_ipp(Message(req.version, status, req.request_id, tuple(attrs)))


def prepare_ipp_fixture(raw: bytes, *, scaling="fill"):
    req = decode_ipp(raw)
    if req.code != 2:
        raise Unsupported("fixture must be a Print-Job request")
    formats = req.values("document-format")
    if formats != [b"image/jpeg"]:
        raise Unsupported("exactly one explicit image/jpeg format is required")
    if req.values("media-col") or req.values("sides") not in ([], [b"one-sided"]):
        raise Unsupported("media collections and duplex are not implemented")
    media = req.values("media")
    if media not in ([], [b"na_index-4x6_4x6in"]):
        raise Unsupported("only the explicit six-inch offline profile is supported")
    values = req.values("copies")
    if len(values) > 1 or (values and len(values[0]) != 4):
        raise Unsupported("invalid copies")
    copies = struct.unpack(">i", values[0])[0] if values else 1
    prepared = prepare_jpeg(req.document, scaling=scaling)
    return prepared, job_model(prepared, copies=copies)
