# Codebase Structure

**Analysis Date:** 2026-03-03

## Directory Layout

```
atlantis-c-libafl-snapshot/
├── bin/                           # Entry point executables
├── config_gen/                    # Harness discovery and config generation
├── deepgen_service/               # HTTP API service for seed generation
├── libs/                          # External libraries (submodules)
│   ├── libDeepGen/               # Core seed generation engine
│   ├── libAgents/                # LLM analysis framework
│   └── claude-code-sdk-python/   # Claude SDK for agents
├── proto/                         # Protocol buffer definitions (if any)
├── oss-crs/                      # OSS-CRS integration modules
├── prebuilt-binaries/            # Pre-compiled LibAFL, glib, etc.
├── start.sh                       # Main orchestration script
├── run.py                         # Fuzzer session manager and monitor
├── build.py                       # Build orchestration
└── runner.Dockerfile             # Container definition
```

## Directory Purposes

**bin/:**
- Purpose: Container entry points and fuzzer launcher
- Contains: Executable shell scripts
- Key files: `run_fuzzer` (main entry), `compile_target` (build script)
- Generated: No
- Committed: Yes

**config_gen/:**
- Purpose: Harness metadata generation and binary symbol resolution
- Contains: Python modules for finding harness paths, LLVM symbolization
- Key files: `create.py` (main config creator), `llvm_symbolizer.py` (symbol resolution), `bb.py` (basic block utilities)
- Generated: No
- Committed: Yes

**deepgen_service/:**
- Purpose: HTTP API for seed script submission and LLM-powered generation
- Contains: FastAPI app, request/response models, lifespan management
- Key files: `simple_service.py` (main service), `__init__.py` (exports), `__main__.py` (entry point)
- Pattern: Single-file service (simple_service.py) with clear separation of concerns (endpoints, models, lifespan)
- Generated: No
- Committed: Yes

**libs/libDeepGen/:**
- Purpose: Task scheduling engine and seed generation pipeline
- Contains: Engine, tasks, executors, IPC utilities, ZMQ submission
- Structure:
  - `engine.py`: Core DeepGenEngine class
  - `tasks/`: Task implementations (script_loader, harness_seedgen, diff, etc.)
  - `executor/`: Worker execution models (in-process, direct call)
  - `ipc_utils/`: Shared memory and ringbuffer for inter-process seed exchange
  - `libGenerator/`: Code generation utilities
- Generated: No (but part of git submodule)
- Committed: Yes (via submodule)

**libs/libAgents/:**
- Purpose: Claude-powered code analysis and generation
- Contains: Agent framework, tools for code analysis, project utilities
- Key usage: Harness analysis, patch analysis, LLM prompt building
- Generated: No (git submodule)
- Committed: Yes

**libs/claude-code-sdk-python/:**
- Purpose: Claude Code SDK for agent orchestration
- Contains: Agent communication, tool execution framework
- Generated: No (git submodule)
- Committed: Yes

**proto/:**
- Purpose: Protocol buffer definitions
- Contains: Potentially .proto files for data serialization
- Generated: No
- Committed: Yes

**oss-crs/:**
- Purpose: Integration with OSS-CRS framework
- Contains: Custom modules extending libCRS functionality
- Generated: Varies
- Committed: Partially

**prebuilt-binaries/:**
- Purpose: Pre-compiled fuzzing infrastructure
- Contains: LibAFL (Rust fuzzing framework), glib, other dependencies
- Generated: No (pre-built externally)
- Committed: Yes (but large, .gitignored in many setups)

**Root level (.py files):**
- `run.py`: Fuzzer session manager and monitoring
- `build.py`: Build orchestration script
- `start.sh`: Master orchestration script
- `runner.Dockerfile`: Container image definition


## Key File Locations

**Entry Points:**
- `bin/run_fuzzer`: Container init entry, orchestrates artifact download
- `start.sh`: CPU allocation, service startup, delegates to run.py
- `run.py`: Main fuzzer session and monitoring loop
- `deepgen_service/__main__.py`: HTTP service via `python -m deepgen_service`

