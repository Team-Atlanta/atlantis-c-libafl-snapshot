# =============================================================================
# LibAFL Archive (Prepare Phase)
# =============================================================================
# Thin image that extracts build artifacts from atlantis-c-libafl-toolchain.
# Referenced by builder.Dockerfile via COPY --from=atlantis-c-libafl-archive.
# =============================================================================

FROM scratch

# Fuzzer artifacts
COPY --from=atlantis-c-libafl-toolchain /artifacts/libfuzzer.so /libafl/artifacts/libfuzzer.so
COPY --from=atlantis-c-libafl-toolchain /artifacts/cc_wrapper /libafl/artifacts/cc_wrapper
COPY --from=atlantis-c-libafl-toolchain /artifacts/cxx_wrapper /libafl/artifacts/cxx_wrapper
COPY --from=atlantis-c-libafl-toolchain /artifacts/libc++.a /libafl/artifacts/libc++.a

# Python 3.12 (needed by builder for build.py / config_gen)
COPY --from=atlantis-c-libafl-toolchain /usr/local/bin /libafl/python/bin
COPY --from=atlantis-c-libafl-toolchain /usr/local/lib /libafl/python/lib
COPY --from=atlantis-c-libafl-toolchain /usr/local/include/python3.12 /libafl/python/include/python3.12
