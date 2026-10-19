"""Usage: python3 prepare.py input.jpg output-directory [--fit] [--rotate-180]."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))

import argparse
import json
from pathlib import Path
from offline import prepare_jpeg, job_model

parser = argparse.ArgumentParser(description="Prepare a six-inch JPEG and offline job model. No printing.")
parser.add_argument("input", type=Path)
parser.add_argument("output", type=Path)
parser.add_argument("--fit", action="store_true", help="white bars instead of centered crop")
parser.add_argument("--rotate-180", action="store_true", help="explicit transport image rotation")
args = parser.parse_args()
result = prepare_jpeg(args.input.read_bytes(), scaling="fit" if args.fit else "fill",
                      rotate_180=args.rotate_180)
args.output.mkdir(parents=True, exist_ok=True)
for name in ("photo.jpg", "job-model.json"):
    if (args.output/name).exists():
        parser.error(f"refusing to overwrite {args.output/name}")
(args.output/"photo.jpg").write_bytes(result.jpeg)
model = job_model(result)
model["jpeg-quality"] = result.quality
model["pixel-size"] = list(result.size)
(args.output/"job-model.json").write_text(json.dumps(model, indent=2)+"\n")
print(f"Prepared {result.size[0]}x{result.size[1]} JPEG, {len(result.jpeg)} bytes, Q={result.quality}; no transport.")
