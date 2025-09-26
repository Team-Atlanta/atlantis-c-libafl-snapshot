#!/bin/bash

set -e

cd $(dirname $0)/glib-2.66
python3.12 -m venv /meson-venv
source /meson-venv/bin/activate
pip3 install setuptools
pip3 install meson
meson _build
ninja -C _build
ninja -C _build install

