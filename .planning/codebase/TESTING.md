# Testing Patterns

**Analysis Date:** 2026-03-03

## Test Framework

**Runner:**
- pytest (with pytest-asyncio for async tests)
- Configuration in `pyproject.toml` (claude-code-sdk-python)
- Async mode: `auto` (tests are detected and run as async automatically)

**Assertion Library:**
- Standard `assert` statements (no special assertion library)
- Example: `assert result.success, f"Fixed script should execute successfully"`

**Run Commands:**
```bash
# Run all tests
python -m pytest tests/

# Watch mode (via pytest-watch or repeated invocation)
# Not explicitly configured

# Coverage
python -m pytest --cov=src tests/
```

## Test File Organization

**Location:**
- Tests are organized parallel to source code in separate `tests/` directories
- libDeepGen: `/home/andrew/post/atlantis-c-libafl-snapshot/libs/libDeepGen/tests/`
- libAgents: `/home/andrew/post/atlantis-c-libafl-snapshot/libs/libAgents/tests/`
- claude-code-sdk-python: Both `tests/` (unit) and `e2e-tests/` (integration)

**Naming:**
- Test files: `test_*.py`
- Test classes: `Test*` (e.g., `TestToolPermissionCallbacks`, `TestMessageTypes`)
- Test functions: `test_*` (e.g., `test_mock_developer`, `test_permission_callback_allow`)

**Structure:**
```
libs/
├── libDeepGen/
│   ├── tests/
│   │   ├── test_engine.py
│   │   ├── test_ringbuffer.py
│   │   ├── test_developers/
│   │   │   ├── test_claude_code.py
│   │   │   └── test_codex.py
│   │   ├── test_executor/
│   │   │   ├── test_directcall.py
│   │   │   └── test_inprocess.py
│   │   ├── test_tasks/
│   │   │   ├── test_script_checker.py
│   │   │   ├── test_script_selector.py
│   │   └── conftest.py
│   └── libDeepGen/
├── libAgents/
│   ├── tests/
│   │   ├── test_agents/
│   │   ├── test_tools/
│   │   ├── test_plugins/
│   │   ├── test_utils/
│   │   └── conftest.py
└── claude-code-sdk-python/
    ├── tests/               # Unit tests
    ├── e2e-tests/          # Integration tests
    └── conftest.py
```

## Test Structure

**Suite Organization:**
```python
# From test_types.py
class TestMessageTypes:
    """Test message type creation and validation."""

    def test_user_message_creation(self):
        """Test creating a UserMessage."""
        msg = UserMessage(content="Hello, Claude!")
        assert msg.content == "Hello, Claude!"

    def test_assistant_message_with_text(self):
        """Test creating an AssistantMessage with text content."""
        text_block = TextBlock(text="Hello, human!")
        msg = AssistantMessage(content=[text_block], model="claude-opus-4-1-20250805")
        assert len(msg.content) == 1
        assert msg.content[0].text == "Hello, human!"
```

**Patterns:**
- Classes group related tests by functionality
- Each test method focuses on single behavior
- Descriptive docstrings explain what is being tested
- Setup/teardown handled via pytest fixtures where needed

## Async Testing

**Framework:** pytest-asyncio

**Pattern:**
```python
@pytest.mark.asyncio
async def test_mock_developer():
    tasks = [TestTask()]

    try:
        with DeepGenEngine(core_ids=[0, 1, 2, 3], model="gpt-4.1-nano", submit_class=AssertMockSubmit) as engine:
            engine.add_developer(MockDeveloper(model="gpt-4.1-nano"))
            await engine.run(
                tasks,
                time_limit=10,
            )
    except Exception as e:
        assert False, f"Exception: {e}"
    finally:
        engine.submit.check_for_exceptions()
```

**Key Features:**
- `@pytest.mark.asyncio` decorator on async test functions
- `async def` and `await` used for async operations
- Can mix async operations with synchronous assertions
- Context managers (`with`) work with async (`async with`)

## Mocking

**Framework:** `unittest.mock` (standard library)

**Patterns:**
```python
# From test_timeout.py
from unittest.mock import patch, AsyncMock

@pytest.mark.asyncio
async def test_timeout():
    with (
        patch(
            "libAgents.agents.deep_search_agent.DeepSearchAgent._query",
            new_callable=AsyncMock,
        ) as mock_internal_query,
        patch.object(
            DeepSearchAgent, "_handle_beast_mode", new_callable=AsyncMock
        ) as mock_beast_mode,
    ):

        async def side_effect(*args, **kwargs):
            await asyncio.sleep(3)
            return "Should not return this"

        mock_internal_query.side_effect = side_effect
        mock_beast_mode.return_value = (False, None)

        agent = DeepSearchAgent()
        result = await agent.query("What is the capital of France?", timeout=1)

        assert result is None
        mock_internal_query.assert_awaited_once()
```

**What to Mock:**
- External dependencies (LLM APIs, file I/O)
- Expensive operations (network calls, long computations)
- Use `AsyncMock` for async functions

**What NOT to Mock:**
- Core business logic (test actual implementations)
- Type conversions and data transformations
- Integration between core components

## Fixtures

