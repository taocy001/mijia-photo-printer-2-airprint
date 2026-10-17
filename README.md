# Mijia Instant Photo Printer 2 — experimental macOS AirPrint bridge

A user-space USB adapter and temporary IPP/Bonjour service for the Mijia Instant Photo Printer 2 (ZMRSHZP02HT, USB VID 302C / PID 3008). A real six-inch photograph has completed through the USB implementation and its output was confirmed by the operator. IPP discovery, capabilities and Validate-Job have passed local checks. iPhone-to-paper validation is still pending.

## Scope

Six-inch 4×6-inch paper only (IPP `na_index-4x6_4x6in`, 10160×15240 hundredths of a millimeter). One page and one copy per job; up to 20 local queued jobs. No four-inch paper, multi-page printing, duplex, textured coating, automatic installation or system printer configuration.

JPEG inputs default to centered fill: preserve aspect ratio and crop edges. Fit preserves the entire photo with white margins. EXIF orientation is applied and landscape content is rotated into the device's 1216×1824 buffer without stretching. Reverse orientations are supported. PDF/URF/PWG inputs preserve the client's already-laid-out page with fit, without a second crop. Requested zero margins describe the full device image canvas; precise mechanical edge tolerances have not been measured.

## Build

Requires macOS, Xcode command-line tools, Python 3 with Pillow and aiohttp already installed. No vendor DLL, Windows installer or third-party binary is distributed. Build output uses the host's arm64 SDK:

```sh
./build.sh
```

The renderer accepts a single unencrypted PDF page or single 8-bit RGB/sRGB/gray URF/PWG page. Limits: 32 MiB request, 40 million decoded pixels, 30-second renderer timeout and 20 CPU seconds in a separate process. Arbitrary untrusted documents should not be considered comprehensively hardened by these limits.

## Temporary service

Run manually, substituting the actual USB serial, local hostname, private LAN address and subnet. The serial is supplied at runtime, not committed:

```sh
python3 run_candidate.py --serial YOUR_USB_SERIAL \
  --hostname YOUR_MAC.local --host YOUR_PRIVATE_LAN_IP --port 8632 \
  --allow-network YOUR_LAN_CIDR --announce --enable-experimental-live-printing
```

Bonjour name: **Mijia Photo Printer 2 - 6 inch**. Resource: `ipp/print`. Only bind to the current private LAN address. Source addresses must be loopback or within the configured subnet. This is plain IPP without authentication or TLS: devices on the allowed LAN can submit jobs. It does not configure internet forwarding, firewall rules, CUPS, other printer queues or login services.

The service runs until the foreground process ends. Ctrl-C stops it and unregisters its own Bonjour record. Restart manually with the same spool directory; no boot installation is performed. Pending work can continue after restart. Do not delete the spool while tasks are pending or uncertain.

## Queue and duplicate protection

`queued_bridge.py` persists prepared JPEGs and job metadata under `airprint-spool/` using SQLite WAL/FULL synchronous commits. Only one executor uses USB. External device activity or an explicit 8006 queue-full rejection leaves a job pending for a later safe attempt. No job is dropped because the device is busy. A single-process lock prevents two live workers sharing the service spool.

Before creating a device job, it checks the actual job-id list and whole-device status. The empty `get-job-info {}` response is not used as proof of idle. Creation, device job ID, upload-start, upload-acknowledged and terminal stages are persisted. Creation/upload uncertainty stops the queue for read-only investigation; no automatic image replay or replacement job is created. After a restart, acknowledged uploads are only polled by the existing device job ID. Completed work is never replayed. Byte-identical IPP requests reuse the stored local job, including across restarts. A client retry with a new request ID cannot always be distinguished from a deliberate new print.

Cancel-Job cancels a still-local pending task without sending USB. An active cancellation targets only the device ID created for that local task and requires a matching client request; device cancellation is attempted once and status is subsequently polled. It never cancels jobs from other apps. Uncertain tasks remain held for investigation.

Pending/processing/stopped/completed/aborted/canceled states are exposed through Get-Jobs and Get-Job-Attributes. Each job remains single-page/single-copy. Queue limits are 20 nonterminal tasks, 32 MiB per request, and 256 MiB for prepared spool images. Completed photo bytes are removed; metadata and bounded USB audit logs remain local. Spool content must never be committed or shared casually.

## Wire behavior established by device testing

Control requests use `cmd json\n` plus compact JSON. Successful creation can omit `error-code` and return only `result.job_id`; omitted error-code defaults to zero, while explicit errors remain errors. Upload blocks contain a maximum 8192 image bytes, prefixed by `cmd data EXTLEN=N+4\n`, a little-endian 32-bit device job ID, and the bytes. A real successful ACK includes ` OK` after its decimal length. Fragmented replies and asynchronous events are handled. Upload completion is separate from physical completion: device state 9 is Completed, 7 Aborted and 8 Canceled.

The project implements only this printer's experimentally verified subset. No affiliation or official driver certification is claimed.

## Validation

Offline tests use synthetic images/raster fixtures and sanitized protocol-only response fixtures. They cover FIFO, busy/8006 waiting, transport failure, lost ACK, restart behavior, cancellation scope, supported formats, six-inch media, orientations and scaling. No test automatically accesses USB or prints paper. Local service verification uses Get-Printer-Attributes, Get-Jobs and Validate-Job only.

For end-to-end validation, select the Bonjour name on an iPhone on the same LAN, select six-inch paper and one copy, and submit one photo. Check the local job state and actual paper. A discoverable Bonjour/IPP service alone is not proof of full AirPrint compatibility.
