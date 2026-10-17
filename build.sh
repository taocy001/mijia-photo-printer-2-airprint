#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
xcrun clang++ -std=c++17 -arch arm64 -dynamiclib -Wall -Wextra -Werror -Wno-deprecated-declarations -framework IOKit -framework CoreFoundation usb_transport.cpp -o libmijia_usb.dylib
xcrun clang++ -std=c++17 -arch arm64 -dynamiclib -Wall -Wextra -Werror -Wno-deprecated-declarations -framework CoreGraphics -framework CoreFoundation -lcups document_renderer.cpp -o libmijia_render.dylib
xcrun clang++ -std=c++17 -arch arm64 -Wall -Wextra -Werror -Wno-deprecated-declarations -lcups raster_fixture.cpp -o raster_fixture
mkdir -p fixtures
./raster_fixture urf fixtures/one-page.urf
./raster_fixture pwg fixtures/one-page.pwg
./raster_fixture urf fixtures/two-pages.urf
./raster_fixture pwg fixtures/two-pages.pwg
python3 -m unittest -v test_offline test_protocol test_documents test_bridge test_queue test_layout test_capture test_http
