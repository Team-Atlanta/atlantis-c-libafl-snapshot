FROM ubuntu:20.04 AS python-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    build-essential wget libssl-dev zlib1g-dev \
    libbz2-dev libreadline-dev libsqlite3-dev \
    libncursesw5-dev xz-utils libffi-dev liblzma-dev

WORKDIR /tmp
RUN wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz \
    && tar -xf Python-3.12.0.tgz \
    && cd Python-3.12.0 \
    && ./configure --enable-optimizations --prefix=/usr/local \
    && make -j $(nproc) \
    && make altinstall

# Build code-browser-server from Rust source (using Ubuntu 20.04 for glibc compatibility)
FROM ubuntu:20.04 AS rust-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl build-essential pkg-config unzip \
    clang libclang-dev

# Install protoc 3.19 (supports proto3 optional fields)
RUN curl -LO https://github.com/protocolbuffers/protobuf/releases/download/v3.19.6/protoc-3.19.6-linux-x86_64.zip && \
    unzip protoc-3.19.6-linux-x86_64.zip -d /usr/local && \
    rm protoc-3.19.6-linux-x86_64.zip

# Install rustup and stable toolchain
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.83
ENV PATH="/root/.cargo/bin:${PATH}"

COPY ./libs/userspace-code-browser /build/userspace-code-browser
WORKDIR /build/userspace-code-browser

RUN cargo build --release --bin code-browser-server && \
    cp target/release/code-browser-server /usr/local/bin/

# Base runner
FROM gcr.io/oss-fuzz-base/base-runner

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=US \
    PYTHONUNBUFFERED=1

COPY --from=python-builder /usr/local /usr/local
COPY --from=rust-builder /usr/local/bin/code-browser-server /usr/local/bin/code-browser-server

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git ripgrep tzdata tini wget unzip protobuf-compiler \
       libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# For Python
RUN ldconfig

# ============================================================================
# DeepGen dependencies
# ============================================================================

# Copy libs (libDeepGen, libAgents, and their local dependencies)
COPY ./libs/libDeepGen /crs/libs/libDeepGen
COPY ./libs/libAgents /crs/libs/libAgents
COPY ./libs/claude-code-sdk-python /crs/libs/claude-code-sdk-python
COPY ./libs/userspace-code-browser /crs/libs/userspace-code-browser

# Install deepgen service requirements (heavy package list for fuzzing)
COPY ./deepgen_service/requirements.txt /tmp/deepgen_requirements.txt
RUN python3.12 -m pip install --no-cache-dir -r /tmp/deepgen_requirements.txt

# Install grammarinator from public git (required by libDeepGen)
RUN python3.12 -m pip install --no-cache-dir \
    "grammarinator @ git+https://github.com/renatahodovan/grammarinator.git@68b0350"

# Install atomics and pyzmq (required by libDeepGen, not in requirements.txt)
RUN python3.12 -m pip install --no-cache-dir atomics>=1.0.3 pyzmq>=26.4.0

# Install libAgents dependencies not in requirements.txt
RUN python3.12 -m pip install --no-cache-dir \
    aider-chat \
    aiofiles \
    diskcache \
    filelock \
    google-genai \
    litellm \
    pocketflow \
    "tree-sitter==0.24.0" \
    "tree-sitter-cpp==0.23.4" \
    grpcio-tools \
    protoletariat \
    mcp

# Build code-browser-client proto files and install manually (avoid setup.py proto build)
WORKDIR /crs/libs/userspace-code-browser/python-client
RUN python3.12 -m grpc_tools.protoc \
        -I../proto \
        --python_out=code_browser_client \
        --grpc_python_out=code_browser_client \
        ../proto/browser.proto && \
    sed -i 's/^import browser_pb2/from . import browser_pb2/' code_browser_client/browser_pb2_grpc.py && \
    cp -r code_browser_client /usr/local/lib/python3.12/site-packages/
WORKDIR /

# Install claude-agent-sdk
RUN python3.12 -m pip install --no-cache-dir /crs/libs/claude-code-sdk-python

# Install libAgents (use --no-deps to avoid git fetches)
RUN python3.12 -m pip install --no-cache-dir --no-deps /crs/libs/libAgents

# Install libDeepGen (use --no-deps to avoid fetching libagents from git)
RUN python3.12 -m pip install --no-cache-dir --no-deps /crs/libs/libDeepGen

# Copy deepgen service
COPY ./deepgen_service /crs/deepgen_service

# ============================================================================
# Run script entrypoint
# ============================================================================
RUN mkdir -p /crs
COPY ./run.py /crs/run.py
COPY ./start.sh /crs/start.sh
RUN chmod +x /crs/start.sh

ENV PYTHONUNBUFFERED=1
# Start both deepgen_service and fuzzer via start.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/crs/start.sh"]
