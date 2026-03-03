#!/bin/bash
# Start script that runs both deepgen_service and the main fuzzer

set -e

# Add /crs to Python path so deepgen_service can be imported as a module
export PYTHONPATH="/crs:${PYTHONPATH:-}"

# ============================================================================
# LiteLLM API configuration
# Map OSS_CRS variables (with fallback to old names for backward compatibility)
# ============================================================================
if [ -n "$OSS_CRS_LLM_API_KEY" ]; then
    export ANTHROPIC_API_KEY="$OSS_CRS_LLM_API_KEY"
    export LITELLM_KEY="$OSS_CRS_LLM_API_KEY"
elif [ -n "$LITELLM_KEY" ]; then
    export ANTHROPIC_API_KEY="$LITELLM_KEY"
fi

if [ -n "$OSS_CRS_LLM_API_URL" ]; then
    export ANTHROPIC_BASE_URL="$OSS_CRS_LLM_API_URL"
    export LITELLM_URL="$OSS_CRS_LLM_API_URL"
elif [ -n "$LITELLM_URL" ]; then
    export ANTHROPIC_BASE_URL="$LITELLM_URL"
fi

# Reduce libAgents logging verbosity (only show warnings and errors)
export LIBAGENTS_LOG_LEVEL="${LIBAGENTS_LOG_LEVEL:-WARNING}"

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

# Read cpuset from OSS_CRS_CPUSET (set by oss-crs-6)
# Fall back to auto-detection from /proc/self/status if not set
if [ -n "$OSS_CRS_CPUSET" ]; then
    CPUSET_STR="$OSS_CRS_CPUSET"
elif [ -n "$CPUSET_CPUS" ]; then
    # Legacy fallback
    CPUSET_STR="$CPUSET_CPUS"
else
    # Auto-detect from kernel
    CPUSET_STR=$(grep "Cpus_allowed_list:" /proc/self/status 2>/dev/null | awk '{print $2}' || echo "0")
fi

# Final fallback to CORES if still not set
CPUSET_STR="${CPUSET_STR:-${CORES:-0}}"

# Strip surrounding quotes (single or double)
CPUSET_STR=$(echo "$CPUSET_STR" | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")

# Convert range format (e.g., "2-5") to comma format (e.g., "2,3,4,5")
if [[ "$CPUSET_STR" =~ ^([0-9]+)-([0-9]+)$ ]]; then
    START=${BASH_REMATCH[1]}
    END=${BASH_REMATCH[2]}
    CPUSET_STR=$(seq -s ',' $START $END)
fi

IFS=',' read -ra CPU_ARRAY <<< "$CPUSET_STR"
NUM_CPUS=${#CPU_ARRAY[@]}

echo "CPU allocation:"
echo "  Total CPUs available: $NUM_CPUS (${CPUSET_STR})"
echo "  CPU_ARRAY contents: ${CPU_ARRAY[*]}"

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

# Strip any whitespace from FUZZER_CPUS
FUZZER_CPUS=$(echo "$FUZZER_CPUS" | tr -d ' ')

echo "  DeepGen CPUs: ${DEEPGEN_CPUS:-none}"
echo "  Fuzzer CPUs: '$FUZZER_CPUS'"
echo "  FUZZER_CPUS length: ${#FUZZER_CPUS}"

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
