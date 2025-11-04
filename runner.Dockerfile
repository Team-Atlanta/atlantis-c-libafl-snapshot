# ARG parent_image

# DeepGen
FROM openjdk:17-jdk AS java17

FROM nixos/nix:2.28.3 AS nix-builder

WORKDIR /app
RUN nix-env -iA nixpkgs.docker
RUN nix-env -iA nixpkgs.glibc
RUN nix-env -iA nixpkgs.patchelf
RUN nix-env -iA nixpkgs.python3


COPY libs/userspace-code-browser/Cargo.toml /app/
COPY libs/userspace-code-browser/Cargo.lock /app/
COPY libs/userspace-code-browser/flake.nix /app/
COPY libs/userspace-code-browser/flake.lock /app/
COPY libs/userspace-code-browser/deny.toml /app/
COPY libs/userspace-code-browser/taplo.toml /app/
COPY libs/userspace-code-browser/build.rs /app/
COPY libs/userspace-code-browser/proto /app/proto
COPY libs/userspace-code-browser/src /app/src
COPY deepgen_service/patch.py /app/patch.py
RUN chmod +x /app/patch.py

ENV PATH="/root/.cargo/bin:${PATH}"
RUN nix develop --extra-experimental-features 'flakes nix-command' -c cargo install --path . --locked
RUN /app/patch.py /root/.cargo/bin/code-browser-server

FROM ubuntu:20.04 as builder

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

# Base runner
FROM gcr.io/oss-fuzz-base/base-runner

# COPY --from=$parent_image /src /src

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=US \
    JAVA_HOME=/usr/local/openjdk-17 \
    JAVA_15_HOME=/usr/local/openjdk-15 \
    PATH="/usr/local/openjdk-17/bin:/usr/local/openjdk-15/bin:/root/.local/bin:/usr/local/bin:/venv-deepgen/bin:/deepgen_service:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=java17 /usr/local/openjdk-17 /usr/local/openjdk-17
COPY --from=nix-builder /app/out /deepgen_service/
COPY --from=builder /usr/local /usr/local
COPY ./libs/userspace-code-browser/ /libs/userspace-code-browser/
COPY ./libs/claude-code-sdk-python/ /libs/claude-code-sdk-python/
COPY ./libs/libAgents /libs/libAgents
COPY ./libs/libDeepGen /libs/libDeepGen
COPY ./deepgen_service /deepgen_service

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git ripgrep tzdata tini wget unzip protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# For Python
RUN ldconfig

# Updated protoc
WORKDIR /tmp
RUN curl -LO "https://github.com/protocolbuffers/protobuf/releases/download/v30.2/protoc-30.2-linux-x86_64.zip"
RUN unzip protoc-30.2-linux-x86_64.zip -d /usr/local

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g npm@latest \
    && npm install -g @openai/codex \
    && npm install -g @anthropic-ai/claude-code

# NOTE base-runner uses Python 3.11.13
RUN python3.12 -m venv /venv-deepgen

# Install libs
RUN /venv-deepgen/bin/pip3 install --no-cache-dir \
    setuptools \
    mypy-protobuf==3.6.0 \
    hatch
RUN . /venv-deepgen/bin/activate && pip3 install --no-cache-dir /libs/userspace-code-browser/python-client
RUN /venv-deepgen/bin/pip3 install --no-cache-dir \
      -r /deepgen_service/requirements.txt \
      /libs/claude-code-sdk-python \
      "anyio[trio]>=4.0.0" \
      apscheduler>=3.11.0 \
      aider-chat>=0.80.2 \
      aiofiles>=24.1.0 \
      dotenv>=0.9.9 \
      filelock>=3.12.0 \
      google-genai>=1.9.0 \
      litellm>=1.65.1 \
      pocketflow>=0.0.2 \
      psutil>=5.9.0 \
      pydantic>=2.11.1 \
      pytest-asyncio>=0.26.0 \
      tree-sitter==0.24.0 \
      tree-sitter-cpp==0.23.4 \
  && cd /libs/libAgents \
  && /venv-deepgen/bin/pip3 install --no-deps . \
  && cd /libs/libDeepGen \
  && /venv-deepgen/bin/pip3 install --no-cache-dir \
      atomics>=1.0.3 \
      protobuf>=5.29.0 \
      psutil>=7.0.0 \
      zmq>=pyzmq>=26.4.0 \
  && /venv-deepgen/bin/pip3 install --no-cache-dir \
      git+https://github.com/renatahodovan/grammarinator.git@68b0350 \
  && /venv-deepgen/bin/pip3 install --no-deps .

# TODO install deepgen service
  # && cd /deepgen_service \
  # && /venv-deepgen/bin/pip3 install --no-cache-dir .

# Run script entrypoint
RUN mkdir /crs
COPY ./run.py /crs/run.py
COPY ./haiku.py /crs/haiku.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/bin/tini", "--", "python3.12", "/crs/run.py"]

# NOTE this is a nicer debugging setup by passing stuff via env vars, but not that clean
#      e.g. FUZZER env var
# ENTRYPOINT ["/usr/bin/tini", "--"]
# CMD ["python3", "/crs/run.py"]
