#!/usr/bin/env python3.12

import os
import subprocess
from pathlib import Path
import shutil

SRC_DIR = Path(os.getenv("SRC", "/src"))
OUT_DIR = Path("/out")
WORK_DIR = Path("/work")
ARTIFACT_DIR = Path("/data/artifacts")
PREFIX = "/src-"
SRC_BACKUP = f"{PREFIX}backup"
WORKDIR = Path.cwd()


def setup_src_copies():
    copies = [
        "libafl",
        "config_gen",
    ]

    # duplicate source code
    for copy in copies:
        shutil.copytree(SRC_DIR, f"{PREFIX}{copy}", symlinks=True)

    # move to backup
    if len(copies) > 0:
        shutil.move(SRC_DIR, SRC_BACKUP)
        

def restore_src_backup():
    shutil.move(SRC_BACKUP, SRC_DIR)
        

def link_src_copy(mode):
    p = Path(f"{PREFIX}{mode}")
    assert p.is_dir(), f"{p} is not a directory"
    if SRC_DIR.is_symlink():
        SRC_DIR.unlink()
    assert not SRC_DIR.exists(), "/src exists and is not symlink"
    SRC_DIR.symlink_to(p)

    # restore cwd
    os.chdir(WORKDIR)
    

def build_libafl():
    mode = "libafl"
    link_src_copy(mode)

    wrappers = ["cc_wrapper", "cxx_wrapper"]
    for artifact in wrappers:
        shutil.copy(ARTIFACT_DIR / artifact, WORK_DIR)

    # needed in /out for running
    libraries = ["libfuzzer.so"]
    for artifact in libraries:
        shutil.copy(ARTIFACT_DIR / artifact, OUT_DIR)

    cc_wrapper_path = WORK_DIR / f"cc_wrapper_{mode}"
    cxx_wrapper_path = WORK_DIR / f"cxx_wrapper_{mode}"
    guest_env_vars = os.environ.copy()
    guest_env_vars.update({
        "CC": str(cc_wrapper_path),
        "CXX": str(cxx_wrapper_path),
        "LIBFUZZER_PATH": str(OUT_DIR / "libfuzzer.so"),
    })
    if guest_env_vars.get("HELPER") != "True":
        guest_env_vars["FUZZING_LANGUAGE"] = "c++"

    env_vars_text = f"ATLANTIS_CC_INSTRUMENTATION_MODE={mode} "

    with cc_wrapper_path.open("w") as f:
        f.write(f'#!/bin/bash\n{env_vars_text}/work/cc_wrapper "$@"')
    with cxx_wrapper_path.open("w") as f:
        f.write(f'#!/bin/bash\n{env_vars_text}/work/cxx_wrapper "$@"')

    os.chmod(cc_wrapper_path, 0o755)
    os.chmod(cxx_wrapper_path, 0o755)

    print(guest_env_vars)
    subprocess.run(["/usr/local/bin/compile"], env=guest_env_vars, check=True)
    # os.execve("compile", [], env=guest_env_vars)
    

def build_config_gen():
    link_src_copy("config_gen")

    config_gen_module = Path("/crs/config_gen")
    # shutil.copytree(config_gen_module, WORK_DIR / "config_gen")

    # update environment if we're not invoked by helper (for testing)
    guest_env_vars = os.environ.copy()
    if guest_env_vars.get("HELPER") != "True":
        guest_env_vars["FUZZING_LANGUAGE"] = "c++"

    # custom compile with config_gen bb
    subprocess.run(["python3.12", str(config_gen_module / "bb.py")], env=guest_env_vars, check=True)


if __name__ == "__main__":
    # NOTE if we need to export more artifacts, then manage copies of /out
    setup_src_copies()
    build_config_gen()
    build_libafl()
    restore_src_backup()
