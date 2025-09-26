#!/usr/bin/env python3.12

import os
import subprocess
from argparse import ArgumentParser
from pathlib import Path
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Callable
import select
import signal
import fcntl
import json
import configparser
import logging
import threading
import time

SRC_DIR = Path(os.getenv("SRC", "/src"))
OUT_DIR = Path("/out")
WORK_DIR = Path("/work")

BROKER_PORT = 13337
CENTRALIZED_BROKER_PORT = 13338

logging.basicConfig(level=logging.INFO)

@dataclass
class FdHandler:
    buffer: bytes
    processor: Callable[[bytes], None]

@dataclass
class FuzzerStats:
    """Standardized fuzzer statistics"""
    harness_id: str
    exec_sec: float
    coverage: float
    crashes: int

class BaseFuzzerSession(ABC):
    """Base class for fuzzer-specific session implementations."""
    def __init__(
            self,
            cores: list[int],
            harness_id: str,
            work_dir_path: Path,
            initial_corpus_files: list[Path] = None,
            dictionary_files: list[Path] = None, 
            binary: Path = None,
            artifacts_dir: Path | None = None
    ):
        # Core session state
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.error_counter = 0
        self.error_threshold = 10
        self.process = None
        self.panic_detect_thread = None
        self.log_thread = None
        self.verbose = os.environ.get("VERBOSE_FUZZER", "false").lower() in ["true", "1"]

        # Stats tracking
        self.last_info_dump = time.time()
        self.info_interval = 2  # interval for infodumps
        self.latest_stats = None # stores latest stats but can be set to None after flushing
        self.duplicate_stats = False # whether incoming stats are same as previous
        self.snapshot_stats = None # will always store the latest stats
        self.acceptable_speed = 1 # NOTE purely heuristic
        self.frozen_warning_threshold = 60 # flush logs and warn about inactivity
        self.frozen_error_threshold = 300 # flush logs and error about inactivity
        self.log_creation_timeout = 300 # exit if log isn't created after timeout
        self.__fd_handlers = {}

        # Fuzzer configuration
        self.cores: list[int] = cores
        self.original_cores = self.cores
        self.harness_id: str = harness_id
        self.work_dir_path = work_dir_path
        self.initial_corpus_dir: Path = work_dir_path / "initial_corpus"
        self.output_dir: Path = work_dir_path / "crashes"
        # in honggfuzz, corpus and crashes are stored in the same directory
        self.base_env = os.environ.copy()
        self.initial_corpus_files: list[Path] = initial_corpus_files or []
        self.dictionary_files: list[Path] = dictionary_files or []
        self.binary: Path = binary if binary else work_dir_path / harness_id
        self.artifacts_dir: Path | None = artifacts_dir


    def info(self, msg):
        logging.info(f'(FuzzerSession with mode={self.mode}) {msg}')

    def warning(self, msg):
        logging.warning(f'(FuzzerSession with mode={self.mode}) {msg}')

    def error(self, msg):
        logging.error(f'(FuzzerSession with mode={self.mode}) {msg}')

    @property
    @abstractmethod
    def mode(self) -> str:
        """The mode of this fuzzer session. Should be implemented by subclasses."""
        pass

    @property
    @abstractmethod
    def crashes_paths(self) -> list[str]:
        pass

    @property
    @abstractmethod
    def corpus_paths(self) -> list[str]:
        pass

    def prepare_command(self) -> str:
        """Prepare the command to run the fuzzer. Should be implemented by subclasses."""
        return f"run_fuzzer {self.binary.name}"

    def run(self):
        self.setup()
        cmd = self.prepare_command()
        self.info(f'Running fuzzer with cmd: {cmd}')
        self.start(cmd)

        if self.process is None:
            self.error('Fuzzer process not started')
            self.kill()

        self.setup_monitoring()

    def setup_monitoring(self):
        """Set up monitoring threads for the fuzzer"""
        self.panic_detect_thread = threading.Thread(target=self._monitor_std_streams, daemon=True)
        self.panic_detect_thread.start()
        
        if self.needs_logging_thread:
            self.log_thread = threading.Thread(target=self._monitor_log_file, daemon=True)
            self.log_thread.start()
        else:
            self.log_thread = None

    @property
    @abstractmethod
    def needs_logging_thread(self) -> bool:
        """Whether this fuzzer type needs a separate logging thread.
        Must be implemented by subclasses to explicitly declare logging needs."""
        pass

    def setup(self):
        """
        rsync the binary out directory
        populate the initial corpus from seeds syncing or osv analyzer
        copy dictionary files
        set environment variables for run_fuzzer and sanitizers
        create output dir
        """
        # if not IN_K8S:
        #     self.cores = [core + NODE_CPU_CORES * self.node_idx for core in self.cores]

        # setup initial corpus and dicts
        self.initial_corpus_dir.mkdir(parents=True, exist_ok=True)
        # LARGE_DATA_DIR / f"seeds_collector/initial_corpus/{unique_id}:{timestamp}.tar.zst"
        # NOTE run_fuzzer will populate oss-fuzz level corpus
        self.info('No initial corpus files found, creating dummy file...')
        (self.initial_corpus_dir / 'dummy.txt').write_bytes(b'A' * 3)

        engine = self.mode
                
        fuzzer_env = {
            "OUT": str(OUT_DIR),
            "FUZZING_ENGINE": engine,
            "SANITIZER": "address",
            "RUN_FUZZER_MODE": "noninteractive",
            "FUZZER_OUT": str(self.output_dir),
            "CORPUS_DIR": str(self.initial_corpus_dir), # for libfuzzer
            "ASAN_OPTIONS": "abort_on_error=1:detect_leaks=0",
            "TSAN_OPTIONS": "abort_on_error=1",
            "UBSAN_OPTIONS": "abort_on_error=1",
            "MSAN_OPTIONS": "abort_on_error=1",
            "LSAN_OPTIONS": "abort_on_error=1:symbolize=0", # symbolize=0 was from afl
            "CORES": ",".join([str(core) for core in self.cores]),
        }

        self.fuzzer_env = self.base_env.copy()
        self.fuzzer_env.update(fuzzer_env)

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def start(self, cmd: str):
        '''
        start the fuzzer as a background process
        '''
        stderr = subprocess.PIPE
        stdout = subprocess.PIPE
        logging.info("starting")
        self.process = subprocess.Popen(cmd, shell=True, env=self.fuzzer_env, preexec_fn=os.setsid, stdout=stdout, stderr=stderr, cwd=OUT_DIR)

    @abstractmethod
    def _monitor_log_file(self):
        """Monitor and parse the fuzzer's external log file. Override in subclass."""
        pass

    def _should_dump_info(self) -> bool:
        """Check if enough time has passed since last info dump"""
        current_time = time.time()
        delta = current_time - self.last_info_dump

        ret = (
            (not self.duplicate_stats)
            and delta >= self.info_interval
            and self.latest_stats
        )

        errmsg = f"no update to fuzzer log in {delta} seconds"
        
        if delta >= self.frozen_warning_threshold and int(delta) % 10 == 0 and delta - int(delta) <= 0.11:
            # force log flush even if duplicate. handles if exec is still too slow
            self.warning(errmsg)
            if self.snapshot_stats:
                self.latest_stats = self.snapshot_stats
                return True
        return ret

    def _update_info_dump_time(self):
        """Update the last info dump timestamp"""
        self.last_info_dump = time.time()

    def _dump_stats_if_needed(self, stats: FuzzerStats):
        """Store stats and dump if interval has passed"""
        self.duplicate_stats = stats == self.snapshot_stats
        self.latest_stats = stats
        self.snapshot_stats = stats
        if self._should_dump_info():
            self._dump_stats()
            self._update_info_dump_time()

    def _dump_stats(self):
        """Dump the latest stats"""
        if not self.latest_stats:
            return
        stats = self.latest_stats
        if stats.exec_sec < self.acceptable_speed and self.mode:
            self._handle_fuzzer_error(f"Fuzzer {self.harness_id} is running too slow, exec_sec={stats.exec_sec}")
        self.info(f"harness={stats.harness_id} exec_sec={stats.exec_sec} coverage={stats.coverage} crashes={stats.crashes}")
        self.latest_stats = None

    def __make_line_processor(self, log_method, handler):
        """Factory function to create line processors"""
        def process_line(line_bytes):
            try:
                output = line_bytes.decode("utf-8")
                if self.verbose and output.strip():
                    log_method(output.rstrip('\n'))
                handler(output)
            except UnicodeDecodeError:
                pass
        return process_line

    def __handle_fd_data(self, fd):
        """Generic handler for file descriptor data"""
        handler = self.__fd_handlers[fd]
        try:
            # Read available data (non-blocking)
            chunk = os.read(fd, 4096)
            if not chunk:  # EOF
                # Process any remaining buffered data as final line
                if handler.buffer:
                    handler.processor(handler.buffer)
                    handler.buffer = b""
                return
            
            # Add to buffer
            handler.buffer += chunk
            
            # Process complete lines
            while b'\n' in handler.buffer:
                line, handler.buffer = handler.buffer.split(b'\n', 1)
                handler.processor(line + b'\n')
                
        except (OSError, BlockingIOError):
            # No data available (shouldn't happen after select, but be safe)
            pass

    def __setup_fd_handlers(self):
        stderr_fd = self.process.stderr.fileno()
        stdout_fd = self.process.stdout.fileno()
        fcntl.fcntl(stderr_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        fcntl.fcntl(stdout_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        self.__fd_handlers = {
            stderr_fd: FdHandler(
                buffer=b"",
                processor=self.__make_line_processor(self.error, self._handle_error_output),
            ),
            stdout_fd: FdHandler(
                buffer=b"",
                processor=self.__make_line_processor(self.info, self._handle_stdout_output),
            ),
        }
        
    def _monitor_std_streams(self):
        """Monitor and handle stdout and stderr streams from the fuzzer process"""
        self.__setup_fd_handlers()
        
        while not self.stop_event.is_set():
            try:
                ready, _, _ = select.select(list(self.__fd_handlers.keys()), [], [], 0.1)
                if not ready:
                    # Check if we need to dump stats even without new output
                    if self._should_dump_info() and self.latest_stats:
                        self._dump_stats()
                        self._update_info_dump_time()
                    continue

                for fd in ready:
                    self.__handle_fd_data(fd)
            except OSError:
                self.__setup_fd_handlers()

    @abstractmethod
    def _handle_error_output(self, error_output: str):
        """Handle fuzzer-specific error output. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def _handle_stdout_output(self, output: str):
        """Handle fuzzer-specific stdout output. Must be implemented by subclasses."""
        pass
    
    def _handle_fuzzer_error(self, error_msg: str):
        """Common error handling logic for fuzzer errors"""
        with self.lock:
            # any panic detection should be considered as runtime error
            self.error_counter += 1
            if self.error_counter > self.error_threshold:
                logging.info("Fuzzer will now die")
                os._exit(0)
        self.error(error_msg)


class LibAFLFuzzerSession(BaseFuzzerSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error_threshold = 100
        self.corpus_dir: Path = self.work_dir_path / "corpus"
        self.fuzzer_config_path: Path = self.work_dir_path / f"fuzzer_config_{self.harness_id}.json"
        self.fuzzer_config = None
        self.fuzzer_log_path: Path = self.work_dir_path / "fuzzer.log"
        self.traceback_collection = False
        self.traceback_lines = 0
        self.traceback_limit = 100

    @property
    def mode(self) -> str:
        return "libafl"

    @property
    def needs_logging_thread(self) -> bool:
        """LibAFL needs a separate logging thread since it doesn't use stdout for stats"""
        return True

    @property
    def crashes_paths(self) -> list[str]:
        return [str(self.output_dir)]

    @property
    def corpus_paths(self) -> list[str]:
        return [str(self.corpus_dir)]

    def check_setup(self):
        if not self.fuzzer_config_path.exists():
            self.info('Fuzzer config path does not exist, fuzzer not setup correctly')
            return False
        return True
    
    def get_fuzzer_config(self):
        """
        fuzzer_config = {
            "broker_port" : config.BROKER_PORT,
            "centralized_broker_port" : config.CENTRALIZED_BROKER_PORT,
            "campaign_id" : harness_id,
            "harness_id" : harness_id,
            "initial_corpus_dir" : str(initial_corpus_dir),
            "output_dir" : str(output_dir),
            "kafka_broker_addr" : kafka_hostname,
            "kafka_seed_additions_topic" : FUZZER_SEED_ADDITIONS_TOPIC,
            "kafka_seed_requests_topic": FUZZER_SEED_REQUESTS_TOPIC,
            "kafka_seed_updates_topic": FUZZER_SEED_UPDATES_TOPIC,
            "corpus_dir" : str(corpus_dir),
            "log_file" : str(log_file_path),
            "cores" : cores,
            "dictionary_files": [str(p) for p in dictionary_files],
        }
        """
        with self.fuzzer_config_path.open('r') as f:
            self.fuzzer_config = json.load(f)
            self.info(f'Fuzzer config: {self.fuzzer_config}')
    
    def setup_fuzzer_config(self):
        """Set up the fuzzer configuration and environment"""
        # use the mounted_work_dir since the actual fuzzer will use this

        # NOTE without kafka_broker_addr, fuzzer works fine by not including kafka consumer in stages
        fuzzer_config = {
            "broker_port": BROKER_PORT,
            "centralized_broker_port": CENTRALIZED_BROKER_PORT,
            "campaign_id": self.harness_id,
            "harness_id": self.harness_id,
            "initial_corpus_dir": str(self.initial_corpus_dir),
            "output_dir": str(self.output_dir),
            "corpus_dir": str(self.corpus_dir),
            "log_file": str(self.fuzzer_log_path),
            "cores": self.cores,
            "dictionary_files": [str(p) for p in self.dictionary_files],
        }

        # dot-options parsing
        options_file = self.work_dir_path / f'{self.harness_id}.options'
        if options_file.is_file():
            parser = configparser.ConfigParser()
            parser.read(options_file)
            
            libfuzzer_section = "libfuzzer"
            if parser.has_section(libfuzzer_section):
                options = parser[libfuzzer_section]
                if "dict" in options:
                    dict_path = self.work_dir_path / options["dict"]
                    fuzzer_config["dictionary_files"].append(str(dict_path))
                if "max_len" in options:
                    fuzzer_config["max_len"] = int(options["max_len"])
                if "timeout" in options:
                    fuzzer_config["timeout"] = int(options["timeout"])
        
        # setup work_dir
        with self.fuzzer_config_path.open("w", encoding="utf-8") as f:
            json.dump(fuzzer_config, f)

        self.fuzzer_config = fuzzer_config

    def setup(self):
        super().setup()

        fuzzer_env = {
            "LD_LIBRARY_PATH": str(self.artifacts_dir) if self.artifacts_dir else "",
            "FUZZER_CONFIG_PATH": str(self.fuzzer_config_path),
            "FUZZER_LOG_FILE": str(self.fuzzer_log_path),
            "RUST_BACKTRACE": "full",
        }
        if self.verbose:
            fuzzer_env["RUST_LOG"] = "info"
        
        self.fuzzer_env.update(fuzzer_env)
        
        self.setup_fuzzer_config()

    def _handle_error_output(self, error_output: str):
        """Handle LibAFL-specific error output including panic traceback collection"""
        if "thread '<unnamed>' panicked" in error_output:
            self.traceback_collection = True
            self._handle_fuzzer_error("***libafl runtime error, please fallback to libafl***, this is actual runtime error")
        
        if self.traceback_collection:
            self.error(error_output)
            self.traceback_lines += 1
            
            if self.traceback_lines >= self.traceback_limit:
                self.error("Thread panic detected, killing process")
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except Exception as e:
                    self.error(f"Error killing process: {e}")
                finally:
                    os.killpg(self.process.pid, signal.SIGKILL)
                if not self.stop_event.is_set():
                    # restart fuzzer
                    self.info("Restarting fuzzer...")
                    self.start(self.prepare_command())
                self.traceback_lines = 0
                self.traceback_collection = False

    def _handle_stdout_output(self, output: str):
        """Handle LibAFL stdout - no special handling needed"""
        pass

    def second_last_line(self):
        result = subprocess.run(f'tail -n2 "{self.fuzzer_log_path}" | head -n1',
                shell=True,
                capture_output=True,
                text=True
            )
        return result.stdout.strip()

    def _monitor_log_file(self):
        """Monitor LibAFL log file"""
        # First check if log file exists
        check = 0
        sleep_interval = 0.5
        while not self.stop_event.is_set():
            if not self.fuzzer_log_path.exists():
                check += 1
                if check > int(self.log_creation_timeout // sleep_interval):
                    self.send_runtime_failure_message(f"LibAFL log file not created after {self.log_creation_timeout} seconds")
                    return
                time.sleep(sleep_interval)
                continue
            break

        # Now monitor the log file
        while not self.stop_event.is_set():
            try:
                # might not exist yet
                tail = self.second_last_line()
                if tail:
                    raw_stats = json.loads(tail)
                    exec_sec = raw_stats["exec_sec"]
                    num = 0
                    denom = 0
                    for c in raw_stats["client_stats"]:
                        if c["enabled"]:
                            ratio = c["user_monitor"]["edges"]["value"]["Ratio"]
                            num += ratio[0]
                            denom += ratio[1]
                    coverage = num / denom if denom > 0 else 0
                    blist = {".lafl_lock", ".tmp", ".metadata"}
                    crashes = sum(1 for entry in self.output_dir.iterdir()
                                  if entry.is_file() and entry.suffix not in blist)
                    
                    stats = FuzzerStats(
                        harness_id=self.harness_id,
                        exec_sec=exec_sec,
                        coverage=coverage,
                        crashes=crashes
                    )
                    self._dump_stats_if_needed(stats)
            except Exception as e:
                self._handle_fuzzer_error(f"Error logging fuzzer: {e}")
            self.stop_event.wait(self.info_interval) # wakes up when stop_event is set


def parse_cpuset(env_var_name='CPUSET_CPUS'):
    """Parse a cpuset environment variable into a list of integers.
    
    Args:
        env_var_name: Name of the environment variable to parse
        
    Returns:
        List of integers representing CPU IDs
        
    Raises:
        ValueError: If the env var contains invalid characters
        KeyError: If the env var is not set
    """
    cpuset_str = os.getenv("CPUSET_CPUS", "0")
    
    # Assert only contains numbers and commas
    if not all(c.isdigit() or c == ',' for c in cpuset_str):
        raise ValueError(f"{env_var_name} must only contain numbers and commas")
    
    # Split on commas and convert to integers
    cpu_list = [int(cpu) for cpu in cpuset_str.split(',')]
    
    return cpu_list
            
            
# TODO figure out whether input/output corpus is required as interface
def run(harness):
    work_dir_path = Path("/out")
    cores = parse_cpuset()
    logging.info(f"Running with {len(cores)} cores")
    fuzzer = LibAFLFuzzerSession(cores, harness, work_dir_path)
    fuzzer.run()
    # wait for panic handler to kill fuzzer, otherwise let subprocess cook
    while True:
        time.sleep(1)


if __name__ == "__main__":
    # Request a haiku in the background to demonstrate LLM integration
    print("\n" + "="*60)
    print("Starting haiku request in background...")
    print("="*60)
    try:
        haiku_process = subprocess.Popen(
            ["/venv-deepgen/bin/python3", "/crs/haiku.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        logging.info(f"Haiku request started in background (PID: {haiku_process.pid})")
    except Exception as e:
        logging.warning(f"Failed to start haiku request: {e}")
        # Don't fail the runner if haiku request fails to start

    parser = ArgumentParser()
    parser.add_argument("harness")
    parser.add_argument("fuzzer_args", nargs="*")
    args = parser.parse_args()
    run(args.harness)

    # alternative interface with env vars
    # harness = os.environ.get("FUZZER")
    # run(harness)
