#!/bin/bash
# Start script that runs both deepgen_service and the main fuzzer

set -e

# Add /crs to Python path so deepgen_service can be imported as a module
export PYTHONPATH="/crs:${PYTHONPATH:-}"

# Configuration
SERVICE_PORT=${SERVICE_PORT:-8000}
SERVICE_HOST=${SERVICE_HOST:-0.0.0.0}

# Get harness name from first argument (passed by oss-crs command)
# Export it so deepgen_service can access it
export HARNESS_NAME="${1:-${HARNESS_NAME:-}}"

# ============================================================================
# CPU allocation split between deepgen and fuzzer
# - 1 core: fuzzer only, no deepgen
# - 2-3 cores: 1 for deepgen, rest for fuzzer
# - 4+ cores: 2 for deepgen, rest for fuzzer
# ============================================================================
CPUSET_STR="${CPUSET_CPUS:-${CORES:-0}}"
IFS=',' read -ra CPU_ARRAY <<< "$CPUSET_STR"
NUM_CPUS=${#CPU_ARRAY[@]}

echo "CPU allocation:"
echo "  Total CPUs available: $NUM_CPUS (${CPUSET_STR})"

if [ $NUM_CPUS -eq 1 ]; then
    # 1 core: fuzzer only
    DEEPGEN_CPUS=""
    FUZZER_CPUS="${CPU_ARRAY[0]}"
    SKIP_DEEPGEN=1
    echo "  Mode: Single core - fuzzer only"
elif [ $NUM_CPUS -le 3 ]; then
    # 2-3 cores: 1 for deepgen, rest for fuzzer
    DEEPGEN_CPUS="${CPU_ARRAY[0]}"
    FUZZER_CPUS=$(IFS=','; echo "${CPU_ARRAY[*]:1}")
    SKIP_DEEPGEN=0
    echo "  Mode: 2-3 cores - 1 deepgen, rest fuzzer"
else
    # 4+ cores: 2 for deepgen, rest for fuzzer
    DEEPGEN_CPUS="${CPU_ARRAY[0]},${CPU_ARRAY[1]}"
    FUZZER_CPUS=$(IFS=','; echo "${CPU_ARRAY[*]:2}")
    SKIP_DEEPGEN=0
    echo "  Mode: 4+ cores - 2 deepgen, rest fuzzer"
fi

echo "  DeepGen CPUs: ${DEEPGEN_CPUS:-none}"
echo "  Fuzzer CPUs: $FUZZER_CPUS"

# Export for child processes
export DEEPGEN_CPUS
export FUZZER_CPUS

echo ""
echo "Starting services..."
echo "  Port: $SERVICE_PORT"
echo "  Host: $SERVICE_HOST"
echo "  Harness: $HARNESS_NAME"

SERVICE_PID=""
if [ "$SKIP_DEEPGEN" -eq 0 ]; then
    # Start the simple service in background
    python3.12 -m deepgen_service &
    SERVICE_PID=$!
    echo "DeepGen Service started (PID: $SERVICE_PID)"
else
    echo "DeepGen Service SKIPPED (single core mode)"
fi

if [ -n "$SERVICE_PID" ]; then
    # Wait a bit for service to start
    sleep 3

    # Check if service is running
    if ! kill -0 $SERVICE_PID 2>/dev/null; then
        echo "ERROR: DeepGen Service failed to start"
        exit 1
    fi

    echo "DeepGen Service is running at http://$SERVICE_HOST:$SERVICE_PORT"
fi

echo "Starting main fuzzer process..."

# Run the main fuzzer script (pass all arguments)
python3.12 /crs/run.py "$@"

# Cleanup: kill service when main process exits
if [ -n "$SERVICE_PID" ]; then
    kill $SERVICE_PID 2>/dev/null || true
fi
