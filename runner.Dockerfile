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

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=US \
    PYTHONUNBUFFERED=1

COPY --from=builder /usr/local /usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git ripgrep tzdata tini wget unzip protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# For Python
RUN ldconfig

# Run script entrypoint
RUN mkdir /crs
COPY ./run.py /crs/run.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/bin/tini", "--", "python3.12", "/crs/run.py"]

# NOTE this is a nicer debugging setup by passing stuff via env vars, but not that clean
#      e.g. FUZZER env var
# ENTRYPOINT ["/usr/bin/tini", "--"]
# CMD ["python3", "/crs/run.py"]
