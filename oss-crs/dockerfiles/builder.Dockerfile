ARG target_base_image

# libc-2.31 / Don't change this. Using old ubuntu/libc is for compatibility with subsequent libc versions
FROM ubuntu:20.04@sha256:fa17826afb526a9fc7250e0fbcbfd18d03fe7a54849472f86879d8bf562c629e AS base

ENV TZ=US \
    DEBIAN_FRONTEND=noninteractive

# TODO host it? or OK to put in local dir
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

# Install glib from source
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

# Install LLVM: for now, let's improvise to 18
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

# Install python
WORKDIR /tmp
RUN wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz \
    && tar -xf Python-3.12.0.tgz \
    && cd Python-3.12.0 \
    && ./configure --enable-optimizations --prefix=/usr/local \
    && make -j $(nproc) \
    && make altinstall

COPY $PREBUILT_BINARIES/LibAFL /root/LibAFL
COPY proto /libs/libatlantis/proto
ENV PROTO_DIR=/libs/libatlantis/proto

COPY $PREBUILT_BINARIES/fuzzer /root/fuzzer
WORKDIR /root/fuzzer
RUN ./build.sh

COPY $PREBUILT_BINARIES/atlantis_cc /root/atlantis_cc
WORKDIR /root/atlantis_cc
RUN ./build.sh

RUN mkdir -p /artifacts && \
    cp /root/fuzzer/libfuzzer.so /artifacts/libfuzzer.so && \
    cp /root/atlantis_cc/cc_wrapper /artifacts/cc_wrapper && \
    cp /root/atlantis_cc/cxx_wrapper /artifacts/cxx_wrapper && \
    cp /usr/lib/libc++.a /artifacts/libc++.a

# Stage 2: Final builder image
FROM $target_base_image

# Install libCRS
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

# Copy artifacts. Do not recall why we use /data/artifacts, but let's not break it
RUN mkdir -p /data /crs
COPY --from=base /artifacts /data/artifacts
COPY --from=base /usr/local/bin /usr/local/bin
COPY --from=base /usr/local/lib /usr/local/lib
COPY --from=base /usr/local/include/python3.12 /usr/local/include/python3.12
RUN ldconfig # for python

# Set up venv and install openai
RUN python3.12 -m venv /crs/venv && \
    /crs/venv/bin/pip install --no-cache-dir openai requests

COPY ./config_gen /crs/config_gen
COPY ./build.py /crs/build.py
COPY ./bin/compile_target /usr/local/bin/compile_target
RUN chmod +x /usr/local/bin/compile_target

CMD ["compile_target"]
