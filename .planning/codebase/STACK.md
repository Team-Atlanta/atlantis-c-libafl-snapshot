# Technology Stack

**Analysis Date:** 2026-03-03

## Languages

**Primary:**
- Python 3.12 - All application code, service layer, libraries, and fuzzing framework
- Rust 1.87 - Fuzzer binary compilation and interop

**Secondary:**
- C/C++ - LibAFL and AFL++ core fuzzing engines (prebuilt binaries)
- Shell - Build and deployment scripts (`start.sh`, build helpers)

## Runtime

**Environment:**
- Python 3.12.0 - Required for all modules
- Rust 1.87 toolchain - For fuzzer compilation

**Package Manager:**
- pip/setuptools - Python dependency management
- uv - Modern Python package management (as seen in pyproject.toml configurations)
- Cargo - Rust package management (for prebuilt fuzzer binaries)

**Lockfile:** Project uses PEP 508 `pyproject.toml` dependencies without pinned lock files in root; individual libraries define strict requirements.

## Frameworks

**Core:**
- FastAPI 0.109.0+ - Modern async web framework for HTTP service (`deepgen_service/simple_service.py`)
- Uvicorn 0.27.0+ - ASGI server for FastAPI deployment

**LLM Integration:**
- litellm 1.65.1+ - Unified LLM API abstraction (primary provider: `openai`/`litellm` config)
- aider-chat 0.83.2+ - Code editing and AI agent integration in `libAgents`
- claude-agent-sdk (internal) - Team-Atlanta custom Claude SDK wrapper
- google-genai 1.9.0+ - Gemini model support (fallback provider)

**Code Analysis & Exploration:**
- tree-sitter 0.24.0 (pinned) - Code parsing and AST analysis
- tree-sitter-cpp 0.23.4 (pinned) - C++ language support for AST
- code-browser-client (internal) - Custom userspace code browser for codebase exploration
- grammarinator (git source) - Grammar-based test generation

**Testing & Validation:**
- hypothesis 6.0.0+ - Property-based testing and seed data generation
- pytest-asyncio 0.26.0+ - Async test execution
- faker 18.0.0+ - Realistic fake data generation

## Key Dependencies

**Critical:**
- libAgents - Custom deep search agent framework integrating with multiple LLMs
- libDeepGen - Seed generation and fuzzing pipeline orchestration
- claude-code-sdk-python - Anthropic Claude Code SDK for agent interactions
- userspace-code-browser - Custom code browser client for semantic code analysis

**Infrastructure & IPC:**
- pyzmq 26.4.0+ - ZeroMQ async messaging for seed submission to fuzzer (`libDeepGen/submit.py`)
- aiofiles 24.1.0+ - Async file I/O operations
- diskcache 5.6.3+ - Persistent disk-based caching layer
- psutil 7.0.0+ - System resource monitoring and CPU affinity

**Data Generation & Mutation:**
- pwntools 4.8.0+ - Binary data crafting and exploitation utilities
- capstone 4.0.2+ - Disassembly framework for binary analysis
- pefile 2023.2.0+ - PE executable format manipulation
- pyelftools 0.29+ - ELF executable format manipulation
- Pillow 9.5.0+ - Image format generation (PNG, JPEG, GIF, BMP, TIFF)
- opencv-python 4.7.0+ - Computer vision format support
- pypdf 3.8.0+ - PDF file generation/mutation
- python-docx 0.8.11+ - Microsoft Word document generation
- openpyxl 3.1.2+, xlwt, xlrd - Excel file format support
- lxml 4.9.2+, beautifulsoup4 4.12.2+ - XML/HTML generation and parsing
- python-magic 0.4.27+ - File type detection
- patool 1.12.0+ - Universal archive manipulation