**Configuration:**
- `run.py` (lines 23-28): Environment variable defaults (SRC, OUT, WORK dirs)
- `deepgen_service/simple_service.py` (lines 68-103): Service config (ports, API keys, cores)
- `start.sh` (lines 44-69): CPU allocation logic
- `/artifacts/config.json`: Harness metadata (loaded by Project.from_runner_config)

**Core Logic:**
- `run.py` (BaseFuzzerSession, LibAFLFuzzerSession): Fuzzer lifecycle and monitoring
- `deepgen_service/simple_service.py` (FastAPI app, endpoints): API and task submission
- `libs/libDeepGen/libDeepGen/engine.py`: Task scheduling and execution
- `config_gen/create.py`: Harness path discovery and config generation

**Testing:**
- `libs/libDeepGen/tests/`: Test suites for engine, tasks, executor
- `libs/libAgents/tests/`: Agent and tool tests
- `libs/claude-code-sdk-python/tests/`: SDK integration tests

**Build & Deployment:**
- `runner.Dockerfile`: Container definition
- `builder.Dockerfile`: Build stage container
- `build.py`: Build orchestration
- `prebuilt-binaries/`: Pre-built artifacts


## Naming Conventions

**Files:**
- Python modules: `lowercase_with_underscores.py`
- Entry points: `run.py`, `build.py`, `start.sh`
- Service modules: `simple_service.py` (main service), `__init__.py` (package exports)
- Tests: `test_*.py` or `*_test.py`

**Directories:**
- Packages: `lowercase_with_underscores/` (e.g., `deepgen_service/`, `config_gen/`)
- Subpackages: Follow same naming (e.g., `libs/libDeepGen/libDeepGen/tasks/`)
- Prefixed directories: `lib*` indicates library (e.g., `libDeepGen`, `libAgents`)

**Classes:**
- Session classes: `*FuzzerSession` (e.g., `BaseFuzzerSession`, `LibAFLFuzzerSession`)
- Task classes: `*Task` (e.g., `ScriptLoaderTask`, `AnyHarnessSeedGen`)
- Request/Response models: `*Request`, `*Response` (Pydantic models)
- Executor classes: `Executor*` or `*Executor`

**Functions:**
- Private helpers: `_leading_underscore()` (e.g., `_process_file()`)
- Public functions: `lowercase_with_underscores()` (e.g., `watch_and_copy_directory()`)

**Environment Variables:**
- Service config: `SERVICE_HOST`, `SERVICE_PORT`
- Engine config: `DEEPGEN_CPUS`, `FUZZER_CPUS`, `CORES`
- API keys: `OSS_CRS_LLM_API_KEY`, `LITELLM_KEY`
- Paths: `ARTIFACTS_DIR`, `ENSEMBLER_TMPFS`, `REF_DIFF_PATH`
- Feature flags: `IS_JVM`, `VERBOSE_FUZZER`, `LIBAGENTS_LOG_LEVEL`


## Where to Add New Code

**New Feature (Fuzzer Enhancement):**
- Primary code: Extend `BaseFuzzerSession` with new fuzzer type in `run.py`
- Sub-command: Add new monitoring method (pattern: `_handle_*_output()`)
- Tests: Create test file alongside implementation
- Configuration: Add env vars to `start.sh` and documentation

**New LLM-Powered Task:**
- Task class: `libs/libDeepGen/libDeepGen/tasks/my_task.py`
- Extends: `TaskBase` from `task_base.py`
- Integration: Import in `deepgen_service/simple_service.py`, add HTTP endpoint
- Models: Add request/response Pydantic models in service file

**New Seed Generation Algorithm:**
- Script loader: Already supports arbitrary Python via `ScriptLoaderTask`
- Custom executor: `libs/libDeepGen/libDeepGen/executor/exec_*.py` (copy `exec_direct_call.py` pattern)
- Register: Link executor in `Executor` factory (executor.py)

