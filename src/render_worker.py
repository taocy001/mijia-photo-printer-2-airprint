"""Bounded single-use document renderer; no USB, networking, or file output."""
import sys,json,resource
from documents import prepare_document,prepare_splice_document
from offline import Unsupported
resource.setrlimit(resource.RLIMIT_CPU,(20,20))
args=json.loads(sys.stdin.buffer.readline(4096))
data=sys.stdin.buffer.read(32*1024*1024+1)
if len(data)>32*1024*1024:raise ValueError('document too large')
try:
 if args.get('splice'):
  image=prepare_splice_document(data,args['mime'],scaling=args['scaling'])
 else:
  image=prepare_document(data,args['mime'],scaling=args['scaling'],rotate_180=args['reverse'],orientation=args.get('orientation'))
except Unsupported as e:
 print(str(e),file=sys.stderr);sys.exit(2)   # 调用方据此回退普通整版渲染
sys.stdout.buffer.write(image.jpeg)
