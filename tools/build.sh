#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."
mkdir -p build tests/fixtures
ARCH="$(uname -m)"
xcrun clang++ -std=c++17 -arch "$ARCH" -dynamiclib -Wall -Wextra -Werror -Wno-deprecated-declarations -framework IOKit -framework CoreFoundation native/usb_transport.cpp -o build/libmijia_usb.dylib
xcrun clang++ -std=c++17 -arch "$ARCH" -dynamiclib -Wall -Wextra -Werror -Wno-deprecated-declarations -framework CoreGraphics -framework CoreFoundation -lcups native/document_renderer.cpp -o build/libmijia_render.dylib
xcrun clang++ -std=c++17 -arch "$ARCH" -Wall -Wextra -Werror -Wno-deprecated-declarations -lcups tools/raster_fixture.cpp -o build/raster_fixture
for format in urf pwg; do
  for page in one-page landscape two-pages; do
    ./build/raster_fixture "$format" "tests/fixtures/$page.$format"
  done
done
./tools/test.sh
