#!/bin/sh

set -e

cd $(dirname $0)

export ATLANTIS_CC_INSTRUMENTATION_MODE=symcc_clang_cov
export ATLANTIS_CC_PATCH_PROFILE_RUNTIME=./patch_profile_runtime.sh
export SYMCC_PASS_PATH=../symcc-pass/build/libsymcc.so 
export LIBSYMCC_RT_PATH=../concolic_executor/libsymcc-rt.so
export LLVM_CONFIG=$(which llvm-config)

./cc_wrapper -o test -Wl,--wrap=open test.c 
