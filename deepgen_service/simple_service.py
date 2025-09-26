#!/usr/bin/env python3
"""
Simplified DeepGen Service - HTTP API for script submission
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional
import psutil

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator
from contextlib import asynccontextmanager

from libDeepGen.engine import DeepGenEngine
from libDeepGen.submit import ZeroMQSubmit
from libDeepGen.tasks.script_loader import ScriptLoaderTask

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration from environment (set at startup)
# ============================================================================
TEMPFS_DIR = os.environ.get("ENSEMBLER_TMPFS", "/tmpfs")

# Optional default harness name
DEFAULT_HARNESS_NAME = os.environ.get("HARNESS_NAME")

# Engine configuration - with defaults
CORES = list(map(int, os.environ.get("CORES", "1,2,3,4").split(",")))
SHM_LABEL = os.environ.get("SHM_LABEL", "dg_simple")
SEED_MAX_SIZE = int(os.environ.get("SEED_MAX_SIZE", 262144))
SEED_POOL_SIZE = int(os.environ.get("SEED_POOL_SIZE", 10000))
N_EXEC = int(os.environ.get("N_EXEC", 1000))
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
script_submission_count = 0


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
    cores: list[int]
    shm_label: str
    scripts_submitted: int
    workdir: str


# ============================================================================
# Script Counting Submit wrapper
# ============================================================================
class SeedCountingZeroMQSubmit(ZeroMQSubmit):
    """ZeroMQ submit with seed counting"""

    async def request_seed_submit(self, proc_id, script_id, script, seed_ids):
        await super().request_seed_submit(proc_id, script_id, script, seed_ids)
        if seed_ids:
            logger.info(f"Script {script.sha256[:8]} generated {len(seed_ids)} seeds via {proc_id}")


# ============================================================================
# Lifespan management
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic"""
    global engine

    logger.info("=" * 80)
    logger.info("Starting Simplified DeepGen Service")
    logger.info("=" * 80)
    logger.info(f"Default Harness: {DEFAULT_HARNESS_NAME or 'None (must specify in request)'}")
    logger.info(f"Cores: {CORES}")
    logger.info(f"SHM Label: {SHM_LABEL}")
    logger.info(f"Router Address: {ROUTER_ADDR}")
    logger.info("=" * 80)

    # Prepare working directory
    workdir = Path(TEMPFS_DIR) / "deepgen_workdir"
    workdir.mkdir(exist_ok=True, parents=True)
    logger.info(f"Working directory: {workdir}")

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
            n_exec=N_EXEC,
            task_para=TASK_PARA,
            shm_label=SHM_LABEL,
            workdir=workdir,
        )

        await engine.__aenter__()
        logger.info("DeepGenEngine initialized and ready")

        # Start engine in background
        asyncio.create_task(engine.run())
        logger.info("DeepGenEngine background task started")

    except Exception as e:
        logger.error(f"Failed to initialize DeepGenEngine: {e}", exc_info=True)
        raise

    logger.info("Service startup complete - ready to accept scripts!")

    yield

    # Shutdown
    logger.info("Shutting down service...")
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
        "service": "DeepGen Script Submission Service",
        "version": "1.0.0",
        "status": "running" if engine else "initializing",
        "endpoints": {
            "submit": "POST /submit_script",
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
    }


@app.get("/status", response_model=ServiceStatus)
async def get_status():
    """Get service status"""
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    return ServiceStatus(
        running=True,
        default_harness_name=DEFAULT_HARNESS_NAME,
        cores=CORES,
        shm_label=SHM_LABEL,
        scripts_submitted=script_submission_count,
        workdir=str(engine.workdir) if engine and hasattr(engine, 'workdir') else str(Path(TEMPFS_DIR) / "deepgen_workdir"),
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

    uvicorn.run(
        app,
        host=SERVICE_HOST,
        port=SERVICE_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    run()
