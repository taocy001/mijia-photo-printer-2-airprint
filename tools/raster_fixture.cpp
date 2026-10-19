// Synthetic fixture writer. Writes only named local files; no printer APIs.
#include <cups/raster.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstring>
#include <vector>
#include <cstdio>
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    bool apple = !std::strcmp(argv[1], "urf");
    int fd = open(argv[2], O_CREAT|O_TRUNC|O_WRONLY, 0600);
    if (fd < 0) return 3;
    cups_raster_t *r = cupsRasterOpen(fd, apple ? CUPS_RASTER_WRITE_APPLE : CUPS_RASTER_WRITE_PWG);
    if (!r) { close(fd); return 4; }
    cups_page_header2_t h{};
    const bool landscape = std::strstr(argv[2], "landscape");
    h.cupsWidth = landscape ? 1800 : 1200; h.cupsHeight = landscape ? 1200 : 1800;
    h.HWResolution[0] = h.HWResolution[1] = 300;
    h.PageSize[0] = landscape ? 432 : 288; h.PageSize[1] = landscape ? 288 : 432;
    h.cupsPageSize[0] = h.PageSize[0]; h.cupsPageSize[1] = h.PageSize[1];
    h.cupsBitsPerColor = 8; h.cupsBitsPerPixel = 24;
    h.cupsNumColors = 3; h.cupsColorSpace = CUPS_CSPACE_SRGB;
    h.cupsColorOrder = CUPS_ORDER_CHUNKED; h.cupsBytesPerLine = h.cupsWidth*3;
    h.NumCopies = 1;
    int pages = std::strstr(argv[2], "two-pages") ? 2 : 1;
    std::vector<unsigned char> line(h.cupsBytesPerLine);
    for (int page = 0; page < pages; ++page) {
        if (!cupsRasterWriteHeader2(r, &h)) return 5;
        for (unsigned y = 0; y < h.cupsHeight; ++y) {
            for (unsigned x = 0; x < h.cupsWidth; ++x) {
                line[3*x] = y < h.cupsHeight/2 ? 255 : 0;
                line[3*x+1] = 0;
                line[3*x+2] = y < h.cupsHeight/2 ? 0 : 255;
            }
            if (cupsRasterWritePixels(r, line.data(), unsigned(line.size())) != line.size()) return 6;
        }
    }
    cupsRasterClose(r); close(fd);
    return 0;
}