**Test Data Setup:**
```python
# From conftest.py (libDeepGen)
@pytest.fixture(scope="session", autouse=True)
def setup_environment():
    env_file_path = os.path.join(os.getcwd(), '.env.base')
    load_dotenv(dotenv_path=env_file_path)

    # pre-clone OSS-Fuzz repo
    repo_url = os.getenv("OSS_FUZZ_REPO_URL")
    branch = os.getenv("OSS_FUZZ_BRANCH", "main")
    oss_fuzz_dir = os.getenv("OSS_FUZZ_CLONE_DIR", "./oss-fuzz")

    if not os.path.isdir(oss_fuzz_dir):
        print(f"Cloning repository from {repo_url} into {oss_fuzz_dir}")
        subprocess.run(["git", "clone", "-b", branch, repo_url, oss_fuzz_dir], check=True)
```

**Location:**
- Fixtures in `conftest.py` at package root or test subdirectory
- Session-scoped fixtures for expensive setup (cloning repos, loading configs)
- Autouse fixtures run automatically without explicit parameter

**Fixture Pattern:**
```python
@pytest.fixture(scope="session", autouse=True)
def load_my_utils():
    """Provide the `get_repo()` function to tests."""
    pytest.get_oss_repo = get_repo_path
    pytest.get_oss_project = get_oss_project_path
```

## Coverage

**Requirements:**
- Not explicitly enforced in any codebase
- Some configuration in claude-code-sdk-python for coverage tooling

**View Coverage:**
```bash
python -m pytest --cov=src --cov-report=html tests/
python -m pytest --cov=src --cov-report=term tests/
```

## Test Types

**Unit Tests:**
- Fast, isolated tests of single functions/methods
- Mock external dependencies
- Most common test type
- Example: `test_types.py` tests type creation and validation

**Integration Tests:**
- Test multiple components working together
- Minimal mocking (only external services)
- Example: `test_engine.py` tests DeepGenEngine with mocked developer but real executor
- Located in `e2e-tests/` for end-to-end tests in claude-code-sdk-python

**E2E Tests:**
- Full workflow tests
- Minimal mocking or none
- Located in `e2e-tests/` directory
- Example: `test_agents_and_settings.py` tests full SDK workflow

## Test Classes and Objects

**Mock Classes (used in tests):**
```python
# From test_engine.py
class MockDeveloper(Developer):
    def gen(self, task: Task) -> tuple[str, float]:
        content = """
<script>
def gen_one_seed():
    return b"mock_seed"
</script>
"""
        processed_content = task.post_process(content)
        token_cost = 0.0
        return processed_content, token_cost


class AssertSubmit(MockEnsemblerSubmit):
    assert_value = b"mock_seed"

    def __init__(self, proc_map: Dict[str, Tuple[str, str]], workdir: Path, ensembler_submit_rb_name: str, ensembler_processed_rb_name: str):
        super().__init__(proc_map, workdir, ensembler_submit_rb_name, ensembler_processed_rb_name)
        self.seed_pool_consumers = {}
        self.exception = None
        self.exception_event = threading.Event()
```

**Mock Transport (for SDK tests):**
```python
# From test_tool_callbacks.py
class MockTransport(Transport):
    """Mock transport for testing."""

    def __init__(self):
        self.written_messages = []
        self.messages_to_read = []
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False
```

## Agent Testing Pattern

**Agent Flow Tests:**
```python
# From test_agent_flow.py
class FetchData(AgentBase):
    @override
    async def run(self, input_data):
        return {"data": [1, 2, 3]}


class Multiply(AgentBase):
    @override
    async def run(self, input_data):
        factor = self.data.get("factor", 1)
        return [x * factor for x in input_data["data"]]


@pytest.mark.asyncio
def test_agent_flow():
    head = FetchData()
    head >> Multiply(factor=10) >> ToString() >> Print()
    res = asyncio.run(head.start())
    assert res == "10, 20, 30"
```

**Notes:**
- Agents connected with `>>` operator
- Data passed through pipeline
- Each agent implements `run()` method
- Tests verify end-to-end flow

## Error Testing

**Pattern:**
```python
# From test_script_checker.py
try:
    executor = InProcessExec(script_content=fixed_script)
    result = executor.exec()

    assert result.success, f"Fixed script should execute successfully on run {i+1}. Error: {result.error}"
    assert result.result is not None, "gen_one_seed should return a value"
    assert isinstance(result.result, bytes), \
        f"gen_one_seed should return bytes, got {type(result.result)}"
except Exception as e:
    pytest.fail(f"Fixed script failed to execute properly: {e}")
```

**Guidelines:**
- Use `pytest.fail()` for explicit test failures with messages
- Assert on result object fields (success, error, etc.)
- Check both success path and error attributes
- Use f-strings in assertion messages for context

## Test Configuration

**pytest.ini (via pyproject.toml):**
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = [
    "--import-mode=importlib",
    "-p", "asyncio",
]

[tool.pytest-asyncio]
asyncio_mode = "auto"
```

**conftest.py:**
- Disables warnings: `config.option.filterwarnings = ["ignore"]`
- Session-scoped setup fixtures
- Provides custom pytest attributes (e.g., `pytest.get_oss_repo`)

---

*Testing analysis: 2026-03-03*
