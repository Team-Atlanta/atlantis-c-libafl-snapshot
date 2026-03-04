#!/usr/bin/env python3
"""
Simplified DeepGen Service - HTTP API for script submission and LLM-powered seed generation
"""

import asyncio
import logging
import os
import tarfile
import httpx
from pathlib import Path
from typing import Optional
import psutil

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator
from contextlib import asynccontextmanager

from libDeepGen.engine import DeepGenEngine
from libDeepGen.submit import ZeroMQSubmit
from libDeepGen.tasks.script_loader import ScriptLoaderTask
from libDeepGen.tasks.harness_seedgen import AnyHarnessSeedGen
from libDeepGen.tasks.diff import DiffAnalysisTask
from libAgents.utils import Project

# ============================================================================
# Configure logging levels based on LIBAGENTS_LOG_LEVEL environment variable
# This reduces verbosity of libAgents (which logs full prompts by default)
# ============================================================================
_log_level_str = os.environ.get("LIBAGENTS_LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_str, logging.INFO)

# Configure basic logging early - force it to add handler to root
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True,  # Force reconfiguration even if already configured
)

# Explicitly add stream handler to libDeepGen and libAgents loggers
# This ensures they log even if root logger is reconfigured later
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
_handler.setLevel(logging.INFO)

# Configure parent loggers to handle all child logger output
for _logger_name in ["libAgents", "libDeepGen"]:
    _lib_logger = logging.getLogger(_logger_name)
    _lib_logger.setLevel(_log_level)
    _lib_logger.addHandler(_handler)
    _lib_logger.propagate = False  # Don't duplicate to root

# Also configure specific libDeepGen child loggers that might be initialized early
for _child_logger_name in ["libDeepGen.engine", "libDeepGen.executor.executor", "libDeepGen.tasks.task_board"]:
    _child_logger = logging.getLogger(_child_logger_name)
    _child_logger.setLevel(_log_level)
# Reduce litellm verbosity (it logs a lot of debug info)
logging.getLogger("litellm").setLevel(max(_log_level, logging.WARNING))
logging.getLogger("LiteLLM").setLevel(max(_log_level, logging.WARNING))
# Reduce httpx verbosity (used by litellm)
logging.getLogger("httpx").setLevel(max(_log_level, logging.WARNING))

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration from environment (set at startup)
# ============================================================================
TEMPFS_DIR = os.environ.get("ENSEMBLER_TMPFS", "/tmpfs")

# Optional default harness name
DEFAULT_HARNESS_NAME = os.environ.get("HARNESS_NAME")

# Project configuration (for LLM-powered seed generation)
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
PROJECT_NAME = os.environ.get("OSS_FUZZ_PROJECT_NAME", os.environ.get("PROJECT_NAME", "unknown"))
CONFIG_JSON_PATH = ARTIFACTS_DIR / "config.json"
PROJECT_TARBALL_PATH = ARTIFACTS_DIR / "project.tar.gz"
REF_DIFF_PATH = Path(os.environ.get("REF_DIFF_PATH", "/ref.diff"))

# LLM configuration
DEFAULT_HARNESS_ENTRYPOINT = os.environ.get("HARNESS_ENTRYPOINT", "LLVMFuzzerTestOneInput")
LLM_WEIGHTED_MODELS = os.environ.get("LLM_MODELS", "claude-sonnet-4-20250514:1").split(",")
IS_JVM_PROJECT = os.environ.get("IS_JVM", "false").lower() == "true"

# Engine configuration - with defaults
# Use DEEPGEN_CPUS (set by start.sh) or fall back to CPUSET_CPUS/CORES
_cpuset_str = os.environ.get("DEEPGEN_CPUS") or os.environ.get("CPUSET_CPUS") or os.environ.get("CORES", "1,2,3,4")
CORES = list(map(int, _cpuset_str.split(","))) if _cpuset_str else []
SHM_LABEL = os.environ.get("SHM_LABEL", "dg_simple")
# Reduced defaults to fit in Docker's default 64MB shm_size
# Memory budget for 64MB shm with up to 128 cores:
#   Script pool: 8KB * 64 = 512KB
#   Per core: ~400KB (seed pool + ring buffers)
#   128 cores: 512KB + 128*400KB = ~51MB
SEED_MAX_SIZE = int(os.environ.get("SEED_MAX_SIZE", 2048))  # 2KB per seed
SEED_POOL_SIZE = int(os.environ.get("SEED_POOL_SIZE", 128))  # 128 seeds per core
SCRIPT_MAX_SIZE = int(os.environ.get("SCRIPT_MAX_SIZE", 8192))  # 8KB per script
SCRIPT_POOL_SIZE = int(os.environ.get("SCRIPT_POOL_SIZE", 64))  # 64 scripts total
N_EXEC = int(os.environ.get("N_EXEC", 50))  # Executions per scheduling (affects ring buffer slot size)
TASK_PARA = int(os.environ.get("TASK_PARA", 3))

