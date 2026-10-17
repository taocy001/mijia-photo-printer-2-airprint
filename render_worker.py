"""Bounded single-use document renderer; no USB, networking, or file output."""
import sys,json,resource
from documents import prepare_document
resource.setrlimit(resource.RLIMIT_CPU,(20,20))
args=json.loads(sys.stdin.buffer.readline(4096))
data=sys.stdin.buffer.read(32*1024*1024+1)
if len(data)>32*1024*1024:raise ValueError('document too large')
image=prepare_document(data,args['mime'],scaling=args['scaling'],rotate_180=args['reverse'],orientation=args.get('orientation'))
sys.stdout.buffer.write(image.jpeg)
