# =============================================================================
# CRS Builder Dockerfile
# =============================================================================
# BUILD phase: Uses pre-built artifacts from atlantis-c-libafl-archive (prepare phase).
# No Rust/LLVM compilation here — just copies artifacts and build scripts.
# =============================================================================

ARG target_base_image

# Reference archive image from prepare phase
FROM atlantis-c-libafl-archive AS crs-tools

FROM ${target_base_image}

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

# Copy pre-built fuzzer artifacts
RUN mkdir -p /data /crs
COPY --from=crs-tools /libafl/artifacts /data/artifacts

# Copy Python 3.12 from archive
COPY --from=crs-tools /libafl/python/bin /usr/local/bin
COPY --from=crs-tools /libafl/python/lib /usr/local/lib
COPY --from=crs-tools /libafl/python/include/python3.12 /usr/local/include/python3.12
RUN ldconfig

# Set up venv and install openai
RUN python3.12 -m venv /crs/venv && \
    /crs/venv/bin/pip install --no-cache-dir openai requests

COPY ./config_gen /crs/config_gen
COPY ./build.py /crs/build.py
COPY ./bin/compile_target /usr/local/bin/compile_target
RUN chmod +x /usr/local/bin/compile_target

CMD ["compile_target"]