# ZeroMQ configuration
ROUTER_ADDR = os.environ.get("ROUTER_ADDR", "ipc:///tmp/ipc/haha")
DEALER_TIMEOUT = int(os.environ.get("DEALER_TIMEOUT", 60))
SEED_TIMEOUT = int(os.environ.get("SEED_TIMEOUT", 300))

# HTTP server configuration
SERVICE_HOST = os.environ.get("SERVICE_HOST", "0.0.0.0")
SERVICE_PORT = int(os.environ.get("SERVICE_PORT", 8000))

# ============================================================================
# Global state
# ============================================================================
engine: Optional[DeepGenEngine] = None
project: Optional[Project] = None
script_submission_count = 0
llm_generation_count = 0


# ============================================================================
# LiteLLM Budget Checker
# ============================================================================
class LiteLLMBudgetChecker:
    """
    Periodically checks LiteLLM budget and stops the engine when budget is exhausted.

    Uses the /key/info endpoint to check spend vs max_budget.
    """

    def __init__(self, check_interval: int = 60, budget_margin: float = 1.0):
        """
        Args:
            check_interval: Seconds between budget checks
            budget_margin: Stop when remaining budget is less than this amount in dollars
        """
        self.check_interval = check_interval
        self.budget_margin = budget_margin
        self.litellm_url = os.environ.get("OSS_CRS_LLM_API_URL") or os.environ.get("LITELLM_URL", "")
        self.litellm_key = os.environ.get("OSS_CRS_LLM_API_KEY") or os.environ.get("LITELLM_KEY", "")
        self.last_spend: Optional[float] = None
        self.last_max_budget: Optional[float] = None
        self.budget_exhausted = False
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def check_budget(self) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Query LiteLLM /key/info endpoint to get current spend and max budget.

        Returns:
            tuple of (spend, max_budget, error_message)
        """
        if not self.litellm_url or not self.litellm_key:
            return None, None, "LITELLM_URL or LITELLM_KEY not configured"

        try:
            url = f"{self.litellm_url.rstrip('/')}/key/info"
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self.litellm_key}"}
                )

                if response.status_code != 200:
                    return None, None, f"HTTP {response.status_code}: {response.text}"

                data = response.json()
                info = data.get("info", {})
                spend = info.get("spend")
                max_budget = info.get("max_budget")

                if spend is not None and max_budget is not None:
                    self.last_spend = float(spend)
                    self.last_max_budget = float(max_budget)
                    return self.last_spend, self.last_max_budget, None
                else:
                    return None, None, f"Missing spend or max_budget in response: {data}"

        except Exception as e:
            return None, None, f"Error checking budget: {e}"

    async def _check_loop(self, engine_ref: DeepGenEngine):
        """Background loop that periodically checks budget."""
        logger.info(f"[BUDGET] Starting budget checker (interval={self.check_interval}s, margin=${self.budget_margin})")

        while self._running:
            try:
                spend, max_budget, error = await self.check_budget()

                if error:
                    logger.warning(f"[BUDGET] Check failed: {error}")
                else:
                    remaining = max_budget - spend
                    logger.info(f"[BUDGET] Spend: ${spend:.2f} / ${max_budget:.2f} (remaining: ${remaining:.2f})")

                    if remaining < self.budget_margin:
                        logger.warning(f"[BUDGET] Budget exhausted! Remaining ${remaining:.2f} < margin ${self.budget_margin}")
                        self.budget_exhausted = True
                        # Signal engine to stop
                        if engine_ref:
                            engine_ref._should_exit.store(1)
                            logger.info("[BUDGET] Signaled engine to stop")
                        break

            except Exception as e:
                logger.error(f"[BUDGET] Error in check loop: {e}")

            await asyncio.sleep(self.check_interval)

        logger.info("[BUDGET] Budget checker stopped")

    def start(self, engine_ref: DeepGenEngine):
        """Start the background budget checker."""
        if not self.litellm_url or not self.litellm_key:
            logger.warning("[BUDGET] Skipping budget checker - LITELLM_URL or LITELLM_KEY not configured")
            return

        self._running = True
        self._task = asyncio.create_task(self._check_loop(engine_ref))

    def stop(self):
        """Stop the background budget checker."""
        self._running = False
        if self._task:
            self._task.cancel()


# Global budget checker instance
budget_checker: Optional[LiteLLMBudgetChecker] = None


# ============================================================================
# Request/Response Models
# ============================================================================
class ScriptSubmission(BaseModel):
    """Request model for script submission"""
    script_content: str = Field(..., description="Python script content with gen_one_seed() function")
    harness_name: Optional[str] = Field(None, description="Target harness name (uses default if not provided)")
    label: Optional[str] = Field(None, description="Optional label for tracking")
    priority: int = Field(1, ge=1, le=100, description="Priority (1-100, higher = more important)")
    num_repeat: int = Field(1, ge=1, description="Number of times to repeat script generation")
    max_exec: Optional[int] = Field(None, description="Max executions for this script (unlimited if not set)")

    @validator('script_content')
    def validate_script_syntax(cls, v):
        """Validate that script content is valid Python and contains gen_one_seed"""
        if not v or not v.strip():
            raise ValueError("script_content cannot be empty")

        # Check if it compiles
        try:
            compile(v, '<string>', 'exec')
        except SyntaxError as e:
            raise ValueError(f"Invalid Python syntax: {e}")

        # Check if it contains gen_one_seed function
        if 'def gen_one_seed(' not in v:
            raise ValueError("Script must contain 'def gen_one_seed()' function")

        return v


class ScriptSubmissionResponse(BaseModel):
    """Response model for script submission"""
    status: str
    message: str
    task_id: Optional[str] = None
    label: str
    harness_name: str


class ServiceStatus(BaseModel):
    """Response model for service status"""
    running: bool
    default_harness_name: Optional[str]
    project_name: Optional[str]
    available_harnesses: list[str]
    llm_enabled: bool
    diff_mode: bool
    diff_lines: Optional[int]
    cores: list[int]
    shm_label: str
    scripts_submitted: int
    llm_generations: int
    workdir: str
    # LLM budget info
    llm_budget_spend: Optional[float] = None
    llm_budget_max: Optional[float] = None
    llm_budget_remaining: Optional[float] = None
    llm_budget_exhausted: bool = False


class ScriptInfo(BaseModel):
    """Info about a generated script"""
    file_path: str
    task_label: str
    harness_name: str
    sha256: str
    size_bytes: int
    content_preview: str  # First 500 chars


class LLMGenerationRequest(BaseModel):
    """Request model for LLM-powered seed generation"""
    harness_name: str = Field(..., description="Target harness name (must exist in project)")
    harness_entrypoint: str = Field(
        default="LLVMFuzzerTestOneInput",
        description="Entrypoint function name"
    )
    priority: int = Field(1, ge=1, le=100, description="Priority (1-100)")
    num_repeat: int = Field(1, ge=1, description="Number of times to repeat")
    max_exec: Optional[int] = Field(None, description="Max executions (unlimited if not set)")


class LLMGenerationResponse(BaseModel):
    """Response model for LLM generation"""
    status: str
    message: str
    task_id: Optional[str] = None
    harness_name: str
    harness_path: Optional[str] = None


# ============================================================================
# Script Counting Submit wrapper with observability
# ============================================================================
class SeedCountingZeroMQSubmit(ZeroMQSubmit):
    """ZeroMQ submit with seed counting and observability logging."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.total_seeds_sent = 0
        self.seeds_by_script = {}  # script_hash -> seed_count

    async def request_seed_submit(self, proc_id, script_id, script, seed_ids):
        """Track and log seed submissions for observability."""
        await super().request_seed_submit(proc_id, script_id, script, seed_ids)
        if seed_ids:
            seed_count = len(seed_ids)
            self.total_seeds_sent += seed_count
            script_hash = script.sha256[:8]

            # Track per-script seed counts
            if script_hash not in self.seeds_by_script:
                self.seeds_by_script[script_hash] = 0
            self.seeds_by_script[script_hash] += seed_count

            # Log seed submission with observability info
            logger.info(
                f"[SEEDS] Script {script_hash} -> {seed_count} seeds sent to fuzzer "
                f"(script_total={self.seeds_by_script[script_hash]}, "
                f"all_scripts_total={self.total_seeds_sent})"
            )


