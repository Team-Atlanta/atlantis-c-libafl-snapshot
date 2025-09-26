import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def get_new_file_path(old_file_path: str, new_project_root: str) -> Optional[str]:
    filename = os.path.basename(old_file_path)
    candidate_paths: List[str] = []

    for root, _, files in os.walk(new_project_root):
        if filename in files:
            candidate_paths.append(os.path.join(root, filename))

    if not candidate_paths:
        return None

    if len(candidate_paths) == 1:
        return candidate_paths[0]

    old_parts = old_file_path.split(os.sep)
    old_parts.reverse()
    best_match = None
    longest_match_length = 0
    longest_match_candidate_parts_count = 0

    for candidate_path in candidate_paths:
        candidate_parts = candidate_path.split(os.sep)
        candidate_parts.reverse()

        match_length = 0
        while (
            match_length < len(old_parts)
            and match_length < len(candidate_parts)
            and old_parts[match_length] == candidate_parts[match_length]
        ):
            match_length += 1

        if match_length > longest_match_length or (
            match_length == longest_match_length
            and len(candidate_parts) < longest_match_candidate_parts_count
        ):
            longest_match_length = match_length
            best_match = candidate_path
            longest_match_candidate_parts_count = len(candidate_parts)

    return best_match


def remove_args(name):
    idx = len(name) - 1
    cnt = 0
    while idx >= 0:
        if name[idx] == ")":
            cnt += 1
        elif name[idx] == "(":
            cnt -= 1
        if cnt == 0:
            break
        idx -= 1
    if idx == 0:
        return name
    return name[:idx]


@dataclass
class LlvmSymbolizerResult:
    function_name: str
    src_file: str
    line_number: int
    error: bool

    def __init__(
        self, function_name: str, src_file: str, line_number: int, error: bool
    ):
        if function_name[-1] == ")":
            function_name = remove_args(function_name)
        self.function_name = function_name
        self.src_file = src_file
        self.line_number = line_number
        self.error = error


class LLVMSymbolizer:
    def __init__(self, harness: str, llvm_symbolizer_path: str):
        self.harness = harness
        self.llvm_symbolizer_path = llvm_symbolizer_path
        self.llvm_symbolizer_process = self._start_llvm_symbolizer()
        self.path_fix_cache: dict[str, str] = {}
        self.src_exists: dict[str, bool] = {}
        self.project_root = os.getenv("CP_PROJ_PATH", "/src")
        self.src_root = os.getenv("CP_SRC_PATH", "/src/repo")

    def _start_llvm_symbolizer(self) -> subprocess.Popen[str]:
        cmd: List[str] = [
            self.llvm_symbolizer_path,
            f"--obj={self.harness}",
            "--pretty-print",
        ]
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if process.stdin:
            os.set_blocking(process.stdin.fileno(), False)
        return process

    def _restart_llvm_symbolizer(self) -> None:
        if self.llvm_symbolizer_process.poll() is None:
            try:
                self.llvm_symbolizer_process.terminate()
                self.llvm_symbolizer_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.llvm_symbolizer_process.kill()
                self.llvm_symbolizer_process.wait()
        self.llvm_symbolizer_process = self._start_llvm_symbolizer()

    def _health_check_symbolizer(self) -> None:
        if self.llvm_symbolizer_process.poll() != None:
            self.llvm_symbolizer_process = self._start_llvm_symbolizer()

    def _fix_src_path(self, path_from_build: str) -> str:
        if path_from_build in self.path_fix_cache:
            return self.path_fix_cache[path_from_build]
        ret = get_new_file_path(path_from_build, self.project_root)
        if ret != None:
            self.path_fix_cache[path_from_build] = ret
            return ret
        ret = get_new_file_path(path_from_build, self.src_root)
        if ret != None:
            self.path_fix_cache[path_from_build] = ret
            return ret
        # if is_running_under_pytest():
        #     path = Path(path_from_build).resolve()
        #     if not str(path).startswith("/usr/local/include") and not str(
        #         path
        #     ).startswith("/usr/include"):
        #         raise FileNotFoundError(
        #             f"File not in project or repo: {path_from_build}"
        #         )
        self.path_fix_cache[path_from_build] = path_from_build
        return path_from_build

    def run_llvm_symbolizer_addr(self, addr: int) -> LlvmSymbolizerResult:
        self._health_check_symbolizer()
        self.llvm_symbolizer_process.stdin.write(hex(addr) + "\n")
        try:
            self.llvm_symbolizer_process.stdin.flush()
        except BlockingIOError:
            self._restart_llvm_symbolizer()
            return self.run_llvm_symbolizer_addr(addr)
        line = self.llvm_symbolizer_process.stdout.readline()
        while True:
            _ = self.llvm_symbolizer_process.stdout.readline()
            if _ == "\n":
                break
            line = _.split("(inlined by)")[1].strip()

        try:
            t = line.split(" at ")
            func_name = t[0]
            l = t[1].split(":")
            src_path = l[0]
            src_line = int(l[1])
            if src_path not in self.src_exists:
                self.src_exists[src_path] = Path(src_path).exists()
            return LlvmSymbolizerResult(
                func_name,
                src_path,
                src_line,
                not (self.src_exists[src_path] and src_line != 0),
            )
        except Exception as e:
            # if is_running_under_pytest():
            #     raise e
            return LlvmSymbolizerResult("", "", -1, True)

    def run_llvm_symbolizer_addrs(self, addr: List[int]) -> List[LlvmSymbolizerResult]:
        num_addrs = len(addr)
        results: List[LlvmSymbolizerResult] = []

        self._health_check_symbolizer()
        input_data = "\n".join([hex(a) for a in addr]) + "\n"
        stdout, _ = self.llvm_symbolizer_process.communicate(input_data)

        symbolizer_results = stdout.split("\n\n")
        if symbolizer_results[-1] == "":
            symbolizer_results = symbolizer_results[:-1]
        # if is_running_under_pytest():
        #     assert (
        #         len(symbolizer_results) == num_addrs
        #     ), f"Expected {num_addrs} results, got {len(symbolizer_results)}"

        for result in symbolizer_results:
            line = result.split("\n")[-1]
            if "(inlined by)" in line:
                line = line.split("(inlined by)")[1].strip()
            try:
                t = line.split(" at ")
                func_name = t[0]
                l = t[1].split(":")
                src_path = l[0]
                src_line = int(l[1])
                if src_path not in self.src_exists:
                    self.src_exists[src_path] = Path(src_path).exists()
                results.append(
                    LlvmSymbolizerResult(
                        func_name,
                        src_path,
                        src_line,
                        not (self.src_exists[src_path] and src_line != 0),
                    )
                )
            except Exception as e:
                # if is_running_under_pytest():
                #     raise e
                results.append(LlvmSymbolizerResult("", "", -1, True))

        return results
