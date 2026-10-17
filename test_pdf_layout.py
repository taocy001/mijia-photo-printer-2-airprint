"""Synthetic PDFs exercise page geometry; no user data or USB access."""
from io import BytesIO
import unittest
from PIL import Image,ImageChops,ImageDraw
from documents import prepare_document

def photo_pdf(width=288,height=432,rotate=0,origin=(0,0),user_unit=1,inset=0,colored=False,markers=False,photo_size=None,image_box=None,extra_ops="",white_content=False,intent=None):
 landscape=width>height;im=Image.new('RGB',photo_size or ((1800,1200) if landscape else (1200,1800)),'blue')
 if white_content:im.paste('white',(0,0,im.width//3,im.height))
 if colored:im.paste('red',(0,0,im.width//2,im.height))
 if markers:
  d=ImageDraw.Draw(im)
  for xy,color in [((0,0,im.width-1,49),'yellow'),((0,im.height-50,im.width-1,im.height-1),'cyan'),((0,0,49,im.height-1),'red'),((im.width-50,0,im.width-1,im.height-1),'green')]:d.rectangle(xy,fill=color)
 b=BytesIO();im.save(b,'JPEG',quality=95);jpg=b.getvalue();x,y=origin
 bw,bh,bx,by=image_box or (width*(1-2*inset),height*(1-2*inset),x+width*inset,y+height*inset)
 stream=((f'q Q q /{intent} ri ' if intent else '')+f'q {bw} 0 0 {bh} {bx} {by} cm /Photo Do Q\n'+extra_ops+(' Q' if intent else '')).encode()
 objs=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',f'<< /Type /Page /Parent 2 0 R /MediaBox [{x} {y} {x+width} {y+height}] /CropBox [{x} {y} {x+width} {y+height}] /Rotate {rotate} /UserUnit {user_unit} /Resources << /XObject << /Photo 5 0 R >> >> /Contents 4 0 R >>'.encode(),f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'endstream',f'<< /Type /XObject /Subtype /Image /Width {im.width} /Height {im.height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpg)} >>\nstream\n'.encode()+jpg+b'\nendstream']
 out=bytearray(b'%PDF-1.6\n');offsets=[0]
 for i,obj in enumerate(objs,1):offsets.append(len(out));out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
 start=len(out);out.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
 for off in offsets[1:]:out.extend(f'{off:010d} 00000 n \n'.encode())
 out.extend(f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode());return bytes(out)
class PdfLayoutTests(unittest.TestCase):
 def test_full_page_images_not_shrunk_by_rotation_or_box_origin(self):
  for kw in [{},{'width':432,'height':288},{'width':432,'height':288,'rotate':90},{'width':432,'height':288,'rotate':270},{'origin':(100,200)},{'user_unit':3}]:
   with self.subTest(**kw):
    result=prepare_document(photo_pdf(**kw),'application/pdf',scaling='fit')
    with Image.open(BytesIO(result.jpeg)) as out:
     self.assertEqual(out.size,(1216,1824))
     for xy in [(20,20),(1190,20),(20,1790),(1190,1790)]:self.assertGreater(out.getpixel(xy)[2],240);self.assertLess(out.getpixel(xy)[0],10)
 def test_baked_page_margins_are_preserved_not_automatically_trimmed(self):
  result=prepare_document(photo_pdf(inset=0.32),'application/pdf',scaling='fit')
  with Image.open(BytesIO(result.jpeg)) as out:
   self.assertGreater(min(out.getpixel((600,20))),240)
   box=ImageChops.difference(out.convert('RGB'),Image.new('RGB',out.size,'white')).point(lambda x:255 if x>30 else 0).getbbox()
   self.assertLess((box[2]-box[0])*(box[3]-box[1])/(1216*1824),0.15)

 def test_landscape_image_direction_honors_page_rotation_once(self):
  for rotation,top,bottom in [(0,2,0),(90,0,2),(270,2,0)]:
   with self.subTest(rotation=rotation):
    r=prepare_document(photo_pdf(width=432,height=288,rotate=rotation,colored=True),'application/pdf',scaling='fit')
    with Image.open(BytesIO(r.jpeg)) as im:
     a=im.getpixel((600,100));b=im.getpixel((600,1700))
     self.assertGreater(a[top],240);self.assertLess(a[bottom],10)
     self.assertGreater(b[bottom],240);self.assertLess(b[top],10)

 def test_all_four_edge_markers_survive_full_page_render(self):
  for w,h in [(288,432),(432,288)]:
   r=prepare_document(photo_pdf(width=w,height=h,markers=True),'application/pdf',scaling='fit')
   with Image.open(BytesIO(r.jpeg)) as im:
    samples=[im.getpixel(p) for p in [(600,10),(600,1810),(10,900),(1200,900)]]
    self.assertTrue(all(min(p)<80 for p in samples),samples)
    self.assertTrue(any(p[0]>200 and p[1]<80 for p in samples))
    self.assertTrue(any(p[1]>70 and p[0]<80 for p in samples))

class PhotoPdfFillTests(unittest.TestCase):
 def render(self,**kw):
  return Image.open(BytesIO(prepare_document(photo_pdf(**kw),'application/pdf',scaling='fill').jpeg)).convert('RGB')
 def test_skinny_portrait_uses_image_boundary(self):
  im=self.render(photo_size=(900,1800),image_box=(216,432,36,0))
  for xy in [(10,900),(1200,900),(600,10),(600,1810)]:
   self.assertGreater(im.getpixel(xy)[2],240);self.assertLess(im.getpixel(xy)[0],10)
 def test_wider_portrait_landscape_rotation(self):
  for kw in [dict(photo_size=(1200,1600),image_box=(288,384,0,24)),dict(width=432,height=288,photo_size=(1600,900),image_box=(432,243,0,22.5)),dict(photo_size=(900,1800),image_box=(216,432,36,0),rotate=90)]:
   im=self.render(**kw)
   for xy in [(10,900),(1200,900),(600,10),(600,1810)]:self.assertLess(im.getpixel(xy)[0],10)
 def test_real_white_content_preserved(self):
  im=self.render(photo_size=(900,1800),image_box=(216,432,36,0),white_content=True)
  self.assertGreater(min(im.getpixel((10,900))),240);self.assertLess(im.getpixel((1200,900))[0],10)
 def test_ambiguous_pdf_falls_back_to_fit(self):
  for kw in [dict(image_box=(216,432,36,0),extra_ops='q 10 0 0 10 0 0 cm /Photo Do Q'),dict(image_box=(216,432,36,0),extra_ops='BT ET'),dict(inset=.1)]:
   a=Image.open(BytesIO(prepare_document(photo_pdf(**kw),'application/pdf',scaling='fill').jpeg)).convert('RGB')
   b=Image.open(BytesIO(prepare_document(photo_pdf(**kw),'application/pdf',scaling='fit').jpeg)).convert('RGB')
   self.assertIsNone(ImageChops.difference(a,b).getbbox())

class RenderingIntentPdfTests(unittest.TestCase):
 def test_ios_photo_intents_do_not_force_layout_fallback(self):
  for intent in ['Perceptual','RelativeColorimetric','AbsoluteColorimetric','Saturation']:
   with self.subTest(intent=intent):
    kw=dict(photo_size=(900,1800),image_box=(216,432,36,0))
    a=Image.open(BytesIO(prepare_document(photo_pdf(**kw,intent=intent),'application/pdf',scaling='fill').jpeg)).convert('RGB')
    b=Image.open(BytesIO(prepare_document(photo_pdf(**kw),'application/pdf',scaling='fill').jpeg)).convert('RGB')
    self.assertIsNone(ImageChops.difference(a,b).getbbox())
 def test_unknown_intent_falls_back_without_cropping(self):
  pdf=photo_pdf(photo_size=(900,1800),image_box=(216,432,36,0),intent='UnknownIntent')
  a=Image.open(BytesIO(prepare_document(pdf,'application/pdf',scaling='fill').jpeg)).convert('RGB')
  b=Image.open(BytesIO(prepare_document(pdf,'application/pdf',scaling='fit').jpeg)).convert('RGB')
  self.assertIsNone(ImageChops.difference(a,b).getbbox())
