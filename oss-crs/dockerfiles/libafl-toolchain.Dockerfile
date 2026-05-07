# =============================================================================
# LibAFL Toolchain (Prepare Phase)
# =============================================================================
# Compiles LibAFL fuzzer, cc/cxx wrappers, and Python 3.12 on Ubuntu 20.04
# (libc-2.31 for forward-compatibility with newer libc versions).
#
# Output artifacts at /artifacts/:
#   - libfuzzer.so      (LibAFL fuzzer runtime)
#   - cc_wrapper         (ASAN-instrumented compiler wrapper)
#   - cxx_wrapper        (ASAN-instrumented compiler wrapper)
#   - libc++.a           (static libc++)
#
# Python 3.12 installed at /usr/local/{bin,lib,include}
# =============================================================================

# libc-2.31 / Don't change this. Using old ubuntu/libc is for compatibility with subsequent libc versions
FROM ubuntu:20.04@sha256:fa17826afb526a9fc7250e0fbcbfd18d03fe7a54849472f86879d8bf562c629e

ENV TZ=US \
    DEBIAN_FRONTEND=noninteractive

ARG PREBUILT_BINARIES=./prebuilt-binaries

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
    bsdmainutils \
    netcat \
    ninja-build \
    gnupg \
    libzstd-dev \
    colordiff \
    xxd \
    wdiff \
    zlib1g-dev \
    libssl-dev \
    pkg-config

WORKDIR /root

# Install zlib from source
COPY $PREBUILT_BINARIES/zlib-1.2.13 /root/zlib-1.2.13
COPY $PREBUILT_BINARIES/install-zlib.sh /root/install-zlib.sh
RUN ./install-zlib.sh

# Install yq
ARG YQ_VERSION=4.43.1
ARG YQ_BINARY=yq_linux_amd64
RUN wget -q https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/${YQ_BINARY} -O /usr/bin/yq && \
    chmod +x /usr/bin/yq

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

# Build LibAFL fuzzer
COPY $PREBUILT_BINARIES/LibAFL /root/LibAFL
COPY proto /libs/libatlantis/proto
ENV PROTO_DIR=/libs/libatlantis/proto

COPY $PREBUILT_BINARIES/fuzzer /root/fuzzer
WORKDIR /root/fuzzer
RUN ./build.sh

# Build cc/cxx wrappers
COPY $PREBUILT_BINARIES/atlantis_cc /root/atlantis_cc
WORKDIR /root/atlantis_cc
RUN ./build.sh

# Collect artifacts
RUN mkdir -p /artifacts && \
    cp /root/fuzzer/libfuzzer.so /artifacts/libfuzzer.so && \
    cp /root/atlantis_cc/cc_wrapper /artifacts/cc_wrapper && \
    cp /root/atlantis_cc/cxx_wrapper /artifacts/cxx_wrapper && \
    cp /usr/lib/libc++.a /artifacts/libc++.a
