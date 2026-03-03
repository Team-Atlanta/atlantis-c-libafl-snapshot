#!/usr/bin/env python3.12

import os
import subprocess
from pathlib import Path
import shutil
import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

SRC_DIR = Path(os.getenv("SRC", "/src"))
OUT_DIR = Path("/out")
WORK_DIR = Path("/work")
ARTIFACT_DIR = Path("/data/artifacts")
PREFIX = "/src-"
SRC_BACKUP = f"{PREFIX}backup"
WORKDIR = Path.cwd()


def setup_src_copies():
    logger.info("=== Starting setup_src_copies ===")
    copies = [
        "libafl",
        "config_gen",
    ]
    logger.info(f"Source copies to create: {copies}")
    logger.info(f"SRC_DIR: {SRC_DIR} (exists: {SRC_DIR.exists()})")

    # duplicate source code
    for copy in copies:
        dest = f"{PREFIX}{copy}"
        logger.info(f"Copying {SRC_DIR} -> {dest}")
        shutil.copytree(SRC_DIR, dest, symlinks=True)
        logger.info(f"Successfully created {dest}")

    # move to backup
    if len(copies) > 0:
        logger.info(f"Moving {SRC_DIR} -> {SRC_BACKUP}")
        shutil.move(SRC_DIR, SRC_BACKUP)
        logger.info(f"Successfully backed up to {SRC_BACKUP}")
    logger.info("=== Completed setup_src_copies ===\n")
        

def restore_src_backup():
    logger.info("=== Starting restore_src_backup ===")
    logger.info(f"Restoring {SRC_BACKUP} -> {SRC_DIR}")
    shutil.move(SRC_BACKUP, SRC_DIR)
    logger.info(f"Successfully restored {SRC_DIR}")
    logger.info("=== Completed restore_src_backup ===\n")
        

def link_src_copy(mode):
    logger.info(f"=== Starting link_src_copy(mode={mode}) ===")
    p = Path(f"{PREFIX}{mode}")
    logger.info(f"Target copy path: {p} (is_dir: {p.is_dir()})")
    assert p.is_dir(), f"{p} is not a directory"

    logger.info(f"Checking SRC_DIR: {SRC_DIR} (is_symlink: {SRC_DIR.is_symlink()}, exists: {SRC_DIR.exists()})")
    if SRC_DIR.is_symlink():
        logger.info(f"Unlinking existing symlink at {SRC_DIR}")
        SRC_DIR.unlink()
    assert not SRC_DIR.exists(), "/src exists and is not symlink"

    logger.info(f"Creating symlink: {SRC_DIR} -> {p}")
    SRC_DIR.symlink_to(p)
    logger.info(f"Symlink created successfully")

    # restore cwd
    logger.info(f"Restoring working directory to {WORKDIR}")
    os.chdir(WORKDIR)
    logger.info("=== Completed link_src_copy ===\n")
    

def build_libafl():
    logger.info("=== Starting build_libafl ===")
    mode = "libafl"
    link_src_copy(mode)

    logger.info("Copying compiler wrappers to WORK_DIR")
    wrappers = ["cc_wrapper", "cxx_wrapper"]
    for artifact in wrappers:
        src = ARTIFACT_DIR / artifact
        dst = WORK_DIR / artifact
        logger.info(f"Copying {src} -> {dst}")
        shutil.copy(src, dst)

    # needed in /out for running
    logger.info("Copying libraries to OUT_DIR")
    libraries = ["libfuzzer.so"]
    for artifact in libraries:
        src = ARTIFACT_DIR / artifact
        dst = OUT_DIR / artifact
        logger.info(f"Copying {src} -> {dst}")
        shutil.copy(src, dst)

    cc_wrapper_path = WORK_DIR / f"cc_wrapper_{mode}"
    cxx_wrapper_path = WORK_DIR / f"cxx_wrapper_{mode}"
    logger.info(f"Creating wrapper scripts: {cc_wrapper_path}, {cxx_wrapper_path}")

    guest_env_vars = os.environ.copy()
    guest_env_vars.update({
        "CC": str(cc_wrapper_path),
        "CXX": str(cxx_wrapper_path),
        "LIBFUZZER_PATH": str(OUT_DIR / "libfuzzer.so"),
    })
    if guest_env_vars.get("HELPER") != "True":
        logger.info("HELPER != True, setting FUZZING_LANGUAGE=c++")
        guest_env_vars["FUZZING_LANGUAGE"] = "c++"

    env_vars_text = f"ATLANTIS_CC_INSTRUMENTATION_MODE={mode} "

    logger.info(f"Writing {cc_wrapper_path}")
    with cc_wrapper_path.open("w") as f:
        f.write(f'#!/bin/bash\n{env_vars_text}/work/cc_wrapper "$@"')
    logger.info(f"Writing {cxx_wrapper_path}")
    with cxx_wrapper_path.open("w") as f:
        f.write(f'#!/bin/bash\n{env_vars_text}/work/cxx_wrapper "$@"')

    logger.info("Setting executable permissions on wrappers")
    os.chmod(cc_wrapper_path, 0o755)
    os.chmod(cxx_wrapper_path, 0o755)

    logger.info("Environment variables for compile:")
    for key in ["CC", "CXX", "LIBFUZZER_PATH", "FUZZING_LANGUAGE", "HELPER"]:
        logger.info(f"  {key}={guest_env_vars.get(key, 'NOT SET')}")

    logger.info("Running /usr/local/bin/compile")
    subprocess.run(["/usr/local/bin/compile"], env=guest_env_vars, check=True)
    logger.info("compile finished successfully")

    # Log output artifacts
    logger.info("Checking build outputs:")
    if OUT_DIR.exists():
        out_files = list(OUT_DIR.iterdir())
        logger.info(f"  /out contains {len(out_files)} files:")
        for f in sorted(out_files)[:10]:  # Show first 10
            logger.info(f"    - {f.name}")
        if len(out_files) > 10:
            logger.info(f"    ... and {len(out_files) - 10} more files")

    logger.info("=== Completed build_libafl ===\n")
    # os.execve("compile", [], env=guest_env_vars)
    

