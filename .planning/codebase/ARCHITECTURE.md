# Architecture

**Analysis Date:** 2026-03-03

## Pattern Overview

**Overall:** Two-tier concurrent architecture with orchestrated fuzzing engine and LLM-powered seed generation service

**Key Characteristics:**
- **Process-based parallelism**: Main fuzzer (`run.py`) and HTTP service (`simple_service.py`) run concurrently
- **IPC-driven design**: Fuzzer and seed generation communicate via ZeroMQ (ZMQ) with file system synchronization
- **Abstract fuzzer backend**: Supports multiple fuzzing engines (currently LibAFL) via polymorphic sessions
- **Event-driven monitoring**: Thread pools monitor fuzzer process streams and file system changes using inotify
- **LLM-augmented generation**: Optional Claude-powered seed generator integrated via libDeepGen/libAgents


## Layers

**Entry Points & Orchestration:**
- Purpose: Process startup, CPU allocation, service lifecycle management
- Location: `bin/run_fuzzer`, `start.sh`
- Contains: Shell scripts that coordinate DeepGen service and fuzzer startup
- Depends on: Environment variables (CORES, HARNESS_NAME), libCRS utilities
- Used by: Container init system, CI/CD orchestrators

**Fuzzer Session Management:**
- Purpose: Manages fuzzer process lifecycle, monitoring, and restart logic
- Location: `run.py` (classes: `BaseFuzzerSession`, `LibAFLFuzzerSession`)
- Contains: Abstract base class defining fuzzer interface, LibAFL-specific implementation
- Depends on: Standard library (subprocess, threading, select), ctypes for inotify
- Used by: Main fuzzer entry point in `run()` function

**Fuzzer Monitoring & File Synchronization:**
- Purpose: Watch staging directories, deduplicate seeds by SHA256, forward unique seeds
- Location: `run.py` (functions: `watch_and_copy_directory`, `_process_file`, `_process_existing_files`)
- Contains: inotify-based file watcher with multithreaded processing, SHA256 checksum tracking
- Depends on: Linux inotify syscalls, file I/O, threading locks
- Used by: `LibAFLFuzzerSession.setup_monitoring()` to track `/work/staging_corpus` and `/work/staging_povs`

**HTTP API Service:**
- Purpose: Accept Python scripts and LLM generation requests, manage task submission
- Location: `deepgen_service/simple_service.py` (FastAPI app)
- Contains: REST endpoints for script submission, LLM generation, metrics, status
- Depends on: FastAPI, Pydantic, libDeepGen (DeepGenEngine), libAgents (Project)
- Used by: External clients submitting scripts or requesting LLM-powered generation

**DeepGen Execution Engine:**
- Purpose: Task scheduling, script execution, seed generation pipeline orchestration
- Location: `libs/libDeepGen/libDeepGen/engine.py` (DeepGenEngine)
- Contains: Async task scheduler, processor workers, seed pool management
- Depends on: ZeroMQ (zmq), shared memory utilities, executor pool
- Used by: HTTP service to execute tasks and communicate with fuzzer via ZMQ

**Task Pipeline:**
- Purpose: Define different types of seed generation tasks (scripts, LLM, diff analysis)
- Location: `libs/libDeepGen/libDeepGen/tasks/` (multiple task classes)
- Contains: `ScriptLoaderTask`, `AnyHarnessSeedGen`, `DiffAnalysisTask`
- Depends on: libAgents for LLM interactions, task_board for scheduling
- Used by: HTTP service endpoints to create and submit tasks to engine

**Script Execution & Code Generation:**
- Purpose: Load user Python scripts, execute gen_one_seed() functions, validate output
- Location: `libs/libDeepGen/libDeepGen/tasks/script_loader.py`, `deepgen_service/simple_service.py` (ScriptSubmission validation)
- Contains: Script compilation, syntax validation, function extraction
- Depends on: Python compile(), Pydantic validators
- Used by: HTTP service to validate submissions before engine processing

**LLM Integration:**
- Purpose: Analyze harness code and generate seed scripts via Claude
- Location: `libs/libAgents/` (agents framework), task classes in libDeepGen
- Contains: Code analysis agents, prompt engineering, LLM API integration via litellm
- Depends on: Claude API, project source code, diff patches
- Used by: `AnyHarnessSeedGen` and `DiffAnalysisTask` for automated seed generation

**Seed Submission (ZeroMQ):**
- Purpose: Forward generated seeds to fuzzer process via broker pattern
- Location: `libs/libDeepGen/libDeepGen/submit.py` (ZeroMQSubmit), `deepgen_service/simple_service.py` (SeedCountingZeroMQSubmit wrapper)
- Contains: ZMQ router-dealer pattern, seed serialization, timeout handling
- Depends on: ZeroMQ (zmq), routing configuration
- Used by: Engine to push generated seeds to connected fuzzer dealers

