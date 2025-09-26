# Simplified DeepGen Service

A streamlined HTTP API service for submitting seed generation scripts to the DeepGen engine.

## Overview

This service replaces the complex Kafka-based deepgen_service with a simple REST API that:
- Accepts Python seed generation scripts via HTTP POST
- Validates script syntax
- Loads scripts into the DeepGenEngine for execution
- Distributes generated seeds via ZeroMQ to dealers

## Architecture

```
External Agents → HTTP POST /submit_script → DeepGenEngine
                                               ├─ Executor (runs scripts on CPU cores)
                                               └─ ZeroMQSubmit (sends to dealers)
                                                     └─ Dealers (consume seeds)
```

## Configuration

All configuration is done via environment variables at startup:

### Required
None! The service can run with all defaults.

### Optional
- `HARNESS_NAME`: Default harness name (can be overridden in requests)
- `CORES`: CPU cores to use (default: `1,2,3,4`)
- `SHM_LABEL`: Shared memory label (default: `dg_simple`)
- `ROUTER_ADDR`: ZeroMQ router address (default: `ipc:///tmp/ipc/haha`)
- `SERVICE_HOST`: HTTP server host (default: `0.0.0.0`)
- `SERVICE_PORT`: HTTP server port (default: `8000`)
- `ENSEMBLER_TMPFS`: Temp directory (default: `/tmpfs`)

### Advanced Engine Configuration
- `SEED_MAX_SIZE`: Max seed size in bytes (default: `262144`)
- `SEED_POOL_SIZE`: Seed pool size (default: `10000`)
- `N_EXEC`: Executions per scheduling (default: `1000`)
- `TASK_PARA`: Parallel tasks (default: `3`)
- `DEALER_TIMEOUT`: Dealer timeout seconds (default: `60`)
- `SEED_TIMEOUT`: Seed timeout seconds (default: `300`)

## Running the Service

### Method 1: Direct Python
```bash
# Set optional configuration
export HARNESS_NAME="my_harness"
export CORES="1,2,3,4"
export SERVICE_PORT=8000

# Run the service
python -m deepgen_service.simple_service
```

### Method 2: As a module
```bash
python3 -c "from deepgen_service.simple_service import run; run()"
```

## API Endpoints

### POST /submit_script

Submit a seed generation script to the engine.

**Request Body:**
```json
{
  "script_content": "import random\n\ndef gen_one_seed():\n    return b'test_' + str(random.randint(0, 1000)).encode()",
  "harness_name": "my_harness",
  "label": "agent_v1_script_3",
  "priority": 10,
  "num_repeat": 1,
  "max_exec": 100000
}
```

**Parameters:**
- `script_content` (required): Python script with `gen_one_seed()` function
- `harness_name` (optional): Target harness name (uses HARNESS_NAME env var if not provided)
- `label` (optional): Tracking label (auto-generated if not provided)
- `priority` (optional): Priority 1-100, higher = more important (default: 1)
- `num_repeat` (optional): Number of times to repeat (default: 1)
- `max_exec` (optional): Max executions, unlimited if not set (default: None)

**Response:**
```json
{
  "status": "success",
  "message": "Script submitted successfully",
  "task_id": "abc123",
  "label": "agent_v1_script_3",
  "harness_name": "my_harness"
}
```

### GET /status

Get service status and statistics.

**Response:**
```json
{
  "running": true,
  "default_harness_name": "my_harness",
  "cores": [1, 2, 3, 4],
  "shm_label": "dg_simple",
  "scripts_submitted": 42,
  "workdir": "/tmpfs/deepgen_workdir"
}
```

### GET /health

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "engine_running": true
}
```

### GET /

Root endpoint with service info.

**Response:**
```json
{
  "service": "DeepGen Script Submission Service",
  "version": "1.0.0",
  "status": "running",
  "endpoints": {
    "submit": "POST /submit_script",
    "status": "GET /status",
    "health": "GET /health"
  }
}
```

## Example Usage

### Python Client Example

```python
import requests

# Service URL
SERVICE_URL = "http://localhost:8000"

