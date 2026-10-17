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
#include <regex>
#include <string>

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
// Conservative photo-page recognition: one image, no other drawing operators.
struct PhotoScan { CGAffineTransform ctm=CGAffineTransformIdentity; std::vector<CGAffineTransform> stack; CGRect box=CGRectZero; int images=0; bool valid=true; };
static void photo_q(CGPDFScannerRef,void *v){auto &s=*static_cast<PhotoScan*>(v);s.stack.push_back(s.ctm);}
static void photo_Q(CGPDFScannerRef,void *v){auto &s=*static_cast<PhotoScan*>(v);if(s.stack.empty())s.valid=false;else{s.ctm=s.stack.back();s.stack.pop_back();}}
static void photo_cm(CGPDFScannerRef sc,void *v){auto &s=*static_cast<PhotoScan*>(v);CGPDFReal n[6];for(int i=5;i>=0;--i)if(!CGPDFScannerPopNumber(sc,&n[i])){s.valid=false;return;}s.ctm=CGAffineTransformConcat(CGAffineTransformMake(n[0],n[1],n[2],n[3],n[4],n[5]),s.ctm);}
static void photo_ri(CGPDFScannerRef sc,void *v){auto &s=*static_cast<PhotoScan*>(v);const char *name=nullptr;if(!CGPDFScannerPopName(sc,&name)||(!(!std::strcmp(name,"Perceptual")||!std::strcmp(name,"RelativeColorimetric")||!std::strcmp(name,"AbsoluteColorimetric")||!std::strcmp(name,"Saturation"))))s.valid=false;}
static void photo_Do(CGPDFScannerRef sc,void *v){auto &s=*static_cast<PhotoScan*>(v);const char *name=nullptr;if(!CGPDFScannerPopName(sc,&name)){s.valid=false;return;}auto obj=CGPDFContentStreamGetResource(CGPDFScannerGetContentStream(sc),"XObject",name);CGPDFStreamRef stream=nullptr;if(!obj||!CGPDFObjectGetValue(obj,kCGPDFObjectTypeStream,&stream)){s.valid=false;return;}auto d=CGPDFStreamGetDictionary(stream);const char *type=nullptr;CGPDFBoolean mask=false;if(!CGPDFDictionaryGetName(d,"Subtype",&type)||std::strcmp(type,"Image")||CGPDFDictionaryGetBoolean(d,"ImageMask",&mask)||CGPDFDictionaryGetObject(d,"SMask",&obj)||CGPDFDictionaryGetObject(d,"Mask",&obj)){s.valid=false;return;}if(std::abs(s.ctm.b)>0.001||std::abs(s.ctm.c)>0.001){s.valid=false;return;}s.box=CGRectApplyAffineTransform(CGRectMake(0,0,1,1),s.ctm);++s.images;}
static bool photo_bounds(CGPDFPageRef page,CGRect &out){
 auto pd=CGPDFPageGetDictionary(page);CGPDFArrayRef annotations=nullptr;if(CGPDFDictionaryGetArray(pd,"Annots",&annotations)&&CGPDFArrayGetCount(annotations))return false;
 CGPDFStreamRef stream=nullptr;if(!CGPDFDictionaryGetStream(pd,"Contents",&stream))return false;
 auto bytes=CGPDFStreamCopyData(stream,nullptr);if(!bytes)return false;
 std::string text((const char*)CFDataGetBytePtr(bytes),CFDataGetLength(bytes));CFRelease(bytes);
 // Reject text, paths, clipping, forms, inline images and unknown operators.
 static const std::regex tokens(R"((\s+|[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)|/[A-Za-z0-9_]+|cm|Do|ri|q|Q)+)");
 if(!std::regex_match(text,tokens))return false;
 PhotoScan state;auto table=CGPDFOperatorTableCreate();CGPDFOperatorTableSetCallback(table,"q",photo_q);CGPDFOperatorTableSetCallback(table,"Q",photo_Q);CGPDFOperatorTableSetCallback(table,"cm",photo_cm);CGPDFOperatorTableSetCallback(table,"Do",photo_Do);CGPDFOperatorTableSetCallback(table,"ri",photo_ri);
 auto content=CGPDFContentStreamCreateWithPage(page);auto scanner=CGPDFScannerCreate(content,table,&state);bool ok=CGPDFScannerScan(scanner);CGPDFScannerRelease(scanner);CGPDFContentStreamRelease(content);CGPDFOperatorTableRelease(table);
 if(!ok||!state.valid||state.images!=1||!state.stack.empty())return false;
 CGRect crop=CGPDFPageGetBoxRect(page,kCGPDFCropBox),b=CGRectStandardize(state.box);double eps=0.1;
 if(b.size.width<=0||b.size.height<=0||b.origin.x<crop.origin.x-eps||b.origin.y<crop.origin.y-eps||CGRectGetMaxX(b)>CGRectGetMaxX(crop)+eps||CGRectGetMaxY(b)>CGRectGetMaxY(crop)+eps)return false;
 bool fullw=std::abs(b.size.width-crop.size.width)<eps,fullh=std::abs(b.size.height-crop.size.height)<eps;
 if(!fullw&&!fullh)return false;
 if(b.size.width/crop.size.width<0.5||b.size.height/crop.size.height<0.5)return false;
 if(std::abs(CGRectGetMidX(b)-CGRectGetMidX(crop))>eps||std::abs(CGRectGetMidY(b)-CGRectGetMidY(crop))>eps)return false;
 out=b;return true;
}
static int render_pdf(const unsigned char *data, size_t size, std::vector<unsigned char> &rgb,
                      uint32_t &width, uint32_t &height, char *err, size_t cap, bool photo=false) {
    CGDataProviderRef provider = CGDataProviderCreateWithData(nullptr, data, size, nullptr);
    if (!provider) return fail(err, cap, "cannot create PDF data provider");
    CGPDFDocumentRef doc = CGPDFDocumentCreateWithProvider(provider); CGDataProviderRelease(provider);
    if (!doc) return fail(err, cap, "invalid PDF");
    if (CGPDFDocumentIsEncrypted(doc) || CGPDFDocumentGetNumberOfPages(doc) != 1) {
        CGPDFDocumentRelease(doc); return fail(err, cap, "only unencrypted single-page PDF supported");
    }
    CGPDFPageRef page = CGPDFDocumentGetPage(doc, 1);
    CGRect crop = CGPDFPageGetBoxRect(page, kCGPDFCropBox);
    CGRect photo_box=CGRectZero;bool recognized=photo&&photo_bounds(page,photo_box);
    double natural_width=crop.size.width,natural_height=crop.size.height;
    if(CGPDFPageGetRotationAngle(page)%180)std::swap(natural_width,natural_height);
    auto natural=CGPDFPageGetDrawingTransform(page,kCGPDFCropBox,CGRectMake(0,0,natural_width,natural_height),0,true);
    CGRect bounds=recognized?CGRectApplyAffineTransform(photo_box,natural):CGRectMake(0,0,natural_width,natural_height);
    double page_width = recognized?bounds.size.width:crop.size.width, page_height = recognized?bounds.size.height:crop.size.height;
    if (!recognized && CGPDFPageGetRotationAngle(page) % 180) std::swap(page_width, page_height);
    if (!std::isfinite(page_width) || !std::isfinite(page_height) || page_width <= 0 || page_height <= 0) {
        CGPDFDocumentRelease(doc); return fail(err, cap, "invalid PDF page box");
    }
    double aspect = page_width/page_height;
    const double target_width = aspect > 1 ? 1824.0 : 1216.0;
    const double target_height = aspect > 1 ? 1216.0 : 1824.0;
    if (aspect >= target_width/target_height) { width = unsigned(target_width); height = std::max(1U, unsigned(std::round(target_width/aspect))); }
    else { height = unsigned(target_height); width = std::max(1U, unsigned(std::round(target_height*aspect))); }
    std::vector<unsigned char> rgba(size_t(width)*height*4, 255);
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(rgba.data(), width, height, 8, width*4,
                                                space, kCGImageAlphaNoneSkipLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(space);
    if (!context) { CGPDFDocumentRelease(doc); return fail(err, cap, "cannot create PDF bitmap"); }
    // Do not rely on GetDrawingTransform to upscale PDF points to bitmap pixels.
    // On the tested macOS runtime it centers small pages at unit scale even in a
    // larger destination rectangle. Normalize rotation/origin at natural page
    // size, then explicitly convert those page units to target raster pixels.
    CGAffineTransform transform = CGPDFPageGetDrawingTransform(page, kCGPDFCropBox,
        CGRectMake(0, 0, page_width, page_height), 0, true);
    const double scale = std::min(double(width)/page_width, double(height)/page_height);
    CGContextTranslateCTM(context, (width-page_width*scale)/2, (height-page_height*scale)/2);
    CGContextScaleCTM(context, scale, scale);
    if(recognized){CGContextTranslateCTM(context,-bounds.origin.x,-bounds.origin.y);CGContextConcatCTM(context,natural);}else CGContextConcatCTM(context, transform); CGContextDrawPDFPage(context, page);
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
    if (!std::strcmp(mime, "application/pdf") || !std::strcmp(mime,"application/x-mijia-photo-pdf")) ok = render_pdf(data, size, rgb, *width, *height, err, cap, !std::strcmp(mime,"application/x-mijia-photo-pdf"));
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

extern "C" int mijia_pdf_photo_bounds(const unsigned char *data,size_t size){
 if(!data||!size||size>32*1024*1024)return 0;
 auto provider=CGDataProviderCreateWithData(nullptr,data,size,nullptr);if(!provider)return 0;
 auto doc=CGPDFDocumentCreateWithProvider(provider);CGDataProviderRelease(provider);if(!doc)return 0;
 CGRect bounds=CGRectZero;bool ok=!CGPDFDocumentIsEncrypted(doc)&&CGPDFDocumentGetNumberOfPages(doc)==1&&photo_bounds(CGPDFDocumentGetPage(doc,1),bounds);CGPDFDocumentRelease(doc);return ok?1:0;
}
