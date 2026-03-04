# atlantis-c-libafl-snapshot DeepGen Debugging Progress

**Date:** 2026-03-03
**Status:** ROOT CAUSE FOUND - shm_size too small

---

## Root Cause Identified

**DeepGen crashes because Docker container has only 64MB shm_size, but DeepGen needs ~2GB+**

### Evidence
```
docker inspect $CONTAINER --format '{{.HostConfig.ShmSize}}'
# Returns: 67108864 (64MB)
```

### Logs show crash during shared memory allocation:
```
2026-03-04 01:11:01,122 - deepgen_service.simple_service - INFO - Creating DeepGenEngine instance...
/usr/local/lib/python3.12/multiprocessing/resource_tracker.py:224: UserWarning: resource_tracker: There appear to be 1 leaked shared_memory objects to clean up at shutdown
```

---

## Fixes Applied (need verification)

### 1. Reduced pool sizes in simple_service.py (committed c5b0dd2)
```python
# Reduced from 262144 * 10000 = 2.5GB per core
SEED_MAX_SIZE = 65536    # 64KB
SEED_POOL_SIZE = 2000    # = 128MB per core
```

### 2. Added shm_size to oss-crs-6 template (NOT YET WORKING)
File: `~/post/oss-crs-6/oss_crs/src/templates/run-crs-compose.docker-compose.yaml.j2`
Added `shm_size: "2g"` outside conditional block.

**Problem:** Template change not taking effect - container still gets 64MB.

---

## Next Steps

1. **Debug template rendering** - Find why shm_size isn't being applied
   - Check if compose file is generated from different template
   - Verify template syntax is correct (YAML indentation)

2. **Alternative fix** - Add shm_size directly to compose.yaml:
   ```yaml
   # In example/atlantis-c-deepgen/compose2.yaml or similar
   atlantis-c-deepgen:
     shm_size: "2g"
   ```

3. **Verify fix works** - Once shm_size is 2GB:
   - DeepGen should initialize fully
   - Look for "DeepGenEngine initialized and ready" in logs
   - LLM tasks should auto-trigger

---

## Debug Logging Added

### engine.py (libs/libDeepGen)
- `[ENGINE-INIT] Step 1-6` for each initialization phase
- Pinpoints exact crash location in DeepGenEngine.__init__

### executor.py (libs/libDeepGen)
- `[EXECUTOR-INIT]` logs for each core's shared memory creation
- Shows memory sizes being allocated

---

## Files Modified

- `deepgen_service/simple_service.py` - Reduced pool sizes
- `libs/libDeepGen/libDeepGen/engine.py` - Debug logging
- `libs/libDeepGen/libDeepGen/executor/executor.py` - Debug logging
- `~/post/oss-crs-6/oss_crs/src/templates/run-crs-compose.docker-compose.yaml.j2` - Added shm_size (not working yet)

---

## Key Insight

The oss-crs-6 template only sets `shm_size: "2g"` conditionally:
```jinja2
{%- if module_config.run_snapshot or crs.config.is_builder %}
    shm_size: "2g"
{%- endif %}
```

For the runner container, this condition is FALSE, so it gets Docker's default 64MB.

**The fix must ensure shm_size is set unconditionally for runner containers.**
