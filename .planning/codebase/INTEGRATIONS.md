# External Integrations

**Analysis Date:** 2026-03-03

## APIs & External Services

**Large Language Models:**
- Claude (Anthropic) - Primary model via litellm proxy
  - SDK: `claude-agent-sdk` (internal Team-Atlanta wrapper)
  - Auth: `ANTHROPIC_API_KEY` (env var, proxied through litellm)
  - Models: `claude-opus-4-20250514`, `claude-sonnet-4-20250514`
  - Usage: DeepGen seed generation, code analysis via agents

- OpenAI (GPT models) - Fallback provider with direct API support
  - SDK: `openai` (AsyncOpenAI client)
  - Auth: `OPENAI_API_KEY` (env var)
  - Base URL: `OPENAI_BASE_URL` (optional, for compatible APIs)
  - Models: `gpt-4.1`, `gpt-4o`, `gpt-4o-mini`, reasoning models (`o1`, `o3`, `o4`)
  - Usage: Structured output generation, code completion

- Google Gemini - Secondary provider
  - SDK: `google-genai`
  - Auth: `GEMINI_API_KEY` (env var)
  - Models: `gemini-2.5-pro`, `gemini-2.5-flash-preview-05-20`
  - Usage: Fallback LLM inference when OpenAI unavailable

- LiteLLM Proxy - API abstraction layer
  - Endpoint: `LITELLM_URL` (env var)
  - Auth: `LITELLM_KEY` (env var)
  - Purpose: Unified access to multiple LLM providers with routing
  - Implementation: `libAgents/model.py` wraps litellm for compatibility

**Code Analysis & Repositories:**
- GitHub (Team-Atlanta organization)
  - Dependencies pulled via SSH: `git+ssh://git@github.com/Team-Atlanta/libAgents.git`
  - Dependencies pulled via SSH: `git+ssh://git@github.com/Team-Atlanta/userspace-code-browser.git`
  - Dependencies pulled via SSH: `git+ssh://git@github.com/Team-Atlanta/claude-code-sdk-python.git`
  - Usage: Private library distribution for internal agents and SDK

- Grammarinator (External GitHub)
  - Dependency: `git+https://github.com/renatahodovan/grammarinator.git@68b0350`
  - Purpose: Grammar-based test case generation
  - Pinned commit: 68b0350 for stability

**Web Search & Information Retrieval:**
- Brave Search API (optional)
  - Auth: `BRAVE_API_KEY` (env var, may be used by agents)
  - Purpose: Web search integration for context gathering

- Jina AI
  - Auth: `JINA_API_KEY` (env var, optional)
  - Purpose: Possible web content extraction/parsing

## Data Storage

