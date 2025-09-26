import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Any, Optional
import yaml
from libCRS import util, CP

from .llvm_symbolizer import LLVMSymbolizer

logger = logging.getLogger(__name__)

def find_harness_in_directory(harness: str, find_path: Path, prefix: str) -> Optional[str]:
    res = subprocess.run(["find", str(find_path), "-name", harness], capture_output=True, text=True)
    if res.stdout:
        # Take the first match if multiple are found and set obj["path"] to the first match with cp_proj_path replaced with $PROJECT
        lines = res.stdout.strip().split('\n')
        first_match = lines[0]
        if len(lines) > 1:
            logger.warning(f"Multiple harness candidates, {lines}")
            fuzz_filtered = [line for line in lines if "fuzz" in line]
            if fuzz_filtered:
                logger.info(f"Choosing first from filtered list {fuzz_filtered}")
                first_match = fuzz_filtered[0]
        return first_match.replace(str(find_path), prefix)
    return None

def find_harness_path(
        obj,
        cp_proj_path: Path,
        cp_src_path: Path,
        copied_src_path: Path,
        out_dir: Path,
):
    confs_path = obj["path"]
    harness_source_path = Path(confs_path)
    harness_source_name = harness_source_path.name

    # check for the existence of the harness source code
    if confs_path.startswith("$REPO"):
        harness_source_path = Path(confs_path.replace("$REPO", str(copied_src_path)))
    elif confs_path.startswith("$PROJECT"):
        harness_source_path = Path(confs_path.replace("$PROJECT", str(cp_proj_path)))
    if harness_source_path.exists():
        return

    # backup heuristics for finding harness code
    # first check oss-fuzz repo
    result = find_harness_in_directory(harness_source_name, cp_proj_path, "$PROJECT")
    if result:
        obj["path"] = result
        return

    # then check the source code
    result = find_harness_in_directory(harness_source_name, copied_src_path, "$REPO")
    if result:
        obj["path"] = result
        return

    # we're desperate now, check in out
    result = find_harness_in_directory(harness_source_name, out_dir, "$OUT")
    if result:
        obj["path"] = result
        return

    logger.error(f"Ultimately cannot find harness path {confs_path}")

def create_conf(
        harnesses: list[str],
        cp_proj_path: Path,
        cp_src_path: Path,
        copied_src_path: Path,
        out_dir: Path,
        cp_mount_path: str,
        conf_path: Path,
        answer_path_if_exists: Path
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
        ret = await util.async_run_cmd(["/bin/bash", "-c", cmd])
        try:
            return int(ret.stdout.decode("utf-8").split(" ")[0], 16)
        except:
            return None

    async def llvm_symbolizer_based(harness):
        logger.debug("try llvm_symbolizer_based")
        harness_path = out_dir / harness
        symbolizer_path = out_dir / "llvm-symbolizer"
        key_addr = await get_key_addr(harness_path)
        if key_addr == None:
            return
        symbolizer = LLVMSymbolizer(str(harness_path), str(symbolizer_path))
        ret = symbolizer.run_llvm_symbolizer_addr(key_addr)
        conf[harness] = normalize_src(ret.src_file)

    async def update_all():
        jobs = []
        for harness in harnesses:
            jobs.append(llvm_symbolizer_based(harness))
        await asyncio.gather(*jobs)

    asyncio.run(update_all())

    to_yaml = []
    for name in conf:
        obj = {"name": name, "path": conf[name]}
        find_harness_path(obj, cp_proj_path, cp_src_path, copied_src_path, out_dir)
        to_yaml.append(obj)
    leftovers = [str(p) for p in harnesses if str(p) not in conf]
    logger.info(f"harnesses without path {leftovers}")
    for name in leftovers:
        obj = {"name": name}
        to_yaml.append(obj)
    conf_obj = {"harness_files": to_yaml}

    if answer_path_if_exists.exists():
        with open(answer_path_if_exists, "r") as f:
            answer_conf = yaml.safe_load(f)["harness_files"]
            for answer in answer_conf:
                name = answer["name"]
                path = answer["path"]
                if name not in conf:
                    logger.error(f"{name} is not in {conf}")
                    continue
                if conf[name] != path:
                    logger.warning(f"answer conf differs! {conf[name]} vs. {path}")

    return conf_obj
