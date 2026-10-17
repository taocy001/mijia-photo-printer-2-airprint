"""Explicit manual receive-only diagnostic service; never starts on import."""
import argparse,ipaddress,subprocess
from pathlib import Path
from capture_bridge import CaptureBridge
from bridge import make_app
from run_candidate import advertisement

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',required=True);p.add_argument('--hostname',required=True);p.add_argument('--allow-network',required=True);p.add_argument('--enable-diagnostic-reception',action='store_true');a=p.parse_args()
 if not a.enable_diagnostic_reception:p.error('diagnostic reception needs explicit authorization')
 if not ipaddress.ip_address(a.host).is_private:p.error('private LAN binding required')
 core=CaptureBridge(f'ipp://{a.hostname}:8633/ipp/print',Path(__file__).parent/'diagnostic-spool')
 app=make_app(core,allowed_networks=[a.allow_network]);child=None
 async def start(app):
  nonlocal child
  args=advertisement(core,8633);args[2]='Mijia Layout Capture - NO PRINT';args=[('ty=Receive only - no paper output' if x.startswith('ty=') else x) for x in args]
  child=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 async def stop(app):
  if child:child.terminate();child.wait(timeout=3)
 app.on_startup.append(start);app.on_cleanup.append(stop)
 from aiohttp import web
 web.run_app(app,host=a.host,port=8633,access_log=None)
if __name__=='__main__':main()
