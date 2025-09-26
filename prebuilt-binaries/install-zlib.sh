#!/bin/sh

set -e

cd $(dirname $0)/zlib-1.2.13
CFLAGS="-fPIC" ./configure --static
make -j$(nproc)
