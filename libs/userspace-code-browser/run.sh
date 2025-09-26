#!/bin/sh
set -eu
# NOT USED ATM
# # https://medium.com/@ferdinandklr/creating-a-docker-in-docker-dind-container-with-any-base-image-7ce3a4d44021
# nohup dockerd >/dev/null 2>&1 &
# sleep 2
# docker ps

SHARED_CRS_SPACE=${SHARED_CRS_SPACE:-/shared-crs-fs}

taskset -c 2-4 /app/code-browser-server --shared "${SHARED_CRS_SPACE}/crs-userspace/code-browser" --address 0.0.0.0:50051