**Databases:**
- None in active use - project focuses on fuzzing seeds, not persistent data storage
- Optional support for testing data generation:
  - PostgreSQL (via `psycopg2-binary`)
  - MySQL (via `PyMySQL`)
  - MongoDB (via `pymongo`)
  - Redis (via `redis` - command generation, not storage)
  - SQLite (via Python's built-in `sqlite3`)

**File Storage:**
- Local filesystem only
  - Artifacts directory: `$ARTIFACTS_DIR` (default: `/artifacts`)
  - Temporary filesystem: `$ENSEMBLER_TMPFS` (default: `/tmpfs`)
  - Project tarball: `$ARTIFACTS_DIR/project.tar.gz`
  - Configuration: `$ARTIFACTS_DIR/config.json`
  - Diff files: `$REF_DIFF_PATH` (default: `/ref.diff`)

**Shared Memory:**
- Ring buffers (custom IPC) - for seed recycling between processes
- Shared memory pools - for seed storage between DeepGen and fuzzer
- Implementation: `libDeepGen/ipc_utils/ringbuffer.py`, `libDeepGen/ipc_utils/shm_pool.py`

**Caching:**
- DiskCache (`diskcache 5.6.3+`) - Persistent disk-based key-value cache
- Purpose: Agent state caching, code analysis results, LLM response caching
- No remote cache backend (local disk only)

## Authentication & Identity

**Auth Provider:**
- Custom JWT token support (`pyjwt 2.7.0+`) - for potential API authentication
- API Key authentication (env vars only):
  - OpenAI, Gemini, LiteLLM, Brave, Jina
  - No OAuth2 or OIDC integration
  - No user management system

**Certificate & TLS:**
- OpenSSL wrapper (`pyopenssl 23.1.1+`) for certificate generation
- ASN.1 support (`asn1crypto 1.5.1+`) for certificate parsing
- Used for: HTTPS connections, TLS validation
- No mutual TLS (mTLS) setup detected

## Monitoring & Observability

**Error Tracking:**
- None detected - no Sentry, Rollbar, or similar integration
- Local error logging via Python `logging` module

**Logs:**
- File/console logging via Python standard `logging`
- Structured logging support via log level configuration (`LIBAGENTS_LOG_LEVEL`)
- Log aggregation: Not implemented (local only)

**Metrics:**
- System resource monitoring via `psutil` (CPU, memory, process stats)
- No external metrics service (Prometheus, CloudWatch, Datadog)
- Statistics tracking in `libDeepGen` engine (seed pool stats, execution counts)

**Tracing:**
- Not implemented - no distributed tracing (Jaeger, Zipkin)

## CI/CD & Deployment

**Hosting:**
- Containerized deployment via Docker
- Base image: `ubuntu:20.04`
- Dockerfile: `/home/andrew/post/atlantis-c-libafl-snapshot/test-fuzzer.Dockerfile`
- No cloud platform integration detected (AWS, GCP, Azure)

**CI Pipeline:**
- Not detected - no GitHub Actions, GitLab CI, or similar
- Build process: Manual Docker build from Dockerfile

**Container Registry:**
- Not detected - no Docker Hub, ECR, or similar configured

**Deployment Pattern:**
- FastAPI service (port configurable via FastAPI defaults)
- ZeroMQ router for seed submission (Unix socket or TCP configurable via `ROUTER_ADDR`)
- Process management via system/Docker

## Environment Configuration

**Required Environment Variables:**

Core LLM:
- `LITELLM_KEY` - API key for litellm proxy (required if `LLM_PROVIDER=litellm`)
- `LITELLM_URL` - Endpoint for litellm proxy (required if `LLM_PROVIDER=litellm`)
- `OPENAI_API_KEY` - OpenAI API key (required if `LLM_PROVIDER=openai`)
- `GEMINI_API_KEY` - Gemini API key (required if `LLM_PROVIDER=gemini`)

Service Configuration:
- `ENSEMBLER_TMPFS` - Temporary filesystem path (default: `/tmpfs`)
- `ARTIFACTS_DIR` - Artifacts directory (default: `/artifacts`)
- `OSS_FUZZ_PROJECT_NAME` / `PROJECT_NAME` - Project identifier
- `HARNESS_NAME` - Default fuzzing harness (optional)
- `HARNESS_ENTRYPOINT` - Fuzzer entry point function (default: `LLVMFuzzerTestOneInput`)

Fuzzer Configuration:
- `DEEPGEN_CPUS` / `CPUSET_CPUS` / `CORES` - CPU affinity (default: `1,2,3,4`)
- `SHM_LABEL` - Shared memory label (default: `dg_simple`)
- `SEED_MAX_SIZE` - Max seed size in bytes (default: `262144`)
- `SEED_POOL_SIZE` - Seed pool size (default: `10000`)
- `N_EXEC` - Number of executions (default: `1000`)
- `TASK_PARA` - Task parallelism (default: `3`)

IPC Configuration:
- `ROUTER_ADDR` - ZeroMQ router address (default: `ipc:///tmp/ipc/haha`)
- `DEALER_TIMEOUT` - DEALER socket timeout in seconds (default: `60`)
- `SEED_TIMEOUT` - Seed submission timeout in seconds (default: `300`)

LLM Model Selection:
- `LLM_PROVIDER` - Provider choice (default: `litellm`)
- `DEFAULT_MODEL_NAME` - Override default model (optional)
- `LLM_MODELS` - Weighted model list (default: `claude-sonnet-4-20250514:1`)

Optional:
- `OPENAI_BASE_URL` - Custom OpenAI endpoint (for compatible APIs)
- `https_proxy` - HTTPS proxy URL
- `LIBAGENTS_LOG_LEVEL` - Log level (default: `INFO`)
- `IS_JVM` - JVM project flag (default: `false`)
- `BRAVE_API_KEY` - Brave Search API key (optional)
- `JINA_API_KEY` - Jina AI API key (optional)
- `REF_DIFF_PATH` - Reference diff file (default: `/ref.diff`)

**Secrets Location:**
- `.env` file (loaded via `python-dotenv`)
- Environment variables at runtime
- **No**: Kubernetes secrets, AWS Secrets Manager, HashiCorp Vault
- **Location**: Local `.env` file or process environment

## Webhooks & Callbacks

**Incoming:**
- FastAPI HTTP endpoints in `deepgen_service/simple_service.py`
- `/submit` endpoint (POST) - Accept scripts for fuzzing
- `/status` endpoint (GET) - Query fuzzing status
- No webhook callback mechanism detected

**Outgoing:**
- ZeroMQ DEALER/ROUTER pattern to fuzzer backend
- No HTTP webhooks to external services
- Agent tool callbacks via `aider-chat` integration

---

*Integration audit: 2026-03-03*
