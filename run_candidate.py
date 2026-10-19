"""Compatibility entry point; implementation lives in tools/."""
import runpy
import sys
from pathlib import Path
root=Path(__file__).resolve().parent
sys.path.insert(0,str(root/"tools"))
if __name__=="__main__":runpy.run_path(str(root/"tools"/'run_candidate.py'),run_name="__main__")
