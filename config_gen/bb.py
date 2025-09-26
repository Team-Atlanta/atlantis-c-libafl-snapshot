#!/usr/bin/env python3

# This entire file is bad copypasta...
# My apologies to the libCRS people

import os
from pathlib import Path
import re
import stat
import asyncio
import subprocess
import tarfile
import json

from llvm_symbolizer import LLVMSymbolizer

ALLOWED_FUZZ_TARGET_EXTENSIONS = ["", ".exe"]
FUZZ_TARGET_SEARCH_STRING = "LLVMFuzzerTestOneInput"
VALID_TARGET_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")
BLOCKLISTED_TARGET_NAME_REGEX = re.compile(r"^(jazzer_driver.*)$")

OUT_DIR = Path("/out")

async def async_run_cmd(
    cmd: list, cwd: str | Path | None = None, env=os.environ, timeout: int | None = None
):
    cmd = list(map(str, cmd))
    if isinstance(cwd, Path):
        cwd = str(cwd)
    if timeout:
        cmd = ["timeout", str(timeout)] + cmd
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return await proc.communicate()


def is_executable(file_path):
    """Returns True if |file_path| is an exectuable."""
    return os.path.exists(file_path) and os.access(file_path, os.X_OK)


def is_fuzz_target_local(file_path):
    """Returns whether |file_path| is a fuzz target binary (local path).
    Copied from clusterfuzz src/python/bot/fuzzers/utils.py
    with slight modifications.
    """
    # pylint: disable=too-many-return-statements
    filename, file_extension = os.path.splitext(os.path.basename(file_path))
    if not VALID_TARGET_NAME_REGEX.match(filename):
        # Check fuzz target has a valid name (without any special chars).
        return False

    if BLOCKLISTED_TARGET_NAME_REGEX.match(filename):
        # Check fuzz target an explicitly disallowed name (e.g. binaries used for
        # jazzer-based targets).
        return False

    if file_extension not in ALLOWED_FUZZ_TARGET_EXTENSIONS:
        # Ignore files with disallowed extensions (to prevent opening e.g. .zips).
        return False

    if not is_executable(file_path):
        return False

    if filename.endswith("_fuzzer"):
        return True

    if os.path.exists(file_path) and not stat.S_ISREG(os.stat(file_path).st_mode):
        return False

    with open(file_path, "rb") as file_handle:
        return file_handle.read().find(FUZZ_TARGET_SEARCH_STRING.encode()) != -1


def get_harness_names():
    ret = []
    for path in OUT_DIR.iterdir():
        if is_fuzz_target_local(str(path)):
            ret.append(path.name)
    return ret

def create_conf(
        harnesses: list[str],
        cp_mount_path: str,
):
    """
    Creates {cp_proj_path}/.aixcc/config.yaml
    Side effects: modifies cp_proj_path and cp_src_path
    """

    def normalize_src(src):
        src_path = Path(src)
        for prefix_str, key in [(cp_mount_path, "$REPO"), ("/src", "$PROJECT")]:
            prefix_path = Path(prefix_str)
            # Check if src_path is relative to prefix_path (proper path containment)
            try:
                relative = src_path.relative_to(prefix_path)
                return key + "/" + str(relative)
            except ValueError:
                # src_path is not relative to prefix_path, continue to next prefix
                continue
        return src

    conf = {}

    async def get_key_addr(harness_path):
        cmd = f"nm {harness_path} | grep LLVMFuzzerTestOneInput"
        out, err = await async_run_cmd(["/bin/bash", "-c", cmd])
        try:
            return int(out.decode("utf-8").split(" ")[0], 16)
        except:
            return None

    async def llvm_symbolizer_based(harness):
        print("try llvm_symbolizer_based")
        harness_path = OUT_DIR / harness
        symbolizer_path = OUT_DIR / "llvm-symbolizer"
        key_addr = await get_key_addr(harness_path)
        if key_addr is None:
            return
        symbolizer = LLVMSymbolizer(str(harness_path), str(symbolizer_path))
        ret = symbolizer.run_llvm_symbolizer_addr(key_addr)
        conf[harness] = ret.src_file

    async def update_all():
        jobs = []
        for harness in harnesses:
            jobs.append(llvm_symbolizer_based(harness))
        await asyncio.gather(*jobs)

    asyncio.run(update_all())
    print(conf)
    return conf

if __name__ == '__main__':
    cp_mount_path = Path.cwd()

    subprocess.run(["compile"], check=True)

    harnesses = get_harness_names()
    config = create_conf(harnesses, str(cp_mount_path))
    with open(OUT_DIR / 'config.json', 'w') as f:
        json.dump(config, f)
    
    # Create a single tarball with both /src and cp_mount_path
    def exclude_fuzzers(tarinfo):
        # First exclude the fuzzer directories
        if tarinfo.name.startswith('src/libfuzzer/') or \
           tarinfo.name.startswith('src/aflplusplus/') or \
           tarinfo.name.startswith('src/honggfuzz/'):
            return None

        # Only include files with specific extensions
        allowed_extensions = {
            # C/C++ source and headers
            '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
            # Generated C/C++ files
            '.l', '.y', '.ll', '.yy',  # lex/yacc files
            '.tab.c', '.tab.h',        # bison/yacc generated
            '.lex.c', '.lex.h',        # flex/lex generated
            # Build system files that might contain C/C++ code
            '.cmake', 'CMakeLists.txt',
            # Makefiles and build scripts
            'Makefile', '.mk',
            # Project configuration
            '.config', '.conf',
            # Documentation that might contain code examples
            '.md', '.txt',
            # Shell scripts and Python
            '.sh', '.bash', '.py', '.pyi'
        }
        
        # If it's a directory, include it (we'll filter its contents)
        if tarinfo.isdir():
            return tarinfo
            
        # Check if the file has an allowed extension
        return tarinfo if any(tarinfo.name.endswith(ext) for ext in allowed_extensions) else None

    src_root = "/src-config_gen" # just hardcode it...
    with tarfile.open(OUT_DIR / 'project.tar.gz', 'w:gz') as tar:
        # Add /src with its full path
        tar.add(src_root, arcname='src', filter=exclude_fuzzers)
        # If cp_mount_path is not inside /src, add it with its full path
        if not str(cp_mount_path).startswith(src_root):
            tar.add(cp_mount_path, arcname=str(cp_mount_path).lstrip('/'))


