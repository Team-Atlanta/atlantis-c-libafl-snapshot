# Test Dockerfile for fuzzer changes
# Builds the fuzzer and tests on an already-instrumented target (libxml2)

# Build stage - compile the fuzzer
FROM ubuntu:20.04 AS builder

ENV TZ=US \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update -y && apt-get install -y \
    build-essential \
    wget \
    curl \
    lsb-release \
    software-properties-common \
    cmake \
    protobuf-compiler \
    git \
    make \
    ninja-build \
    gnupg \
    libzstd-dev \
    zlib1g-dev \
    libssl-dev \
    pkg-config

# Install LLVM 18
ENV LLVM_VERSION=18
RUN wget https://apt.llvm.org/llvm.sh && \
    chmod +x llvm.sh && \
    ./llvm.sh ${LLVM_VERSION} all && \
    ln -s /usr/bin/clang-${LLVM_VERSION} /usr/bin/clang && \
    ln -s /usr/bin/clang++-${LLVM_VERSION} /usr/bin/clang++ && \
    ln -s /usr/lib/llvm-${LLVM_VERSION}/lib/libc++.a /usr/lib/libc++.a

# Install Rust
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y --default-toolchain 1.87
ENV PATH="/root/.cargo/bin:${PATH}"

# Install Python 3.12
WORKDIR /tmp
RUN wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz \
    && tar -xf Python-3.12.0.tgz \
    && cd Python-3.12.0 \
    && ./configure --enable-optimizations --prefix=/usr/local \
    && make -j $(nproc) \
    && make altinstall

# Install zlib from source (for static linking)
COPY prebuilt-binaries/zlib-1.2.13 /root/zlib-1.2.13
COPY prebuilt-binaries/install-zlib.sh /root/install-zlib.sh
RUN cd /root && ./install-zlib.sh

# Copy LibAFL and proto
COPY prebuilt-binaries/LibAFL /root/LibAFL
COPY proto /libs/libatlantis/proto
ENV PROTO_DIR=/libs/libatlantis/proto

# Copy and build fuzzer
COPY prebuilt-binaries/fuzzer /root/fuzzer
WORKDIR /root/fuzzer
RUN ./build.sh

# Copy and build atlantis_cc (LibAFL compiler wrapper)
COPY prebuilt-binaries/atlantis_cc /root/atlantis_cc
WORKDIR /root/atlantis_cc
RUN ./build.sh

# Collect artifacts
RUN mkdir -p /artifacts && \
    cp /root/fuzzer/libfuzzer.so /artifacts/libfuzzer.so && \
    cp /root/atlantis_cc/cc_wrapper /artifacts/cc_wrapper && \
    cp /root/atlantis_cc/cxx_wrapper /artifacts/cxx_wrapper && \
    cp /usr/lib/libc++.a /artifacts/libc++.a

# Test stage - use pre-instrumented libxml2 target
FROM aixcc-afc/afc-libxml2-delta-01:latest

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=US \
    PYTHONUNBUFFERED=1

# Install tini for proper signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Copy Python from builder
COPY --from=builder /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=builder /usr/local/lib/libpython3.12.a /usr/local/lib/libpython3.12.a
RUN ldconfig

# Copy built fuzzer and compiler wrappers
COPY --from=builder /artifacts/libfuzzer.so /out/libfuzzer.so
COPY --from=builder /artifacts/cc_wrapper /usr/local/bin/cc_wrapper
COPY --from=builder /artifacts/cxx_wrapper /usr/local/bin/cxx_wrapper
COPY --from=builder /artifacts/libc++.a /usr/lib/libc++.a
RUN chmod +x /out/libfuzzer.so /usr/local/bin/cc_wrapper /usr/local/bin/cxx_wrapper

# Set up OSS-Fuzz style environment for building harness with LibAFL instrumentation
ENV OUT=/out \
    SRC=/src \
    WORK=/work \
    FUZZING_ENGINE=libafl \
    SANITIZER=address \
    ARCHITECTURE=x86_64 \
    LIB_FUZZING_ENGINE=/out/libfuzzer.so \
    CC=/usr/local/bin/cc_wrapper \
    CXX=/usr/local/bin/cxx_wrapper \
    CFLAGS="-fsanitize=address -fsanitize-address-use-after-scope" \
    CXXFLAGS="-fsanitize=address -fsanitize-address-use-after-scope -stdlib=libc++ -lpthread" \
    LDFLAGS="-lpthread" \
    ATLANTIS_CC_INSTRUMENTATION_MODE=libafl \
    LIBFUZZER_PATH=/out/libfuzzer.so \
    CP_HARNESS="api:html:lint:reader:regexp:schema:uri:valid:xinclude:xml:xpath"

# Build the libxml2 harness
WORKDIR /src/libxml2
RUN ./fuzz/oss-fuzz-build.sh

# Copy run script and set up seed corpus
RUN mkdir -p /crs /seed_share_dir /artifacts/corpus /artifacts/povs /out/initial_corpus
COPY run.py /crs/run.py

# Unzip the xml seed corpus for initial fuzzing
RUN cd /out && unzip -o xml_seed_corpus.zip -d initial_corpus

# Debug: list available harnesses
RUN echo "=== Available harnesses in /out ===" && ls -la /out/

ENV CPUSET_CPUS=0

# Default entrypoint - pass harness name as argument
# Example: docker run <image> xml
ENTRYPOINT ["/usr/bin/tini", "--", "python3.12", "/crs/run.py"]
