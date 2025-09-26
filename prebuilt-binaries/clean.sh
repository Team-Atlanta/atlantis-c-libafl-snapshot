#!/bin/sh
rm -rf atlantis_cc/*_wrapper
rm -rf atlantis_cc/target
rm -rf concolic_executor*/*.so
rm -rf concolic_executor*/target
rm -rf concolic_mutation_service/concolicd
rm -rf concolic_mutation_service/target
rm -rf symqemu/_build
rm -rf z3/build
rm -rf fuzzer/*.so
rm -rf fuzzer/target
rm -rf LibAFL/target
rm -rf symqemu/build
