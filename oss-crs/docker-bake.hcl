# =============================================================================
# atlantis-c-libafl-snapshot Docker Bake Configuration
# =============================================================================
#
# Moves heavy compilation (LLVM, Rust, Python, LibAFL fuzzer, cc_wrapper,
# code-browser-server) into prepare-phase images so the build-target and
# run phases only copy pre-built artifacts.
#
# Build order (with parallelism):
#   parallel:  libafl-toolchain ──► libafl-archive
#              python-builder   ──► runner-base
#              rust-builder     ──┘
#
# Usage:
#   docker buildx bake prepare          # Build all prepare images locally
#   docker buildx bake --print          # Show build plan
#
# =============================================================================

# -----------------------------------------------------------------------------
# Groups
# -----------------------------------------------------------------------------

group "default" {
  targets = ["prepare"]
}

group "prepare" {
  targets = ["libafl-archive", "runner-base"]
}

# -----------------------------------------------------------------------------
# Prepare Phase Targets
# -----------------------------------------------------------------------------

# LibAFL toolchain: Ubuntu 20.04 + LLVM 18 + Rust + Python 3.12 + zlib
# Compiles fuzzer (libfuzzer.so) and cc_wrapper/cxx_wrapper
target "libafl-toolchain" {
  context    = "."
  dockerfile = "oss-crs/dockerfiles/libafl-toolchain.Dockerfile"
  tags       = ["libafl-toolchain:latest"]
}

# Archive image: thin container with just the build artifacts
# Extracts libfuzzer.so, cc_wrapper, cxx_wrapper, libc++.a, Python 3.12
target "libafl-archive" {
  context    = "."
  dockerfile = "oss-crs/dockerfiles/libafl-archive.Dockerfile"
  tags       = ["libafl-archive:latest"]
  contexts = {
    libafl-toolchain = "target:libafl-toolchain"
  }
}

# Runner base: Python 3.12 + code-browser-server + all pip dependencies
# Pre-installs everything the runner needs so runner.Dockerfile just COPYs
target "runner-base" {
  context    = "."
  dockerfile = "oss-crs/dockerfiles/runner-base.Dockerfile"
  tags       = ["runner-base:latest"]
}