# ============================================================================
# Lifespan management
# ============================================================================
def bootstrap_project(workdir: Path) -> Optional[Project]:
    """Bootstrap Project from runner config if available."""
    if not CONFIG_JSON_PATH.exists():
        logger.warning(f"Config JSON not found at {CONFIG_JSON_PATH}, LLM generation disabled")
        return None

    # Extract source tarball if exists
    src_path = workdir / "src"
    if PROJECT_TARBALL_PATH.exists() and not src_path.exists():
        logger.info(f"Extracting {PROJECT_TARBALL_PATH} to {workdir}")
        try:
            with tarfile.open(PROJECT_TARBALL_PATH, "r:gz") as tar:
                tar.extractall(workdir)
            logger.info(f"Extracted source to {src_path}")
        except Exception as e:
            logger.error(f"Failed to extract tarball: {e}")
            return None
    elif not src_path.exists():
        logger.warning(f"Source path {src_path} does not exist and no tarball to extract")
        return None

    # Check for ref.diff (delta mode)
    ref_diff_path = REF_DIFF_PATH if REF_DIFF_PATH.exists() else None
    if ref_diff_path:
        logger.info(f"Found ref.diff at {ref_diff_path} - delta mode enabled")

    # Create Project from runner config
    try:
        proj = Project.from_runner_config(
            config_json_path=CONFIG_JSON_PATH,
            src_path=src_path,
            project_name=PROJECT_NAME,
            ref_diff_path=ref_diff_path,
        )
        logger.info(f"Project bootstrapped: {proj.name} with {len(proj.harnesses)} harnesses")
        logger.info(f"Available harnesses: {list(proj.harnesses.keys())}")
        if proj.ref_diff:
            logger.info(f"Delta mode: ref.diff loaded ({len(proj.ref_diff)} bytes)")
        return proj
    except Exception as e:
        logger.error(f"Failed to bootstrap Project: {e}", exc_info=True)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic"""
    global engine, project

    logger.info("=" * 80)
    logger.info("Starting DeepGen Service with LLM Support")
    logger.info("=" * 80)
    logger.info(f"Project Name: {PROJECT_NAME}")
    logger.info(f"Default Harness: {DEFAULT_HARNESS_NAME or 'None (must specify in request)'}")
    logger.info(f"DeepGen CPUs: {CORES} ({len(CORES)} cores)")
    logger.info(f"SHM Label: {SHM_LABEL}")
    logger.info(f"Router Address: {ROUTER_ADDR}")
    logger.info("=" * 80)

    if not CORES:
        logger.error("No CPUs allocated for DeepGen. Check DEEPGEN_CPUS environment variable.")
        raise RuntimeError("No CPUs allocated for DeepGen executor")

    # Prepare working directory
    workdir = Path(TEMPFS_DIR) / "deepgen_workdir"
    workdir.mkdir(exist_ok=True, parents=True)
    logger.info(f"Working directory: {workdir}")

    # Bootstrap Project for LLM-powered generation
    project = bootstrap_project(workdir)
    if project:
        logger.info("LLM-powered seed generation enabled")
    else:
        logger.info("LLM-powered seed generation disabled (no project config)")

    # Set CPU affinity for main process
    try:
        if CORES:
            psutil.Process().cpu_affinity([CORES[0]])
            logger.info(f"Set main process CPU affinity to core {CORES[0]}")
    except Exception as e:
        logger.warning(f"Could not set CPU affinity: {e}")

    # Initialize DeepGenEngine
    try:
        logger.info("Initializing DeepGenEngine...")
        # Ensure IPC directory exists for ZMQ
        ipc_dir = Path("/tmp/ipc")
        ipc_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created IPC directory: {ipc_dir}")

        # Log estimated shm usage
        num_cores = len(CORES[1:]) if len(CORES) > 1 else len(CORES)
        script_pool_kb = (SCRIPT_MAX_SIZE * SCRIPT_POOL_SIZE) / 1024
        rb_slots = min(32, SEED_POOL_SIZE)
        per_core_kb = (SEED_MAX_SIZE * SEED_POOL_SIZE + rb_slots * 2 * (512 + 4 * N_EXEC)) / 1024
        total_shm_mb = (script_pool_kb + num_cores * per_core_kb) / 1024
        logger.info(f"[SHM-BUDGET] Script pool: {script_pool_kb:.0f}KB, "
                    f"Per core: {per_core_kb:.0f}KB × {num_cores} = {num_cores * per_core_kb:.0f}KB, "
                    f"Total estimate: {total_shm_mb:.1f}MB (64MB limit)")

        logger.info("Creating DeepGenEngine instance...")
        engine = DeepGenEngine(
            core_ids=CORES[1:] if len(CORES) > 1 else CORES,
            submit_class=SeedCountingZeroMQSubmit,
            submit_kwargs={
                "bind_addr": ROUTER_ADDR,
                "dealer_timeout": DEALER_TIMEOUT,
                "seed_timeout": SEED_TIMEOUT,
            },
            seed_max_size=SEED_MAX_SIZE,
            seed_pool_size=SEED_POOL_SIZE,
            script_max_size=SCRIPT_MAX_SIZE,
            script_pool_size=SCRIPT_POOL_SIZE,
            n_exec=N_EXEC,
            task_para=TASK_PARA,
            shm_label=SHM_LABEL,
            workdir=workdir,
        )
        logger.info("DeepGenEngine instance created successfully")

        logger.info("Entering DeepGenEngine context (await engine.__aenter__())...")
        await engine.__aenter__()
        logger.info("DeepGenEngine.__aenter__() completed successfully")
        logger.info("DeepGenEngine initialized and ready")

        # Start engine in background with error handling
        async def run_engine_with_logging():
            try:
                logger.info("Engine background task: Starting engine.run()")
                await engine.run()
                logger.info("Engine background task: engine.run() completed")
            except Exception as e:
                logger.error(f"Engine background task FAILED: {e}", exc_info=True)

        asyncio.create_task(run_engine_with_logging())
        logger.info("DeepGenEngine background task started")

        # Start budget checker to monitor LLM spend
        global budget_checker
        budget_checker = LiteLLMBudgetChecker(
            check_interval=60,  # Check every 60 seconds
            budget_margin=1.0,  # Stop when less than $1 remaining
        )
        budget_checker.start(engine)

    except Exception as e:
        logger.error(f"Failed to initialize DeepGenEngine: {e}", exc_info=True)
        raise

    # Auto-trigger LLM generation on startup
    if project and engine and DEFAULT_HARNESS_NAME:
        target_harness = DEFAULT_HARNESS_NAME
        if target_harness in project.harnesses:
            logger.info("=" * 40)

            # Always submit AnyHarnessSeedGen task (works for both modes)
            logger.info("[LLM-TASK] AUTO-TRIGGER: Submitting LLM harness analysis task")
            try:
                weighted_models = parse_weighted_models(os.environ.get("LLM_MODELS", "claude-sonnet-4-20250514:1"))
                models_str = ", ".join(weighted_models.keys())
                logger.info(f"[LLM-TASK] Using models: {models_str}")
                task = AnyHarnessSeedGen(
                    project_bundle=project,
                    harness_name=target_harness,
                    harness_entrypoint_func=DEFAULT_HARNESS_ENTRYPOINT,
                    is_jvm=IS_JVM_PROJECT,
                    weighted_models=weighted_models,
                    priority=5,  # Lower priority than diff task
                    num_repeat=1000000,  # Repeat until LLM budget exhausted
                )
                task_id = await engine.add_task(task)
                logger.info(f"[LLM-TASK] AUTO-TRIGGER: Submitted LLM task for harness '{target_harness}' (task_id={task_id})")
            except Exception as e:
                logger.error(f"AUTO-TRIGGER: Failed to submit LLM task for '{target_harness}': {e}")

            # Additionally submit DiffAnalysisTask if in delta mode
            if project.ref_diff:
                logger.info("[LLM-TASK] AUTO-TRIGGER: ref.diff detected, also submitting diff analysis task")
                model = os.environ.get("LLM_MODELS", "claude-sonnet-4-20250514").split(":")[0]
                logger.info(f"[LLM-TASK] Diff analysis using model: {model}")
                try:
                    task = DiffAnalysisTask(
                        project_bundle=project,
                        harness_id=target_harness,
                        model=model,
                        priority=10,  # Higher priority for diff tasks
                        num_repeat=1000000,  # Repeat until LLM budget exhausted
                    )
                    task_id = await engine.add_task(task)
                    logger.info(f"[LLM-TASK] AUTO-TRIGGER: Submitted diff task for harness '{target_harness}' (task_id={task_id})")
                except Exception as e:
                    logger.error(f"AUTO-TRIGGER: Failed to submit diff task for '{target_harness}': {e}")

            logger.info("=" * 40)
        else:
            logger.warning(f"AUTO-TRIGGER: HARNESS_NAME '{target_harness}' not found in project harnesses: {list(project.harnesses.keys())}")
    elif project and engine:
        logger.info("AUTO-TRIGGER: No HARNESS_NAME set, skipping auto-trigger (use /generate_llm or /generate_diff_seeds manually)")

    logger.info("Service startup complete - ready to accept scripts!")

    yield

    # Shutdown
    logger.info("Shutting down service...")

    # Stop budget checker first
    if budget_checker:
        budget_checker.stop()
        logger.info("Budget checker stopped")

    if engine:
        try:
            await engine.__aexit__(None, None, None)
            logger.info("DeepGenEngine shut down successfully")
        except Exception as e:
            logger.error(f"Error during engine shutdown: {e}", exc_info=True)

    logger.info("Service shutdown complete")


# ============================================================================
# FastAPI App
# ============================================================================
app = FastAPI(
    title="DeepGen Script Submission Service",
    description="Simplified service for submitting seed generation scripts",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================================
# Endpoints
# ============================================================================
@app.get("/", response_model=dict)
async def root():
    """Root endpoint"""
    return {
        "service": "DeepGen Service with LLM Support",
        "version": "2.0.0",
        "status": "running" if engine else "initializing",
        "llm_enabled": project is not None,
        "endpoints": {
            "submit": "POST /submit_script",
            "generate_llm": "POST /generate_llm",
            "generate_diff_seeds": "POST /generate_diff_seeds",
            "scripts": "GET /scripts",
            "engine_stats": "GET /engine_stats",
            "status": "GET /status",
            "health": "GET /health",
        }
    }


@app.get("/health", response_model=dict)
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy" if engine else "unhealthy",
        "engine_running": engine is not None,
        "llm_enabled": project is not None,
    }


@app.get("/status", response_model=ServiceStatus)
async def get_status():
    """Get service status"""
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    ref_diff = project.ref_diff if project else None

    # Get budget info from checker
    budget_spend = None
    budget_max = None
    budget_remaining = None
    budget_exhausted = False
    if budget_checker:
        budget_spend = budget_checker.last_spend
        budget_max = budget_checker.last_max_budget
        if budget_spend is not None and budget_max is not None:
            budget_remaining = budget_max - budget_spend
        budget_exhausted = budget_checker.budget_exhausted

    return ServiceStatus(
        running=True,
        default_harness_name=DEFAULT_HARNESS_NAME,
        project_name=project.name if project else None,
        available_harnesses=list(project.harnesses.keys()) if project else [],
        llm_enabled=project is not None,
        diff_mode=ref_diff is not None,
        diff_lines=len(ref_diff.splitlines()) if ref_diff else None,
        cores=CORES,
        shm_label=SHM_LABEL,
        scripts_submitted=script_submission_count,
        llm_generations=llm_generation_count,
        workdir=str(engine.workdir) if engine and hasattr(engine, 'workdir') else str(Path(TEMPFS_DIR) / "deepgen_workdir"),
        llm_budget_spend=budget_spend,
        llm_budget_max=budget_max,
        llm_budget_remaining=budget_remaining,
        llm_budget_exhausted=budget_exhausted,
    )


@app.post("/submit_script", response_model=ScriptSubmissionResponse)
async def submit_script(submission: ScriptSubmission):
    """
    Submit a seed generation script to the DeepGen engine.

    The script must contain a `gen_one_seed()` function that returns bytes.

    Example script:
    ```python
    import random

    def gen_one_seed():
        return b"test_seed_" + str(random.randint(0, 1000)).encode()
    ```

    Parameters:
    - script_content: Python script with gen_one_seed() function (required)
    - harness_name: Target harness name (optional, uses default if not provided)
    - label: Optional tracking label (auto-generated if not provided)
    - priority: Priority 1-100, higher = more important (default: 1)
    - num_repeat: Number of times to repeat (default: 1)
    - max_exec: Max executions, unlimited if not set (default: None)
    """
    global script_submission_count

    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    # Use provided harness or default
    harness_name = submission.harness_name or DEFAULT_HARNESS_NAME
    if not harness_name:
        raise HTTPException(
            status_code=400,
            detail="harness_name must be provided in request or set as HARNESS_NAME env var"
        )

    # Generate label if not provided
    script_submission_count += 1
    label = submission.label or f"script_{script_submission_count}"

    try:
        # Create ScriptLoaderTask
        task = ScriptLoaderTask(
            script_content=submission.script_content,
            harness_name=harness_name,
            label=label,
            priority=submission.priority,
            dev_attempts=1,
            dev_cost=0.0,
            num_repeat=submission.num_repeat,
            max_exec=submission.max_exec,
        )

        # Submit to engine
        task_id = await engine.add_task(task)

        if not task_id:
            raise HTTPException(status_code=500, detail="Failed to add task to engine")

        logger.info(
            f"Script submitted: label={label}, task_id={task_id}, harness={harness_name}, "
            f"priority={submission.priority}, num_repeat={submission.num_repeat}, "
            f"max_exec={submission.max_exec or 'unlimited'}"
        )

        return ScriptSubmissionResponse(
            status="success",
            message=f"Script submitted successfully",
            task_id=task_id,
            label=label,
            harness_name=harness_name,
        )

    except Exception as e:
        logger.error(f"Error submitting script: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to submit script: {str(e)}")


def parse_weighted_models(models_str: str) -> dict[str, int]:
    """Parse weighted models string like 'model1:weight1,model2:weight2'"""
    result = {}
    for item in models_str.split(","):
        item = item.strip()
        if ":" in item:
            model, weight = item.rsplit(":", 1)
            result[model.strip()] = int(weight)
        else:
            result[item] = 1
    return result


@app.get("/scripts", response_model=list[ScriptInfo])
async def list_scripts():
    """
    List all generated scripts in the workdir.

    Returns script metadata and a preview of the content.
    Useful for debugging LLM-generated seed generators.
    """
    import glob

    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    workdir = Path(TEMPFS_DIR) / "deepgen_workdir"
    scripts = []

    # Find all script files
    for script_path in glob.glob(str(workdir / "processor-*" / "script-*.py")):
        try:
            path = Path(script_path)
            content = path.read_text()

            # Parse task label and hash from filename: script-{task_label}-{hash}.py
            filename = path.stem  # script-Any-harness_name-abcd1234
            parts = filename.split("-")
            if len(parts) >= 3:
                task_label = "-".join(parts[1:-1])  # Everything between script- and -hash
                sha256_prefix = parts[-1]
            else:
                task_label = "unknown"
                sha256_prefix = "unknown"

            # Try to determine harness from task label (format: "Any:harness_name")
            harness_name = task_label.split(":")[-1] if ":" in task_label else "unknown"

            scripts.append(ScriptInfo(
                file_path=script_path,
                task_label=task_label,
                harness_name=harness_name,
                sha256=sha256_prefix,
                size_bytes=len(content),
                content_preview=content[:500] + ("..." if len(content) > 500 else "")
            ))
        except Exception as e:
            logger.warning(f"Error reading script {script_path}: {e}")

    return sorted(scripts, key=lambda s: s.file_path)


@app.get("/scripts/{script_hash}")
async def get_script_content(script_hash: str):
    """
    Get full content of a script by its hash prefix.

    Use the hash from /scripts endpoint to retrieve full script content.
    """
    import glob

    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    workdir = Path(TEMPFS_DIR) / "deepgen_workdir"

    # Find script with matching hash
    for script_path in glob.glob(str(workdir / "processor-*" / f"script-*-{script_hash}*.py")):
        try:
            content = Path(script_path).read_text()
            return {
                "file_path": script_path,
                "content": content,
                "size_bytes": len(content)
            }
        except Exception as e:
            logger.error(f"Error reading script {script_path}: {e}")
            raise HTTPException(status_code=500, detail=f"Error reading script: {e}")

    raise HTTPException(status_code=404, detail=f"Script with hash {script_hash} not found")


@app.get("/engine_stats")
async def get_engine_stats():
    """
    Get detailed engine statistics including per-script execution stats.

    Shows executions, errors, seeds generated, and whether scripts are masked.
    """
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    try:
        # Build stats from engine's internal state
        stats = {}
        async with engine._stat_lock:
            for script_id, proc_stats in engine.stats.items():
                script = await engine.get_script(script_id)
                script_hash = script.sha256 if script else "unknown"
                script_path = str(script.file_path) if script else "unknown"

                stats[str(script_id)] = {
                    "script_id": script_id,
                    "script_hash": script_hash,
                    "script_path": script_path,
                    "summary": {
                        "ttl_execs": proc_stats[("summary", None)]["ttl_execs"],
                        "ttl_errors": proc_stats[("summary", None)]["ttl_errors"],
                        "ttl_gen_seeds": proc_stats[("summary", None)]["ttl_gen_seeds"],
                        "ttl_stored_seeds": proc_stats[("summary", None)]["stored_seeds"],
                    }
                }
        return {"scripts": stats}
    except Exception as e:
        logger.error(f"Error getting engine stats: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting stats: {e}")


@app.get("/metrics")
async def get_metrics():
    """
    Get observability metrics for the DeepGen service.

    Returns:
    - total_seeds_sent: Total seeds sent to fuzzer via ZMQ
    - seeds_by_script: Seeds sent per script (by hash prefix)
    - scripts_submitted: Number of scripts submitted via API
    - llm_generations: Number of LLM generation tasks triggered
    - active_dealers: Number of connected ZMQ dealers (fuzzers)
    """
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    # Get submit metrics from SeedCountingZeroMQSubmit
    submit = engine.submit
    metrics = {
        "total_seeds_sent": getattr(submit, 'total_seeds_sent', 0),
        "seeds_by_script": getattr(submit, 'seeds_by_script', {}),
        "scripts_submitted_via_api": script_submission_count,
        "llm_generation_tasks": llm_generation_count,
        "active_dealers": len(getattr(submit, 'dealers', {})),
        "pending_seeds": len(getattr(submit, 'pending_seeds', {})),
    }

    # Add script count from engine
    async with engine._script_lock:
        metrics["total_scripts_loaded"] = len(engine.scripts)
        metrics["masked_scripts"] = sum(1 for mask, _, _, _ in engine.scripts.values() if mask)

    return metrics


@app.post("/generate_llm", response_model=LLMGenerationResponse)
async def generate_llm(request: LLMGenerationRequest):
    """
    Generate seed script using LLM analysis of the harness code.

    This uses libAgents to analyze the harness source code and generate
    a high-quality seed generation script automatically.

    Requires:
    - Project to be bootstrapped (config.json + project.tar.gz in /artifacts)
    - LITELLM_KEY and LITELLM_URL environment variables

    Parameters:
    - harness_name: Target harness (must exist in project config)
    - harness_entrypoint: Function name to analyze (default: LLVMFuzzerTestOneInput)
    - priority: Priority 1-100 (default: 1)
    - num_repeat: Number of times to repeat (default: 1)
    - max_exec: Max executions (default: unlimited)
    """
    global llm_generation_count

    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    if not project:
        raise HTTPException(
            status_code=503,
            detail="LLM generation not available - project not bootstrapped. "
                   "Ensure config.json and project.tar.gz exist in /artifacts"
        )

    # Validate harness exists
    if request.harness_name not in project.harnesses:
        available = list(project.harnesses.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Harness '{request.harness_name}' not found. Available: {available}"
        )

    try:
        # Parse weighted models
        weighted_models = parse_weighted_models(os.environ.get("LLM_MODELS", "claude-sonnet-4-20250514:1"))

        # Create AnyHarnessSeedGen task
        task = AnyHarnessSeedGen(
            project_bundle=project,
            harness_name=request.harness_name,
            harness_entrypoint_func=request.harness_entrypoint,
            is_jvm=IS_JVM_PROJECT,
            weighted_models=weighted_models,
            priority=request.priority,
            num_repeat=request.num_repeat,
            max_exec=request.max_exec,
        )

        # Submit to engine
        task_id = await engine.add_task(task)

        if not task_id:
            raise HTTPException(status_code=500, detail="Failed to add LLM task to engine")

        llm_generation_count += 1
        harness_path = str(project.harness_path_by_name(request.harness_name))

        logger.info(
            f"LLM generation submitted: harness={request.harness_name}, "
            f"task_id={task_id}, entrypoint={request.harness_entrypoint}"
        )

        return LLMGenerationResponse(
            status="success",
            message="LLM seed generation task submitted",
            task_id=task_id,
            harness_name=request.harness_name,
            harness_path=harness_path,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting LLM task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to submit LLM task: {str(e)}")


class DiffGenerationRequest(BaseModel):
    """Request model for diff-based seed generation"""
    harness_name: str = Field(..., description="Target harness name (must exist in project)")
    priority: int = Field(1, ge=1, le=100, description="Priority (1-100)")
    num_repeat: int = Field(1, ge=1, description="Number of times to repeat")


class DiffGenerationResponse(BaseModel):
    """Response model for diff generation"""
    status: str
    message: str
    task_id: Optional[str] = None
    harness_name: str
    diff_lines: int


@app.post("/generate_diff_seeds", response_model=DiffGenerationResponse)
async def generate_diff_seeds(request: DiffGenerationRequest):
    """
    Generate seed script using diff analysis.

    This analyzes the ref.diff to identify vulnerabilities and generates
    targeted seeds to trigger bugs introduced by the patch.

    Requires:
    - Project to be bootstrapped with ref.diff available
    - LITELLM_KEY and LITELLM_URL environment variables

    Parameters:
    - harness_name: Target harness (must exist in project config)
    - priority: Priority 1-100 (default: 1)
    - num_repeat: Number of times to repeat (default: 1)
    """
    global llm_generation_count

    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    if not project:
        raise HTTPException(
            status_code=503,
            detail="Diff generation not available - project not bootstrapped"
        )

    if not project.ref_diff:
        raise HTTPException(
            status_code=503,
            detail="Diff generation not available - no ref.diff found. "
                   "Ensure /ref.diff is mounted or pass --diff to oss-crs"
        )

    # Validate harness exists
    if request.harness_name not in project.harnesses:
        available = list(project.harnesses.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Harness '{request.harness_name}' not found. Available: {available}"
        )

    try:
        # Get model from env
        model = os.environ.get("LLM_MODELS", "claude-sonnet-4-20250514").split(":")[0]

        # Create DiffAnalysisTask
        task = DiffAnalysisTask(
            project_bundle=project,
            harness_id=request.harness_name,
            model=model,
            priority=request.priority,
            num_repeat=request.num_repeat,
        )

        # Submit to engine
        task_id = await engine.add_task(task)

        if not task_id:
            raise HTTPException(status_code=500, detail="Failed to add diff task to engine")

        llm_generation_count += 1
        diff_lines = len(project.ref_diff.splitlines())

        logger.info(
            f"Diff-based generation submitted: harness={request.harness_name}, "
            f"task_id={task_id}, diff_lines={diff_lines}"
        )

        return DiffGenerationResponse(
            status="success",
            message="Diff-based seed generation task submitted",
            task_id=task_id,
            harness_name=request.harness_name,
            diff_lines=diff_lines,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting diff task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to submit diff task: {str(e)}")


# ============================================================================
# Main entry point
# ============================================================================
def run():
    """Run the FastAPI service"""
    import uvicorn

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    logger.info(f"Starting FastAPI server on {SERVICE_HOST}:{SERVICE_PORT}")

    try:
        uvicorn.run(
            app,
            host=SERVICE_HOST,
            port=SERVICE_PORT,
            log_level="info",
        )
        logger.info("uvicorn.run() completed normally")
    except Exception as e:
        logger.error(f"FATAL: uvicorn.run() crashed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    run()
