from pathlib import Path
from io import BytesIO
import unittest
from PIL import Image
from documents import prepare_document
from offline import Unsupported


def simple_pdf(pages=1):
    content = b'1 0 0 rg 0 0 100 75 re f 0 0 1 rg 0 75 100 75 re f\n'
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>',
            f'<< /Type /Pages /Count {pages} /Kids ['.encode()+b' '.join(f'{3+i} 0 R'.encode() for i in range(pages))+b'] >>']
    stream_id = 3+pages
    for _ in range(pages):
        objs.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 150] /Contents {stream_id} 0 R >>'.encode())
    objs.append(f'<< /Length {len(content)} >>\nstream\n'.encode()+content+b'endstream')
    out = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out)); out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref = len(out); out.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for pos in offsets[1:]: out.extend(f'{pos:010d} 00000 n \n'.encode())
    out.extend(f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(out)


class DocumentTests(unittest.TestCase):
    def test_pdf_single_page_orientation(self):
        prepared = prepare_document(simple_pdf(), 'application/pdf')
        with Image.open(BytesIO(prepared.jpeg)) as im:
            self.assertEqual(im.size, (1216,1824))
            self.assertLess(im.getpixel((600,100))[0],10)
            self.assertGreater(im.getpixel((600,100))[2],240)  # PDF top is blue
            self.assertLess(im.getpixel((600,1700))[2],10)
            self.assertGreater(im.getpixel((600,1700))[0],240)

    def test_pdf_multiple_pages_and_corruption_rejected(self):
        for data in [simple_pdf(2), b'%PDF-invalid']:
            with self.assertRaises(Unsupported): prepare_document(data, 'application/pdf')

    def test_pwg_and_urf_srgb_rasters(self):
        for ext, mime in [('pwg','image/pwg-raster'), ('urf','image/urf')]:
            raw = (Path(__file__).parent/'fixtures'/('one-page.'+ext)).read_bytes()
            result = prepare_document(raw,mime)
            with Image.open(BytesIO(result.jpeg)) as im:
                self.assertEqual(im.size,(1216,1824))
                self.assertLess(im.getpixel((600,100))[2],10)
                self.assertGreater(im.getpixel((600,100))[0],240)
                self.assertLess(im.getpixel((600,1700))[0],10)
                self.assertGreater(im.getpixel((600,1700))[2],240)

    def test_multiple_raster_pages_and_bad_signature_rejected(self):
        for ext, mime in [('pwg','image/pwg-raster'), ('urf','image/urf')]:
            with self.assertRaises(Unsupported):
                prepare_document((Path(__file__).parent/'fixtures'/('two-pages.'+ext)).read_bytes(),mime)
            with self.assertRaises(Unsupported): prepare_document(b'bad raster',mime)


if __name__ == '__main__': unittest.main()

class LandscapeRasterTests(unittest.TestCase):
    def test_landscape_urf_and_pwg_rotate_without_white_padding(self):
        for ext,mime in [('pwg','image/pwg-raster'),('urf','image/urf')]:
            result=prepare_document((Path(__file__).parent/'fixtures'/('landscape.'+ext)).read_bytes(),mime)
            with Image.open(BytesIO(result.jpeg)) as im:
                left=im.getpixel((20,900));right=im.getpixel((1190,900))
                self.assertGreater(left[0],240);self.assertLess(left[2],10)
                self.assertGreater(right[2],240);self.assertLess(right[0],10)
