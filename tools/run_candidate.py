"""Manual experimental runner. NOT RUN during offline development.

Starts HTTP and optionally Bonjour; subsequent client jobs can consume paper.
No install/LaunchDaemon/CUPS edits. Requires separate operator authorization.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))

import argparse
import re
import subprocess
from pathlib import Path
from bridge import make_app, URF
from queued_bridge import QueuedBridge
from documents import FORMATS
from native_transport import NativeUSBTransport


def advertisement(core, port):
    return ["/usr/bin/dns-sd", "-R", "小米照片打印机 2",
            "_ipp._tcp,_universal", "local.", str(port), "txtvers=1", "qtotal=1",
            "rp=ipp/print", "ty=小米照片打印机 2",
            "product=(Mijia Instant Photo Printer 2)", "Color=T", "Duplex=F", "kind=photo", "priority=50",
            "pdl="+",".join(FORMATS), "URF="+b",".join(URF).decode(),
            "UUID="+core.identity.removeprefix("urn:uuid:")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--hostname", required=True, help="this Mac's actual .local hostname")
    parser.add_argument("--host", default="127.0.0.1", )
    parser.add_argument("--additional-host",action="append",default=[])
    parser.add_argument("--port", type=int, default=8632)
    parser.add_argument("--dedupe-content-window",type=int,default=0)
    parser.add_argument("--retain-layout-evidence",action="store_true",help="Opt-in private original/final diagnostics, 20 documents and 256 MiB maximum")
    parser.add_argument("--announce", action="store_true")
    parser.add_argument("--idle-status-refresh", type=int, default=0, metavar="SECONDS", help="poll device supply/alert status while idle (0=off, recommended 60)")
    parser.add_argument("--allow-network",action="append",default=[])
    parser.add_argument("--enable-experimental-live-printing", action="store_true")
    args = parser.parse_args()
    if not args.enable_experimental_live_printing:
        parser.error("live printing requires explicit operator authorization and enable flag")
    if not re.fullmatch(r"[A-Za-z0-9-]+\.local\.?",args.hostname): parser.error("invalid local hostname")
    if not 1024 <= args.port <= 65535: parser.error("use a separate unprivileged port")
    import ipaddress
    address=ipaddress.ip_address(args.host)
    if args.announce and (address.is_loopback or not address.is_private):parser.error("Bonjour requires private LAN address")
    if any(not ipaddress.ip_address(x).is_private for x in args.additional_host):parser.error("additional hosts must be private/local")
    root = Path(__file__).resolve().parents[1]
    def transport():
        return NativeUSBTransport(serial=args.serial,library=root/"build"/"libmijia_usb.dylib").open()
    import fcntl
    process_lock=(root/'airprint-spool'/'process.lock')
    process_lock.parent.mkdir(exist_ok=True)
    lock_handle=process_lock.open('a')
    try:fcntl.flock(lock_handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:parser.error('another queue worker owns this spool')
    core=QueuedBridge(f"ipp://{args.hostname}:{args.port}/ipp/print",transport,root/"airprint-spool",dedupe_seconds=args.dedupe_content_window,retain_layout_evidence=args.retain_layout_evidence,idle_status_seconds=args.idle_status_refresh)
    # No socket or subprocess is created before this explicit main() path.
    import os,hashlib
    from ipp_trace import record
    from aiohttp import web
    registration = None
    app = make_app(core,allowed_networks=args.allow_network or ["127.0.0.0/8"])
    async def announce(app):
        nonlocal registration
        registration = subprocess.Popen(advertisement(core,args.port),
                                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    async def unregister(app):
        if registration:
            registration.terminate()
            try: registration.wait(timeout=3)
            except subprocess.TimeoutExpired:
                registration.kill();registration.wait()
    if args.announce:
        app.on_startup.append(announce)
        app.on_cleanup.append(unregister)
    record(core,{'lifecycle':'startup','pid':os.getpid(),'renderer_sha256':hashlib.sha256((root/'build'/'libmijia_render.dylib').read_bytes()).hexdigest()})
    try:web.run_app(app,host=[args.host]+args.additional_host,port=args.port,access_log=None)
    finally:record(core,{'lifecycle':'shutdown','pid':os.getpid()})


if __name__ == "__main__": main()
