---
name: run-oss-crs
description: Build and run atlantis-c-libafl + deepgen through oss-crs-2
---

# OSS-CRS Build & Run for Atlantis-C-LibAFL + DeepGen

This skill builds and runs the fuzzer through oss-crs-2.

## Working Directory

All commands run from `$OSS_CRS`.
If environment variable isn't set, try the following:
- `~/post/oss-crs-2`
- `~/projects/oss-crs`
or crawl to see if you can find a dir.

For oss-fuzz directory, `$OSS_FUZZ`.
If environment variable isn't set, try the following:
- `~/post/oss-fuzz-clean`
- `~/projects/oss-fuzz`

For a cloned project repo, `$PROJECT_CLONE`.
If environment variable isn't set, try the following:
- `~/post/clone`
- `~/clone`

## Build Command

Build the project using libafl's instrumentation:

```bash
cd $OSS_CRS && uv run oss-bugfind-crs build \
    --project-image-prefix aixcc-afc \
    --oss-fuzz-dir $OSS_FUZZ \
    example_configs/atlantis-c-deepgen \
    aixcc/c/sanity-mock-c-delta-01 \
    $PROJECT_CLONE/mock-c
```

## Run Command

Run atlantis-c-libafl + deepgen (requires `.env` with LITELLM_URL and LITELLM_KEY):

```bash
cd $OSS_CRS && source .env && uv run oss-bugfind-crs run \
    --external-litellm \
    example_configs/atlantis-c-deepgen \
    aixcc/c/sanity-mock-c-delta-01 \
    fuzz_process_input_header
```

## Run with Diff (Delta Mode)

For bug-finding with a known vulnerable diff:

```bash
cd $OSS_CRS && source .env && uv run oss-bugfind-crs run \
    --external-litellm \
    --diff $OSS_FUZZ/projects/aixcc/c/sanity-mock-c-delta-01/.aixcc/ref.diff \
    example_configs/atlantis-c-deepgen \
    aixcc/c/sanity-mock-c-delta-01 \
    fuzz_process_input_header
```

When `--diff` is provided:
- The diff is mounted at `/ref.diff` in the container
- DeepGen automatically triggers `DiffAnalysisTask` for all harnesses
- LLM analyzes the diff to identify vulnerabilities
- Seeds are generated targeting the changed code paths

## Usage

- `/run-oss-crs` or `/run-oss-crs build run` - Run both build and run sequentially
- `/run-oss-crs build` - Just build
- `/run-oss-crs run` - Just run (assumes already built)

## Environment Variables

The `.env` file in oss-crs-2 should contain:
- `LITELLM_URL` - LiteLLM proxy URL
- `LITELLM_KEY` - LiteLLM API key

## Alternative Target: libxml2

Build libxml2:

```bash
cd $OSS_CRS && uv run oss-bugfind-crs build \
    --project-image-prefix aixcc-afc \
    --oss-fuzz-dir $OSS_FUZZ \
    example_configs/atlantis-c-deepgen \
    aixcc/c/afc-libxml2-delta-01 \
    $PROJECT_CLONE/official-afc-libxml2
```

Run libxml2 with html harness:

```bash
cd $OSS_CRS && source .env && uv run oss-bugfind-crs run \
    --external-litellm \
    example_configs/atlantis-c-deepgen \
    aixcc/c/afc-libxml2-delta-01 \
    html
```

## Parameters

- `example_configs/atlantis-c-deepgen` - CRS config pointing to atlantis-c-libafl-snapshot
- `aixcc/c/sanity-mock-c-delta-01` - Target project config (mock-c)
- `aixcc/c/afc-libxml2-delta-01` - Target project config (libxml2)
- `fuzz_process_input_header` - Harness name for mock-c
- `html` - Harness name for libxml2
- `--external-litellm` - Use external LiteLLM proxy
- `--project-image-prefix aixcc-afc` - Docker image prefix
- `--oss-fuzz-dir` - Path to clean oss-fuzz checkout

## Interactive Run Behavior

When running the fuzzer:
1. Start the run command in the background
2. Wait for initial output (30-60 seconds) to confirm startup success
3. Look for these success indicators in the logs:
   - "DeepGen Service is running"
   - "LLM-powered seed generation enabled"
   - "exec_sec=" stats appearing (fuzzer is running)
   - "crashes=" count (if finding bugs)
4. After confirming success OR after timeout (2 minutes), ask user:
   - "Continue running?" - keep fuzzer going
   - "Stop now?" - stop the container and cleanup
5. To stop the fuzzer, kill the `oss-bugfind-crs` process (NOT the docker containers directly):
   ```bash
   pkill -f "oss-bugfind-crs run"
   ```
   This automatically cleans up containers properly.

## Stopping the Fuzzer

**IMPORTANT:** Always stop by killing the `oss-bugfind-crs` process, not the docker containers:
```bash
pkill -f "oss-bugfind-crs run"
```

The process handles container cleanup automatically. Do NOT run `docker stop` on crs-run containers directly.

## Emergency Cleanup

Only if the process was killed improperly and containers are orphaned:
```bash
docker ps --filter "name=crs-run" -q | xargs -r docker stop
docker ps --filter "name=crs-run" -aq | xargs -r docker rm
```

## Checking Agent/LLM Success

### DeepGen Service Endpoints (inside container)

The service exposes several endpoints on port 8000:

```bash
# Get container ID
CONTAINER=$(docker ps --filter "name=crs-run" -q | head -1)

# Check service status (llm_enabled, llm_generations count)
docker exec $CONTAINER curl -s http://localhost:8000/status | jq

# List all generated scripts (shows file paths, previews)
docker exec $CONTAINER curl -s http://localhost:8000/scripts | jq

# Get full content of a specific script by hash
docker exec $CONTAINER curl -s http://localhost:8000/scripts/abcd1234 | jq

# Get detailed engine stats (per-script executions, errors, seeds)
docker exec $CONTAINER curl -s http://localhost:8000/engine_stats | jq
```

### Container Logs

Look for these log messages:
- `"Added task..."` - LLM task was queued
- `"Initializing AnyHarnessSeedGen with model: ..."` - LLM agent starting
- `"Generated and submitted script..."` - LLM produced a valid script
- `"returned empty script..."` - LLM failed to generate script
- `"Script X generated Y seeds via proc"` - script is producing seeds
- `"Script X has reached max execs"` - script completed its run
- `"Script X has high err rate"` - script was masked due to errors

```bash
docker logs $CONTAINER 2>&1 | grep -E "(Added task|Generated|empty script|seeds via|masked)"
```

### Script Files Location

Generated scripts are saved inside the container at:
```
/tmpfs/deepgen_workdir/processor-*/script-*.py
```

View them with:
```bash
docker exec $CONTAINER cat /tmpfs/deepgen_workdir/processor-0/script-*.py
```