**Cryptography & Serialization:**
- cryptography 41.0.0+ - Modern cryptographic primitives
- pycryptodome 3.17.0+ - Comprehensive cryptography library
- pyopenssl 23.1.1+ - OpenSSL bindings for certificate generation
- pyjwt 2.7.0+ - JSON Web Token support
- protobuf 5.29.0+ - Protocol Buffers serialization
- orjson 3.8.14+, ujson 5.7.0+ - High-performance JSON libraries
- msgpack 1.0.5+, cbor2 5.4.6+, avro 1.11.1+ - Alternative serialization formats

**Network Protocol Crafting:**
- scapy 2.5.0+ - Packet crafting and network protocol manipulation
- impacket 0.10.0+ - Network protocol implementations
- websockets 11.0.3+, aiohttp 3.8.4+, requests 2.31.0+ - HTTP and WebSocket handling
- dnspython 2.3.0+ - DNS record crafting

**Web & Template:**
- jinja2 3.1.2+ - Template generation
- flask 2.3.2+ - Lightweight web framework
- django 4.2.1+ - Full-featured web framework
- tornado 6.3.2+ - Async web framework

**Validation & Configuration:**
- pydantic 2.5.0+ - Data validation and configuration management
- jsonschema 4.17.3+ - JSON schema validation
- pyyaml 6.0+, toml 0.10.2+ - Configuration file parsing

**Database Support (Optional/For Data Generation):**
- psycopg2-binary 2.9.6+ - PostgreSQL support
- PyMySQL 1.0.3+ - MySQL support
- pymongo 4.3.3+ - MongoDB support
- redis 4.5.5+ - Redis command generation
- sqlalchemy 2.0.13+ - SQL query generation

**Development & Utilities:**
- dotenv 0.9.9+ - Environment variable loading from `.env` files
- filelock 3.12.0+ - File-based locking
- atomics 1.0.3+ - Atomic operations support
- tree-sitter dependencies pinned due to compatibility requirements

## Configuration

**Environment:**
- Loaded via `.env` file (see `dotenv` dependency in `config.py`)
- Config overrides: context variables > environment variables > config.json defaults
- LLM provider selection: `LLM_PROVIDER` env var (default: `litellm`)

**Key Environment Variables:**
- `ANTHROPIC_API_KEY` - Claude API key (via litellm proxy)
- `OPENAI_API_KEY` - OpenAI API key
- `GEMINI_API_KEY` - Google Gemini API key
- `LITELLM_URL` - LiteLLM proxy endpoint
- `LITELLM_KEY` - LiteLLM authentication key
- `LLM_PROVIDER` - Provider selection: `openai`, `gemini`, `litellm`
- `DEFAULT_MODEL_NAME` - Override default LLM model per context
- `LIBAGENTS_LOG_LEVEL` - Logging verbosity control

**Build:**
- `pyproject.toml` - Python package build configuration (setuptools/hatchling)
- `setup.py` - Legacy setup files in some libraries
- Docker: `test-fuzzer.Dockerfile` for Ubuntu 20.04-based test environment
- LLVM 18, Rust toolchain, Python 3.12 containerized builds

**Service Configuration:**
- Config file: `libAgents/config.json` - Provider settings, model mappings, default temperatures
- Provider configs support OpenAI-compatible APIs (strict/compatible mode)

## Platform Requirements

**Development:**
- Python 3.12+
- Rust 1.87+
- LLVM 18 (for native builds, prebuilt available)
- Standard build tools (make, cmake, gcc/clang)
- ZeroMQ development libraries (for pyzmq)
- OpenSSL development headers

**Production:**
- Python 3.12 runtime
- Docker container deployment (Ubuntu 20.04 base)
- Fuzzer binaries (prebuilt as statically-linked C/C++)
- FastAPI/Uvicorn service container
- Filesystem access to `/tmpfs`, `/artifacts` directories
- IPC support for ZeroMQ (Unix domain sockets or TCP)
- System resource limits manageable via psutil/cgroup

---

*Stack analysis: 2026-03-03*
