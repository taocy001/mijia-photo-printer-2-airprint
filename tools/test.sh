#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
"${PYTHON:-python3}" -m unittest discover -s tests -p 'test_*.py' -v
