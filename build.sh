#!/bin/zsh
set -euo pipefail
exec "${0:A:h}/tools/build.sh" "$@"
