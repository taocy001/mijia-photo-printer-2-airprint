// Pure memory conversion with macOS CoreGraphics/CUPS. No printing/network API.
#include <CoreGraphics/CoreGraphics.h>
#include <cups/raster.h>
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <vector>

struct Input { const unsigned char *data; size_t size; size_t position; };
static ssize_t read_memory(void *context, unsigned char *dest, size_t length) {
    auto *in = static_cast<Input *>(context);
    size_t count = std::min(length, in->size-in->position);
    std::memcpy(dest, in->data+in->position, count); in->position += count;
    return static_cast<ssize_t>(count);
}
static int fail(char *err, size_t cap, const char *message) {
    if (err && cap) std::snprintf(err, cap, "%s", message);
    return 0;
}
static int render_pdf(const unsigned char *data, size_t size, std::vector<unsigned char> &rgb,
                      uint32_t &width, uint32_t &height, char *err, size_t cap) {
    CGDataProviderRef provider = CGDataProviderCreateWithData(nullptr, data, size, nullptr);
    if (!provider) return fail(err, cap, "cannot create PDF data provider");
    CGPDFDocumentRef doc = CGPDFDocumentCreateWithProvider(provider); CGDataProviderRelease(provider);
    if (!doc) return fail(err, cap, "invalid PDF");
    if (CGPDFDocumentIsEncrypted(doc) || CGPDFDocumentGetNumberOfPages(doc) != 1) {
        CGPDFDocumentRelease(doc); return fail(err, cap, "only unencrypted single-page PDF supported");
    }
    CGPDFPageRef page = CGPDFDocumentGetPage(doc, 1);
    CGRect crop = CGPDFPageGetBoxRect(page, kCGPDFCropBox);
    double page_width = crop.size.width, page_height = crop.size.height;
    if (CGPDFPageGetRotationAngle(page) % 180) std::swap(page_width, page_height);
    if (!std::isfinite(page_width) || !std::isfinite(page_height) || page_width <= 0 || page_height <= 0) {
        CGPDFDocumentRelease(doc); return fail(err, cap, "invalid PDF page box");
    }
    double aspect = page_width/page_height;
    if (aspect >= 1216.0/1824.0) { width = 1216; height = std::max(1U, unsigned(std::round(1216/aspect))); }
    else { height = 1824; width = std::max(1U, unsigned(std::round(1824*aspect))); }
    std::vector<unsigned char> rgba(size_t(width)*height*4, 255);
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(rgba.data(), width, height, 8, width*4,
                                                space, kCGImageAlphaNoneSkipLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(space);
    if (!context) { CGPDFDocumentRelease(doc); return fail(err, cap, "cannot create PDF bitmap"); }
    CGAffineTransform transform = CGPDFPageGetDrawingTransform(page, kCGPDFCropBox,
        CGRectMake(0, 0, width, height), 0, true);
    CGContextConcatCTM(context, transform); CGContextDrawPDFPage(context, page);
    CGContextRelease(context); CGPDFDocumentRelease(doc);
    rgb.resize(size_t(width)*height*3);
    for (size_t i = 0; i < size_t(width)*height; ++i)
        std::memcpy(rgb.data()+3*i, rgba.data()+4*i, 3);
    return 1;
}

static int render_raster(const unsigned char *data, size_t size, std::vector<unsigned char> &rgb,
                         uint32_t &width, uint32_t &height, char *err, size_t cap) {
    Input input{data, size, 0};
    cups_raster_t *raster = cupsRasterOpenIO(read_memory, &input, CUPS_RASTER_READ);
    if (!raster) return fail(err, cap, "cannot open raster");
    cups_page_header2_t header{};
    if (!cupsRasterReadHeader2(raster, &header)) {
        cupsRasterClose(raster); return fail(err, cap, "invalid raster header");
    }
    width = header.cupsWidth; height = header.cupsHeight;
    bool color = (header.cupsColorSpace == CUPS_CSPACE_SRGB || header.cupsColorSpace == CUPS_CSPACE_RGB)
        && header.cupsBitsPerPixel == 24 && header.cupsNumColors == 3;
    bool gray = header.cupsColorSpace == CUPS_CSPACE_SW && header.cupsBitsPerPixel == 8 && header.cupsNumColors == 1;
    unsigned channels = color ? 3 : 1;
    if (!width || !height || uint64_t(width)*height > 40000000 || (!color && !gray) ||
        header.cupsBitsPerColor != 8 || header.cupsColorOrder != CUPS_ORDER_CHUNKED ||
        header.Duplex || header.NumCopies > 1 || header.cupsBytesPerLine < uint64_t(width)*channels ||
        header.cupsBytesPerLine > uint64_t(width)*channels+4096) {
        cupsRasterClose(raster); return fail(err, cap, "unsupported raster geometry/color/duplex/copy count");
    }
    rgb.resize(size_t(width)*height*3);
    std::vector<unsigned char> row(header.cupsBytesPerLine);
    for (uint32_t y = 0; y < height; ++y) {
        if (cupsRasterReadPixels(raster, row.data(), header.cupsBytesPerLine) != header.cupsBytesPerLine) {
            cupsRasterClose(raster); return fail(err, cap, "truncated raster pixels");
        }
        auto *dest = rgb.data()+size_t(y)*width*3;
        if (color) std::memcpy(dest, row.data(), width*3);
        else for (uint32_t x = 0; x < width; ++x) dest[3*x] = dest[3*x+1] = dest[3*x+2] = row[x];
    }
    cups_page_header2_t next{};
    if (cupsRasterReadHeader2(raster, &next)) {
        cupsRasterClose(raster); return fail(err, cap, "multiple raster pages are not supported");
    }
    cupsRasterClose(raster);
    return 1;
}

extern "C" int mijia_render(const unsigned char *data, size_t size, const char *mime,
                             unsigned char **output, uint32_t *width, uint32_t *height,
                             char *err, size_t cap) {
    if (!data || !size || size > 32*1024*1024 || !mime || !output || !width || !height)
        return fail(err, cap, "invalid document arguments");
    *output = nullptr;
    std::vector<unsigned char> rgb;
    int ok = 0;
    if (!std::strcmp(mime, "application/pdf")) ok = render_pdf(data, size, rgb, *width, *height, err, cap);
    else if (!std::strcmp(mime, "image/urf") || !std::strcmp(mime, "image/pwg-raster")) {
        bool signature = !std::strcmp(mime, "image/urf") ?
            (size >= 8 && !std::memcmp(data, "UNIRAST\0", 8)) :
            (size >= 4 && !std::memcmp(data, "RaS2", 4));
        if (!signature) return fail(err, cap, "raster MIME/signature mismatch");
        ok = render_raster(data, size, rgb, *width, *height, err, cap);
    } else return fail(err, cap, "unsupported MIME type");
    if (!ok) return 0;
    auto *buffer = static_cast<unsigned char *>(std::malloc(rgb.size()));
    if (!buffer) return fail(err, cap, "allocation failed");
    std::memcpy(buffer, rgb.data(), rgb.size()); *output = buffer;
    return 1;
}
extern "C" void mijia_render_free(void *data) { std::free(data); }
