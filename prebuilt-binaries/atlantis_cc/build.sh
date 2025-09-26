#!/bin/sh

set -e
cd `dirname $0`

cargo build --release
cp target/release/cc_wrapper .
cp target/release/cxx_wrapper .