**Config Generation & Harness Discovery:**
- Purpose: Locate harness source files, generate configuration metadata
- Location: `config_gen/` (create.py, bb.py, llvm_symbolizer.py)
- Contains: Binary search for harness paths, LLVM symbol resolution, YAML config generation
- Depends on: subprocess (find, nm, llvm-symbolizer), yaml
- Used by: Pre-fuzzing setup to create harness metadata for LLM analysis

**Budget Monitoring:**
- Purpose: Track and enforce LLM API spend limits
- Location: `deepgen_service/simple_service.py` (LiteLLMBudgetChecker class)
- Contains: Periodic budget checks via LiteLLM API, engine shutdown on exhaustion
- Depends on: httpx, asyncio, LiteLLM endpoint
- Used by: Service lifespan to monitor and halt expensive operations


## Data Flow

**Script Submission Flow:**

1. Client POSTs Python script to `/submit_script` endpoint
2. FastAPI validates script syntax and presence of `gen_one_seed()` function
3. `ScriptLoaderTask` created with script content + harness target
4. Task submitted to `DeepGenEngine.add_task()` (async queue)
5. Engine assigns task to available processor worker
6. Worker executes script's `gen_one_seed()` function (repeated per `num_repeat`)
7. Generated seeds sent to fuzzer via `SeedCountingZeroMQSubmit`
8. Fuzzer receives seeds on ZMQ dealer port, integrates into corpus


**LLM Generation Flow (Delta Mode):**

1. Client POSTs to `/generate_diff_seeds` with harness name
2. Service creates `DiffAnalysisTask(project_bundle, harness_id, ref.diff)`
3. Engine schedules task with high priority
4. Task uses libAgents to analyze patch and target harness code
5. Claude LLM generates Python script as output
6. Generated script loaded as `ScriptLoaderTask` and executed automatically
7. Fuzzer receives generated seeds for diff-induced vulnerabilities


**LibAFL Fuzzer Monitoring Flow:**

1. `LibAFLFuzzerSession.run()` starts fuzzer binary with config via subprocess
2. Main monitoring thread (`_monitor_std_streams()`) reads stdout/stderr with select()
3. LibAFL panic detection: buffers lines, detects "panicked" in error output, kills + restarts process
4. Log monitoring thread (`_monitor_log_file()`) reads JSON from `/work/fuzzer.log`
5. Parses exec_sec, coverage ratio, crash count into `FuzzerStats`
6. Stats dumped to logs every 2s if changed (prevents duplicate logging)
7. Separate inotify watchers track:
   - `/work/staging_corpus` → copies unique files to `/artifacts/corpus`
   - `/work/staging_povs` → copies unique crash files to `/artifacts/povs`
8. Uniqueness tracked via SHA256 checksums across both directories (shared `seen_checksums` set)


**State Management:**

**Engine State:**
- Async task queue in `DeepGenEngine._tasks`
- Per-script statistics in `engine.stats` (executions, errors, generated seeds)
- Masked script list to prevent re-execution of errored scripts
- Script ID → Script object mapping via `engine.scripts`

**Fuzzer Session State:**
- `latest_stats`: Last FuzzerStats reported (cleared after dump)
- `snapshot_stats`: Always-available latest (for warning thresholds)
- `process`: Subprocess handle with communication pipes
- `seen_checksums`: Set tracking deduplicated seed hashes
- Error counter with threshold (100 errors trigger restart)

**Global Service State:**
- `engine`: Optional[DeepGenEngine] - None until initialization complete
- `project`: Optional[Project] - None if LLM disabled, contains harness metadata
- `budget_checker`: Optional[LiteLLMBudgetChecker] - tracks spend
- Counters: `script_submission_count`, `llm_generation_count`


## Key Abstractions

**BaseFuzzerSession (Abstract Base Class):**
- Purpose: Define fuzzer interface independent of engine
- Examples: `LibAFLFuzzerSession` (concrete implementation)
- Pattern: Template Method - subclasses implement `mode`, `crashes_paths`, `corpus_paths`, `_monitor_log_file()`, `_handle_error_output()`, `_handle_stdout_output()`
- Key methods: `run()`, `setup()`, `start()`, `setup_monitoring()` (all concrete), `_monitor_std_streams()` (shared impl)

**DeepGenEngine:**
- Purpose: Central task scheduler and worker pool manager
- Examples: Initialized in service lifespan, used by all HTTP endpoints
- Pattern: Async context manager with background task runner
- Key methods: `add_task()` (async), `run()` (main event loop), `get_script()`, stats access via `_stat_lock`