**New HTTP Endpoint:**
- Location: `deepgen_service/simple_service.py` (add @app.post or @app.get)
- Models: Define request/response Pydantic classes near endpoint
- Logic: Call engine methods or LLM tasks
- Testing: Add integration test in service test suite

**New Command-Line Tool:**
- Location: Create `scripts/my_tool.py` or executable in `bin/`
- Pattern: Use argparse, follow existing script conventions
- Entry point: Register in `start.sh` or Dockerfile ENTRYPOINT if standalone

**Utilities & Helpers:**
- Shared helpers: `deepgen_service/simple_service.py` for service-level (e.g., `parse_weighted_models()`)
- Engine utilities: `libs/libDeepGen/libDeepGen/` for engine-level
- System utilities: `config_gen/` for system-level (build, harness discovery)

**Configuration or Constants:**
- Service-level: Top of `deepgen_service/simple_service.py` (lines 66-102)
- Fuzzer-level: Top of `run.py` (lines 23-38)
- Script-level: Direct env var reads with defaults


## Special Directories

**libs/ (Git Submodules):**
- Purpose: External library dependencies
- Generated: No (checked in as submodules)
- Committed: Yes (submodule references)
- Note: Update via `git submodule update --remote` if needed

**prebuilt-binaries/:**
- Purpose: Pre-compiled LibAFL, glib for fuzzing
- Generated: No (pre-built externally)
- Committed: Yes
- Note: Large directory, may be .gitignored in some setups

**/artifacts/ (Runtime):**
- Purpose: Generated harness binaries, config.json, project source
- Generated: Yes (by build stage)
- Committed: No (mount point, created at runtime)
- Contents: `povs/` (crashes), `corpus/` (seeds), `config.json`

**/out/ (Runtime):**
- Purpose: Main work directory for fuzzer
- Generated: Yes (created by run.py)
- Committed: No (runtime directory)
- Contents: Binaries, fuzzer logs, initial corpus

**/work/ (Runtime):**
- Purpose: LibAFL-specific work directory
- Generated: Yes
- Committed: No
- Contents: `staging_corpus/`, `staging_povs/`, `fuzzer.log`, `fuzzer_config_*.json`

**/tmp/ipc/ (Runtime):**
- Purpose: ZeroMQ IPC sockets
- Generated: Yes (created by run.py and start.sh)
- Committed: No
- Contents: Unix domain sockets for ZMQ router-dealer pattern

**/tmpfs/ (Runtime):**
- Purpose: In-memory filesystem for DeepGen working directory
- Generated: Yes
- Committed: No
- Contents: `deepgen_workdir/` with processor workers and generated scripts

**.planning/codebase/ (Analysis Output):**
- Purpose: Architecture and structure documentation
- Generated: Yes (by mapping tools)
- Committed: Yes (after manual review)
- Contents: ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, etc.


## Import Patterns & Dependency Flow

**Horizontal (Same Level):**
- `deepgen_service/simple_service.py` imports from `deepgen_service/__init__.py` (exports)
- `run.py` standalone (no local imports)
- `build.py` standalone (no local imports)

**Vertical (Service → Libraries):**
- `deepgen_service/simple_service.py` imports:
  - `libDeepGen.engine` (DeepGenEngine)
  - `libDeepGen.tasks.*` (task definitions)
  - `libDeepGen.submit` (ZeroMQSubmit)
  - `libAgents.utils` (Project)

**Circular Dependency Avoidance:**
- Config generation is pre-fuzzing (not imported by runtime code)
- run.py doesn't import deepgen_service
- Executor and engine are decoupled via abstract interfaces

**External Dependencies:**
- FastAPI (HTTP service)
- Pydantic (validation)
- ZeroMQ (seed communication)
- httpx (LLM budget checks)
- Async/await (asyncio for concurrent tasks)

---

*Structure analysis: 2026-03-03*
