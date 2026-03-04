# atlantis-c-libafl-snapshot oss-crs-6 Integration Progress

**Date:** 2026-03-03
**Status:** Build working, DeepGen debugging in progress

---

## ✅ Completed

### 1. oss-crs-6 Integration (Commit: 940905a)
- **Created:** `oss-crs/crs.yaml` - CRS configuration (ASAN-only)
- **Created:** `oss-crs/dockerfiles/builder.Dockerfile` - Builder image with `CMD ["compile_target"]`
- **Created:** `oss-crs/dockerfiles/runner.Dockerfile` - Runner image with DeepGen dependencies
- **Created:** `bin/compile_target` - Build entry point with libCRS submission
- **Created:** `bin/run_fuzzer` - Runtime entry point
- **Enhanced:** `build.py` - Added comprehensive logging for all build phases
- **Enhanced:** `run.py` - Use OSS_CRS_CPUSET, improved error messages
- **Enhanced:** `start.sh` - Auto-detect cpuset, strip quotes, convert ranges, map OSS_CRS_LLM_* env vars
- **Enhanced:** `deepgen_service/simple_service.py` - Use OSS_CRS_LLM_API_URL/KEY env vars

### 2. Build Output Flow Documentation
- **Created:** `OUTPUT_FLOW.md` - Complete documentation of build phases and output locations

### 3. Key Fixes Applied

#### Builder Dockerfile
- Added `CMD ["compile_target"]` so container executes build script automatically
- Without this, outputs were never submitted to libCRS

#### CPU Allocation (cpuset)
- **Issue:** Docker doesn't set `CPUSET_CPUS` env var automatically
- **Solution:** Read `OSS_CRS_CPUSET` (set by oss-crs-6)
- **Fallback:** Auto-detect from `/proc/self/status` if not set
- **Quote handling:** Strip quotes from `"2-16"` → `2-16`
- **Range conversion:** Convert `2-16` → `2,3,4,5,...,16`
- **CPU splitting:**
  - 1 core: Fuzzer only, no DeepGen
  - 2-3 cores: 1 for DeepGen, rest for fuzzer
  - 4+ cores: 2 for DeepGen, rest for fuzzer

#### Build Process
- Added `cd /` before libCRS submission (fixes rsync getcwd error)
- Modified jq build.sh to skip git submodule if oniguruma already present
- Downloaded oniguruma source directly to bypass GitHub 500 errors

### 4. Verified Working
- ✅ Build completes successfully (jq with 7 fuzzers)
- ✅ Outputs submitted to libCRS:
  - `config/config.json` - Harness → source mappings
  - `config/project.tar.gz` - Filtered source archive (1.6M)
  - `libafl/build/` - 7 fuzzer binaries + libfuzzer.so (197M)
  - `libafl/artifacts/` - cc_wrapper, cxx_wrapper, libc++.a (140M)
- ✅ Fuzzer runs and finds crashes (4+ crashes, 6% coverage)
- ✅ CPU allocation working (fuzzer gets 13/15 cores)

---

## 🔧 In Progress

### DeepGen Service Debugging
**Problem:** DeepGen starts but crashes silently during initialization

**Symptoms:**
- Logs show: "Initializing DeepGenEngine..."
- Missing: "DeepGenEngine initialized and ready"
- Process (PID 39) starts then disappears
- Fuzzer continues running without LLM seed generation

**Root Cause:** Crash in async `lifespan()` function during:
- `DeepGenEngine()` instantiation, OR
- `await engine.__aenter__()`, OR
- `asyncio.create_task(engine.run())`

**Debugging Steps Taken:**
1. Added try-catch around `uvicorn.run()` - didn't catch crash (crash is before uvicorn starts)
2. **Current:** Adding detailed logging in lifespan function:
   - Log before/after `DeepGenEngine()` creation
   - Log before/after `await engine.__aenter__()`
   - Will show exact crash location

**Files Modified (staged, not committed):**
- `deepgen_service/simple_service.py` - Enhanced lifespan logging
- `OUTPUT_FLOW.md` - Build flow documentation

---

## 📋 Next Steps

1. **Commit current changes:**
   ```bash
   git commit -m "feat: add detailed DeepGen initialization logging"
   ```

2. **Rebuild runner image:**
   ```bash
   cd ~/post/oss-crs-6
   uv run oss-crs prepare --compose-file example/atlantis-c-deepgen/compose.yaml
   ```

3. **Run fuzzer and check logs:**
   ```bash
   uv run oss-crs run \
     --compose-file example/atlantis-c-deepgen/compose.yaml \
     --fuzz-proj-path ~/post/oss-fuzz-vanilla/projects/jq \
     --target-source-path ~/post/clone/jq \
     --target-harness jq_fuzz_execute
   ```

4. **Investigate DeepGen crash with new logs:**
   - Look for exact point of failure
   - Check if it's engine creation, __aenter__, or task creation
   - Debug based on error message

5. **Potential issues to investigate:**
   - Shared memory configuration
   - ZMQ socket binding
   - CPU affinity setting
   - Missing dependencies
   - Resource limits (memory, file descriptors)

---

## 📊 Configuration

### Supported Features (crs.yaml)
- **Languages:** C, C++
- **Sanitizer:** Address only (ASAN)
- **Architecture:** x86_64
- **Mode:** Full, Delta

### Resource Allocation (compose.yaml)
```yaml
oss_crs_infra:
  cpuset: "0-1"      # 2 cores
  memory: "8G"

atlantis-c-deepgen:
  cpuset: "2-16"     # 15 cores → 2 for DeepGen, 13 for fuzzer
  memory: "64G"
  llm_budget: 100
```

### Environment Variables
- `OSS_CRS_CPUSET` - CPU set from oss-crs-6
- `OSS_CRS_LLM_API_URL` - LiteLLM API endpoint
- `OSS_CRS_LLM_API_KEY` - LiteLLM API key
- `LLM_MODELS` - Model selection (default: claude-sonnet-4-20250514:1)

---

## 🐛 Known Issues

1. **DeepGen crashes during initialization** (debugging in progress)
2. **LibAFL only supports AddressSanitizer** - MSAN, UBSAN, TSAN not supported
3. **GitHub submodule fetches can fail** - workaround: pre-download or modify build.sh

---

## 📝 Files Modified

### New Files
- `oss-crs/crs.yaml`
- `oss-crs/dockerfiles/builder.Dockerfile`
- `oss-crs/dockerfiles/runner.Dockerfile`
- `bin/compile_target`
- `bin/run_fuzzer`
- `OUTPUT_FLOW.md`

### Modified Files
- `build.py` - Logging
- `run.py` - OSS_CRS_CPUSET support
- `start.sh` - Cpuset detection, quote stripping, range conversion
- `deepgen_service/simple_service.py` - OSS_CRS env vars, lifespan logging

### External Modifications
- `/home/andrew/post/oss-fuzz-vanilla/projects/jq/build.sh` - Skip git submodule if present
- `/home/andrew/post/clone/jq/vendor/oniguruma/` - Downloaded source directly

---

## 🎯 Success Metrics

- ✅ Build succeeds
- ✅ Outputs submitted to correct libCRS paths
- ✅ Fuzzer runs (6K-26K exec/sec depending on cores)
- ✅ Coverage increases (6%+)
- ✅ Crashes found (1-4 crashes)
- ❌ DeepGen LLM seed generation (not working yet)

---

**Last Updated:** 2026-03-03 (Context at 94%)