def build_config_gen():
    logger.info("=== Starting build_config_gen ===")
    link_src_copy("config_gen")

    config_gen_module = Path("/crs/config_gen")
    logger.info(f"config_gen module path: {config_gen_module} (exists: {config_gen_module.exists()})")
    # shutil.copytree(config_gen_module, WORK_DIR / "config_gen")

    # update environment if we're not invoked by helper (for testing)
    guest_env_vars = os.environ.copy()
    if guest_env_vars.get("HELPER") != "True":
        logger.info("HELPER != True, setting FUZZING_LANGUAGE=c++")
        guest_env_vars["FUZZING_LANGUAGE"] = "c++"

    logger.info("Environment variables for config_gen bb.py:")
    for key in ["FUZZING_LANGUAGE", "HELPER"]:
        logger.info(f"  {key}={guest_env_vars.get(key, 'NOT SET')}")

    # custom compile with config_gen bb
    bb_script = config_gen_module / "bb.py"
    logger.info(f"Running python3.12 {bb_script}")
    subprocess.run(["python3.12", str(bb_script)], env=guest_env_vars, check=True)
    logger.info("config_gen bb.py finished successfully")

    # Log config_gen outputs
    artifacts_dir = Path("/artifacts")
    logger.info("Checking config_gen outputs:")
    if artifacts_dir.exists():
        logger.info(f"  /artifacts directory exists:")
        for f in sorted(artifacts_dir.iterdir()):
            size = f.stat().st_size if f.is_file() else 0
            logger.info(f"    - {f.name} ({size} bytes)")
    else:
        logger.warning("  /artifacts directory does not exist!")

    if OUT_DIR.exists():
        out_files = list(OUT_DIR.iterdir())
        logger.info(f"  /out contains {len(out_files)} files after config_gen:")
        for f in sorted(out_files)[:10]:
            logger.info(f"    - {f.name}")
        if len(out_files) > 10:
            logger.info(f"    ... and {len(out_files) - 10} more files")

    logger.info("=== Completed build_config_gen ===\n")


if __name__ == "__main__":
    logger.info("="*60)
    logger.info("STARTING BUILD.PY")
    logger.info("="*60)
    logger.info(f"Working directory: {WORKDIR}")
    logger.info(f"SRC_DIR: {SRC_DIR}")
    logger.info(f"OUT_DIR: {OUT_DIR}")
    logger.info(f"WORK_DIR: {WORK_DIR}")
    logger.info(f"ARTIFACT_DIR: {ARTIFACT_DIR}")
    logger.info(f"HELPER env var: {os.getenv('HELPER', 'NOT SET')}")
    logger.info("")

    try:
        # NOTE if we need to export more artifacts, then manage copies of /out
        setup_src_copies()
        build_config_gen()
        build_libafl()
        restore_src_backup()

        logger.info("="*60)
        logger.info("BUILD.PY COMPLETED SUCCESSFULLY")
        logger.info("="*60)
    except Exception as e:
        logger.error("="*60)
        logger.error("BUILD.PY FAILED")
        logger.error("="*60)
        logger.error(f"Error: {e}", exc_info=True)
        raise
