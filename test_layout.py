"""Synthetic layout checks; never opens USB/network or uses private photographs."""
import unittest
from io import BytesIO
from PIL import Image
from offline import prepare_jpeg,CANVAS

def jpeg(im,**kwargs):
 b=BytesIO();im.save(b,'JPEG',quality=100,**kwargs);return b.getvalue()
class LayoutTests(unittest.TestCase):
 def test_center_fill_removes_only_aspect_excess_without_bars(self):
  im=Image.new('RGB',(1200,1600),'blue');im.paste('red',(0,0,50,1600));im.paste('red',(1150,0,1200,1600))
  result=prepare_jpeg(jpeg(im),scaling='fill')
  with Image.open(BytesIO(result.jpeg)) as out:
   self.assertEqual(out.size,CANVAS)
   for p in [(10,10),(1200,10),(10,1800),(1200,1800)]:self.assertGreater(out.getpixel(p)[2],240)
 def test_fit_keeps_edges_and_white_bars(self):
  im=Image.new('RGB',(1200,1600),'blue');im.paste('red',(0,0,100,1600))
  with Image.open(BytesIO(prepare_jpeg(jpeg(im),scaling='fit').jpeg)) as out:
   self.assertGreater(min(out.getpixel((600,20))),245)
   self.assertGreater(out.getpixel((20,900))[0],240)
 def test_landscape_three_two_rotates_and_uses_whole_canvas(self):
  im=Image.new('RGB',(1800,1200),'red');im.paste('blue',(900,0,1800,1200))
  with Image.open(BytesIO(prepare_jpeg(jpeg(im),scaling='fit').jpeg)) as out:
   self.assertEqual(out.size,CANVAS)
   self.assertGreater(out.getpixel((600,10))[2],240)
   self.assertGreater(out.getpixel((600,1810))[0],240)
 def test_white_content_is_not_automatically_trimmed(self):
  im=Image.new('RGB',(1200,1800),'white');im.paste('blue',(300,600,900,1200))
  with Image.open(BytesIO(prepare_jpeg(jpeg(im),scaling='fill').jpeg)) as out:
   self.assertGreater(min(out.getpixel((600,10))),245)
   self.assertGreater(out.getpixel((600,900))[2],240)
 def test_exif_6_and_reverse(self):
  im=Image.new('RGB',(1800,1200),'blue');im.paste('red',(0,0,900,1200));e=Image.Exif();e[274]=6
  raw=jpeg(im,exif=e)
  with Image.open(BytesIO(prepare_jpeg(raw).jpeg)) as normal,Image.open(BytesIO(prepare_jpeg(raw,rotate_180=True).jpeg)) as reverse:
   self.assertGreater(normal.getpixel((600,10))[0],240)
   self.assertGreater(reverse.getpixel((600,10))[2],240)
 def test_explicit_portrait_does_not_auto_rotate_landscape(self):
  im=Image.new('RGB',(1800,1200),'red');im.paste('blue',(900,0,1800,1200))
  with Image.open(BytesIO(prepare_jpeg(jpeg(im),orientation=3).jpeg)) as out:
   self.assertGreater(out.getpixel((10,900))[0],240)
   self.assertGreater(out.getpixel((1200,900))[2],240)
 def test_none_orientation_is_not_accepted_as_auto(self):
  from offline import Unsupported
  with self.assertRaises(Unsupported):prepare_jpeg(jpeg(Image.new('RGB',(1800,1200),'blue')),orientation=7)
