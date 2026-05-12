# =============================================================================
# CRS Runner Dockerfile
# =============================================================================
# RUN phase: Uses pre-built atlantis-c-runner-base from prepare phase.
# Python 3.12, code-browser-server, and all pip dependencies are already
# installed — this just copies the CRS scripts and deepgen service.
# =============================================================================

FROM atlantis-c-runner-base:latest

# Install libCRS (injected by oss-crs at build time)
COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

# Copy deepgen service
COPY ./deepgen_service /crs/deepgen_service

# Run script entrypoint
RUN mkdir -p /crs
COPY ./run.py /crs/run.py
COPY ./start.sh /crs/start.sh
RUN chmod +x /crs/start.sh

COPY ./bin/run_fuzzer /usr/local/bin/run_fuzzer
RUN chmod +x /usr/local/bin/run_fuzzer

ENV PYTHONUNBUFFERED=1
# Start both deepgen_service and fuzzer via run_fuzzer -> start.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/run_fuzzer"]