**FuzzerTask (Abstract Base):**
- Purpose: Represents work items in the generation pipeline
- Examples: `ScriptLoaderTask`, `AnyHarnessSeedGen`, `DiffAnalysisTask`
- Pattern: Polymorphic task types with priority and repeat counts
- Key attributes: `priority` (1-100), `num_repeat` (execution count), `max_exec` (limit)

**ZeroMQSubmit (Seed Submission):**
- Purpose: Route generated seeds to fuzzer via ZMQ
- Examples: `SeedCountingZeroMQSubmit` (wrapper with observability)
- Pattern: Router-Dealer pattern with serialization
- Key methods: `request_seed_submit()` (async queue mechanism)

**Project Bundle (libAgents):**
- Purpose: Container for harness metadata and source code
- Examples: Created via `Project.from_runner_config()`
- Pattern: Read-only during execution, bootstrapped at service startup
- Key attributes: `name`, `harnesses` (dict), `ref_diff` (optional patch)


## Entry Points

**Container Entry Point:**
- Location: `bin/run_fuzzer`
- Triggers: Container init
- Responsibilities: Download artifacts, set LD_LIBRARY_PATH, create IPC dirs, delegate to start.sh

**Orchestration Entry Point:**
- Location: `start.sh`
- Triggers: Called by `run_fuzzer`
- Responsibilities: CPU allocation (DeepGen vs Fuzzer), start HTTP service background, launch main fuzzer

**Main Fuzzer Entry Point:**
- Location: `run.py::run()` function
- Triggers: Called by `start.sh` with harness name
- Responsibilities: Create `LibAFLFuzzerSession`, start monitoring, implement restart logic (max 10 restarts)

**HTTP Service Entry Point:**
- Location: `deepgen_service/__main__.py` (via `python3.12 -m deepgen_service`)
- Triggers: Started by `start.sh` in background if multiple cores available
- Responsibilities: Initialize DeepGenEngine, bootstrap Project, start uvicorn server

**Engine Event Loop:**
- Location: `DeepGenEngine.run()` (async)
- Triggers: Started by service lifespan in background task
- Responsibilities: Process task queue, distribute to worker processors, handle completion


## Error Handling

**Strategy:** Multi-level error containment with restart and degradation

**Patterns:**

**Fuzzer Process Errors:**
- Panic detection: stderr monitoring triggers on "panicked" string
- Action: Kill process group (SIGTERM → SIGKILL), increment error counter
- Threshold: >100 errors → exit container (os._exit(0))
- Mitigation: Auto-restart with cooldown (min 5s between restarts)

**LLM Generation Errors:**
- Task-level try-catch: Errors logged, task masked to prevent retry
- Service-level: HTTP 500 responses with exception details
- Budget exhaustion: Monitored by `LiteLLMBudgetChecker`, signals engine._should_exit

**Script Validation Errors:**
- Pydantic validators catch syntax errors and missing functions
- HTTP 400 Bad Request with detailed error message
- Script rejected before engine processing

**Engine Initialization Errors:**
- Caught in service lifespan context manager
- Logged with full traceback, service fails to start
- HTTP 503 responses if engine unavailable

**Monitoring Thread Errors:**
- Inotify watcher: OSError caught, fd_handlers reconstructed
- Log file monitoring: Exception logged, loop continues
- Non-critical errors don't crash service


## Cross-Cutting Concerns

**Logging:**
- Root logger: INFO level, basic format
- libAgents/libDeepGen: Configurable via LIBAGENTS_LOG_LEVEL env var (default WARNING)
- LiteLLM: Forced to WARNING+ to reduce noise
- Per-layer: Custom loggers (e.g., "libDeepGen.engine") with hierarchical control

**Validation:**
- Script content: Compile check + function presence check (Pydantic validator)
- Harness names: Checked against Project.harnesses dict
- Task parameters: Priority (1-100), num_repeat (≥1), max_exec (optional)

**Authentication:**
- None for HTTP service (assumes private network)
- LLM API key: Passed via LITELLM_KEY/OSS_CRS_LLM_API_KEY env vars
- Budget enforcement: LiteLLM endpoint checks keys

**CPU Affinity:**
- Main service process: Pinned to CORES[0]
- Engine workers: Use CORES[1:] (or all if single core)
- Fuzzer: Gets remaining cores from FUZZER_CPUS env var
- Allocation strategy: start.sh divides CPU pool between services

**Resource Limits:**
- Seeds: Max pool size (SEED_POOL_SIZE, default 10000), max file size (SEED_MAX_SIZE, default 256KB)
- Tasks: No hard limit, but queue-based (memory-bounded by DeepGenEngine)
- Memory: Shared memory pool (shm_label="dg_simple") for seed data interchange
- File handles: inotify fd per watcher thread (2 total for corpus/povs)

---

*Architecture analysis: 2026-03-03*
