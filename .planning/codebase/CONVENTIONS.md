# Coding Conventions

**Analysis Date:** 2026-03-03

## Naming Patterns

**Files:**
- Module files use snake_case: `script_checker.py`, `task_board.py`, `deep_search_agent.py`
- Test files follow pytest convention: `test_*.py` (e.g., `test_engine.py`, `test_timeout.py`)
- Base/abstract classes typically end with `_base.py`: `task_base.py`, `exec_base.py`, `plugin_base.py`

**Functions:**
- Private/internal methods prefixed with underscore: `_setup_logging()`, `_signal_handler()`, `_main()`
- Public methods use descriptive camelCase words in snake_case: `post_process()`, `get_script()`, `add_script()`
- Async functions follow same naming: `async def run()`, `async def _executor_task_loop()`

**Variables:**
- Local variables use snake_case: `script_id`, `proc_id`, `seed_pool_name`
- Constants in UPPER_SNAKE_CASE: `TEMPFS_DIR`, `DEFAULT_HARNESS_NAME`, `ARTIFACTS_DIR`
- Private class attributes use leading underscore: `_script_lock`, `_stat_lock`, `_should_exit`

**Types:**
- Class names use PascalCase: `DeepGenEngine`, `ExecTask`, `ExecStat`, `AgentBase`
- Type hints use standard Python typing: `list[int]`, `dict | None`, `Optional[str]`
- Modern union syntax preferred over `Union`: `type[SubmitBase] | None` instead of `Union[type[SubmitBase], None]`

## Code Style

**Formatting:**
- Line length target: 88 characters (configured in ruff)
- 4-space indentation (standard Python)
- No explicit formatter enforced in libDeepGen/libAgents; claude-code-sdk-python uses ruff with strict config

**Linting:**
- claude-code-sdk-python uses ruff with rules: E, W, F, I, N, UP, B, C4, PTH, SIM
- Ignores: E501 (line length - handled by implicit wrapping)
- Naming convention: pep8-naming (N)
- No linting enforcement found in libDeepGen/libAgents

**Import Organization:**
- Standard library imports first (e.g., `asyncio`, `json`, `logging`, `os`)
- Third-party imports second (e.g., `pytest`, `atomics`, `yaml`)
- Local imports last with relative paths: `from ..tasks.task_base import Task`
- Imports use absolute module paths from package root: `from libDeepGen.engine import DeepGenEngine`, `from libAgents.agents import AgentBase`

**Path Aliases:**
- Relative imports within package structure: `from ..script import Script`, `from .exec_base import ExecResult`
- No explicit path alias configuration found; uses standard Python package imports

## Error Handling

**Patterns:**
- Broad exception catching with `except Exception as e:` is common for graceful degradation
- Exceptions logged with context information: `logger.warning(f"Executor {self.proc_id} is already running.")`
- Some contexts use `try`/`except` for validation: `if not issubclass(submit_class, SubmitBase): raise ValueError(...)`
- Async tasks use `asyncio.gather(*async_tasks, return_exceptions=True)` to collect exceptions without failing
- No custom exception hierarchy observed; uses built-in exceptions (ValueError, TypeError, etc.)

**Example from engine.py:**
```python
try:
    await self._executor_task_loop()
except Exception as e:
    logger.error(f"Executor loop failed: {e}")
    traceback.print_exc()
```

## Logging

**Framework:** `logging` module (standard library)

**Patterns:**
- Module-level logger: `logger = logging.getLogger(__name__)`
- Levels used: INFO (general flow), WARNING (non-fatal issues), ERROR (failures), DEBUG (detailed trace)
- Log messages include context: `logger.info(f"Using shmem name label: {self.shm_label}, passed arg is {shm_label}")`
- Can be controlled via environment variable: `LIBAGENTS_LOG_LEVEL` sets log level for libAgents loggers
- Early logging configuration in entry points (e.g., `simple_service.py` configures root logger with format string)

**Example from engine.py:**
```python
logger = logging.getLogger(__name__)
logger.info(f"Executor {self.proc_id} started on core {self.core_id}")
logger.warning(f"Executor {self.proc_id} is already running.")
```

## Comments

**When to Comment:**
- Docstrings required for classes and public methods (not consistently applied in libDeepGen)
- Complex logic sections benefit from inline comments explaining the "why"
- YAML configuration files and setup steps documented in docstrings

**JSDoc/TSDoc:**
- Classes documented with module-level docstrings
- Method parameters and returns documented in triple-quoted docstrings
- Example from `DeepGenEngine.__init__`:
```python
"""
Initialize the DeepGenEngine.

Args:
    core_ids: List of CPU core IDs to use for execution
    workdir: Working directory path
    submit_class: Submit class to use for submitting seeds (default: MockSubmit)
    seed_max_size: Max size of each seed in bytes
    ...
"""
```

## Function Design

**Size:**
- Methods typically 5-50 lines for public APIs
- Complex operations split into private helper methods (e.g., `_setup_logging()`, `_init_cache()`)
- Longer functions (100+ lines) found in executor loop logic and graph traversal

**Parameters:**
- Explicit keyword arguments preferred over *args/**kwargs
- Type hints used throughout
- Dataclass-based parameters for structured data: `ExecTask`, `ExecStat`

**Return Values:**
- Functions return meaningful values or structured types
- Async functions return values (not just side effects): `async def gen() -> tuple[str, float]:`
- None explicitly allowed in return type unions when applicable: `Optional[str]`

## Module Design

**Exports:**
- Package `__init__.py` files explicitly export public API with `__all__` list
- Example from `libAgents/agents/__init__.py`:
```python
__all__ = [
    "DeepSearchAgent",
    "AgentBase",
    "FullDiffAnalysisAgent",
    ...
]
```

**Barrel Files:**
- Used to simplify imports: `from libAgents.agents import AgentBase` instead of full path
- Reduces coupling to internal module structure

## Dataclasses

**Usage:**
- Dataclasses used for structured data containers: `@dataclass class ExecTask:`
- Benefits from type hints and automatic `__init__`
- Example:
```python
@dataclass
class ExecTask:
    script_id: int
    script_hash: str

    @classmethod
    def from_script(cls, script_id: int, script: Script) -> "ExecTask":
        return cls(script_id=script_id, script_hash=script.sha256)
```

## Type Hints

**Usage:**
- Type hints on function parameters and returns (especially in sdk and newer code)
- Method signatures often annotated: `async def gen(self, task: Task) -> tuple[str, float]:`
- Python 3.10+ syntax: `list[int]` instead of `List[int]`, `dict | None` instead of `Optional[Dict]`
- Type checking: claude-code-sdk-python uses mypy with strict mode

## Abstract Base Classes

**Pattern:**
- Used for defining interfaces/contracts
- `ABC` with `@abstractmethod` decorators
- Example from `task_base.py`:
```python
class Task(ABC):
    @abstractmethod
    async def _run_impl(self) -> (str, int):
        pass

    @abstractmethod
    def get_label(self) -> str:
        pass
```

## Known Issues

**TODOs/FIXMEs found:**
- Multiple TODO comments for incomplete implementations (e.g., `test_engine.py:73` "Calculate actual token cost")
- Some architectural decisions noted: `model.py:352` FIXME on openai schema validation
- Missing feature implementations: `seedgen_agents.py:59` seed generation logic stub

---

*Convention analysis: 2026-03-03*
