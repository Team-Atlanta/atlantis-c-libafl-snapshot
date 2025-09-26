#!/bin/sh

set -eu

echo "Current aflplusplus"
$SRC/aflplusplus/afl-cc --version

cp -fa /work/aflplusplus/* $SRC/aflplusplus

echo "New aflplusplus"
$SRC/aflplusplus/afl-cc --version

compile