# Example script
script = '''
import random

def gen_one_seed():
    # Generate a random test seed
    return b"seed_" + str(random.randint(0, 10000)).encode()
'''

# Submit script
response = requests.post(
    f"{SERVICE_URL}/submit_script",
    json={
        "script_content": script,
        "harness_name": "my_harness",
        "label": "random_seed_gen",
        "priority": 5,
        "num_repeat": 1,
        "max_exec": 50000
    }
)

print(response.json())
# Output: {"status": "success", "task_id": "...", "label": "random_seed_gen", "harness_name": "my_harness"}

# Check status
status = requests.get(f"{SERVICE_URL}/status")
print(status.json())
```

### cURL Example

```bash
# Submit a script
curl -X POST http://localhost:8000/submit_script \
  -H "Content-Type: application/json" \
  -d '{
    "script_content": "import random\n\ndef gen_one_seed():\n    return b\"test\"",
    "harness_name": "my_harness",
    "priority": 5
  }'

# Check status
curl http://localhost:8000/status

# Health check
curl http://localhost:8000/health
```

## Script Requirements

All submitted scripts must:

1. **Be valid Python syntax** - Script is compiled to check for syntax errors
2. **Contain `gen_one_seed()` function** - This function will be called repeatedly by the engine
3. **Return bytes** - The function should return a bytes object

### Valid Script Example

```python
import random
import struct

def gen_one_seed():
    """Generate a random binary seed"""
    # Generate random integers
    values = [random.randint(0, 255) for _ in range(100)]
    # Pack as bytes
    return bytes(values)
```

### Invalid Script Examples

```python
# ❌ Missing gen_one_seed function
def generate():
    return b"test"

# ❌ Syntax error
def gen_one_seed():
    return b"test

# ❌ Function name typo
def gen_one_seeds():  # Should be gen_one_seed
    return b"test"
```

## Integration with Agents

This service is designed to work with external script-generating agents:

```python
class MyScriptGenerator:
    def __init__(self, service_url):
        self.service_url = service_url

    def generate_and_submit_script(self, harness_name):
        # Your agent generates a script (using LLM, mutations, etc.)
        script = self.generate_script()

        # Submit to the service
        response = requests.post(
            f"{self.service_url}/submit_script",
            json={
                "script_content": script,
                "harness_name": harness_name,
                "label": f"agent_{self.agent_id}",
                "priority": 10
            }
        )

        return response.json()
```

## Monitoring

The service logs all important events:

```
INFO - Starting Simplified DeepGen Service
INFO - Default Harness: my_harness
INFO - Cores: [1, 2, 3, 4]
INFO - DeepGenEngine initialized and ready
INFO - Service startup complete - ready to accept scripts!
INFO - Script submitted: label=script_1, task_id=abc123, harness=my_harness, priority=5
INFO - Script abc12345 generated 1000 seeds via proc_0
```

## Comparison with Old Service

| Feature | Old Service | New Service |
|---------|-------------|-------------|
| **Input** | Kafka messages | HTTP REST API |
| **Script Generation** | AI-based (OneShotTask, etc.) | External agents |
| **Configuration** | Complex Kafka + many topics | Simple env vars |
| **Dependencies** | Kafka, many services | Just DeepGenEngine + HTTP |
| **Complexity** | ~500 lines, many components | ~350 lines, single file |
| **Use Case** | Integrated fuzzing system | Standalone script executor |

## Troubleshooting

### Service won't start
- Check that required dependencies are installed: `pip install -r requirements.txt`
- Ensure CPU cores are available
- Check port 8000 is not in use (or set `SERVICE_PORT` to different port)

### Script submission fails
- Verify script has valid Python syntax
- Ensure `gen_one_seed()` function exists
- Check harness_name is provided (either in request or HARNESS_NAME env var)

### No seeds generated
- Check the dealer/router is running and accessible at ROUTER_ADDR
- Verify the script logic actually returns bytes
- Check engine logs for execution errors

## Future Enhancements

Potential additions (not implemented):
- Script removal/cancellation endpoint
- Per-script statistics endpoint
- Batch script submission
- WebSocket support for real-time updates
- Script template library
