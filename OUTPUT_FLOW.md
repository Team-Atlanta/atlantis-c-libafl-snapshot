# atlantis-c-libafl-snapshot Output Flow

This document traces where outputs are stored during the build process.

## Build Architecture

The build process creates **two copies** of the source code and builds them separately:

1. **config_gen build** (`/src-config_gen`) - Lightweight build for generating config.json
2. **libafl build** (`/src-libafl`) - Full LibAFL-instrumented build for fuzzing

## Phase 1: setup_src_copies()

**What happens:**
```
/src → /src-libafl     (full copy)
/src → /src-config_gen (full copy)
/src → /src-backup     (rename original)
```

**Output locations:**
- `/src-libafl/` - Source copy for LibAFL build
- `/src-config_gen/` - Source copy for config_gen build
- `/src-backup/` - Original source (restored at end)

---

## Phase 2: build_config_gen()

**What happens:**
1. Symlink: `/src` → `/src-config_gen`
2. Run: `python3.12 /crs/config_gen/bb.py`
   - Internally calls: `compile` (OSS-Fuzz build script)
   - Scans `/out` for harness binaries
   - Uses `llvm-symbolizer` to map harnesses to source files
   - Creates config.json with harness → source file mapping
   - Creates project.tar.gz with filtered source code

**Output locations:**
- `/out/<harness_name>*` - Fuzzer binaries (from `compile` script)
  - Example: `/out/jq_fuzz_parse`, `/out/jq_fuzz_compile`, etc.
- `/artifacts/config.json` - Harness to source file mapping
  - Format: `{"harness_name": "/src/path/to/file.c", ...}`
- `/artifacts/project.tar.gz` - Compressed source archive
  - Contains: `/src-config_gen/` filtered for C/C++ files only

**Key insight:**
The binaries in `/out` from this phase are **temporary** and will be overwritten by Phase 3.

---

## Phase 3: build_libafl()

**What happens:**
1. Unlink `/src` symlink
2. Symlink: `/src` → `/src-libafl`
3. Copy compiler wrappers:
   - `/data/artifacts/cc_wrapper` → `/work/cc_wrapper`
   - `/data/artifacts/cxx_wrapper` → `/work/cxx_wrapper`
4. Create wrapper scripts:
   - `/work/cc_wrapper_libafl` - Injects `ATLANTIS_CC_INSTRUMENTATION_MODE=libafl`
   - `/work/cxx_wrapper_libafl` - Injects `ATLANTIS_CC_INSTRUMENTATION_MODE=libafl`
5. Copy LibAFL runtime:
   - `/data/artifacts/libfuzzer.so` → `/out/libfuzzer.so`
6. Run: `compile` with custom CC/CXX pointing to wrapper scripts

**Output locations:**
- `/out/<harness_name>*` - **LibAFL-instrumented fuzzer binaries** (overwrites Phase 2)
  - Example: `/out/jq_fuzz_parse`, `/out/jq_fuzz_compile`, etc.
  - These binaries contain LibAFL snapshot instrumentation
- `/out/libfuzzer.so` - LibAFL fuzzer runtime library
- `/artifacts/config.json` - **Preserved from Phase 2** (not overwritten)
- `/artifacts/project.tar.gz` - **Preserved from Phase 2** (not overwritten)

---

## Phase 4: restore_src_backup()

**What happens:**
```
/src-backup → /src (restore original)
```

Symlink is removed, original source is restored.

---

## Final Submission (bin/compile_target)

After `build.py` completes, `bin/compile_target` submits outputs to libCRS:

```bash
libCRS submit-build-output /out libafl/build
libCRS submit-build-output /data/artifacts libafl/artifacts
libCRS submit-build-output /artifacts config
```

**Mapping:**
- `/out/` → **`libafl/build/`** in libCRS storage
  - Contains: LibAFL-instrumented harness binaries + libfuzzer.so
- `/data/artifacts/` → **`libafl/artifacts/`** in libCRS storage
  - Contains: cc_wrapper, cxx_wrapper, libc++.a (pre-built from base image)
- `/artifacts/` → **`config/`** in libCRS storage
  - Contains: config.json, project.tar.gz

---

## Summary: Where to Find Each Output

| Artifact | Build Location | libCRS Location | Created By |
|----------|---------------|----------------|------------|
| Fuzzer binaries (instrumented) | `/out/<harness>*` | `libafl/build/<harness>*` | build_libafl() |
| libfuzzer.so | `/out/libfuzzer.so` | `libafl/build/libfuzzer.so` | build_libafl() |
| config.json | `/artifacts/config.json` | `config/config.json` | build_config_gen() |
| project.tar.gz | `/artifacts/project.tar.gz` | `config/project.tar.gz` | build_config_gen() |
| cc_wrapper | `/data/artifacts/cc_wrapper` | `libafl/artifacts/cc_wrapper` | Pre-built |
| cxx_wrapper | `/data/artifacts/cxx_wrapper` | `libafl/artifacts/cxx_wrapper` | Pre-built |
| libc++.a | `/data/artifacts/libc++.a` | `libafl/artifacts/libc++.a` | Pre-built |

---

## Sanity Checks

### ✅ config.json should contain harness → source mappings
```json
{
  "jq_fuzz_parse": "/src/tests/jq_fuzz_parse.c",
  "jq_fuzz_compile": "/src/tests/jq_fuzz_compile.c"
}
```

### ✅ /out binaries should be LibAFL-instrumented
- Built with `ATLANTIS_CC_INSTRUMENTATION_MODE=libafl`
- Linked against `/out/libfuzzer.so`
- Contain snapshot instrumentation hooks

### ✅ /artifacts should contain exactly 2 files
```
/artifacts/config.json
/artifacts/project.tar.gz
```

### ✅ project.tar.gz should contain filtered source
- Only C/C++ files, headers, build files
- Excludes fuzzer directories (libfuzzer/, aflplusplus/, honggfuzz/)
- Archive structure: `src/` (from /src-config_gen)

### ⚠️ Potential Issue: /out overwrite
The config_gen phase writes binaries to `/out`, then libafl phase **overwrites them**.
This is **intentional** - we only need the LibAFL-instrumented binaries for fuzzing.
The config_gen binaries are discarded after config.json is created.

---

## Debugging Tips

If build fails, check:

1. **After config_gen phase:**
   - Does `/artifacts/config.json` exist?
   - Does `/artifacts/project.tar.gz` exist?
   - Are there binaries in `/out`?

2. **After libafl phase:**
   - Are binaries in `/out` re-created?
   - Does `/out/libfuzzer.so` exist?
   - Run `ldd /out/<harness>` - should show libfuzzer.so dependency

3. **Environment variables:**
   - config_gen: Uses default compile environment
   - libafl: Sets `CC=/work/cc_wrapper_libafl`, `CXX=/work/cxx_wrapper_libafl`

4. **Check wrapper scripts:**
   ```bash
   cat /work/cc_wrapper_libafl
   # Should contain: ATLANTIS_CC_INSTRUMENTATION_MODE=libafl
   ```
