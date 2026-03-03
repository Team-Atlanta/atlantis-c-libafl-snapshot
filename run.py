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
import hashlib
import ctypes
import ctypes.util
import struct

SRC_DIR = Path(os.getenv("SRC", "/src"))
OUT_DIR = Path("/out")
WORK_DIR = Path("/work")

BROKER_PORT = 13337
CENTRALIZED_BROKER_PORT = 13338

logging.basicConfig(level=logging.INFO)

# inotify constants
IN_CREATE = 0x00000100
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
INOTIFY_EVENT_SIZE = struct.calcsize('iIII')

IGNORED_SUFFIXES = {'.tmp', '.metadata', '.lafl_lock'}


def watch_and_copy_directory(
    source_dir: Path,
    dest_dir: Path,
    seen_checksums: set,
    lock: threading.Lock,
    stop_event: threading.Event,
    logger: Callable[[str], None]
):
    """
    Watch source_dir using inotify and copy new unique files to dest_dir.

    Files are skipped if:
    - They start with '.' (hidden files)
    - They end with .tmp, .metadata, or .lafl_lock
    - Their SHA256 checksum has already been seen
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Load libc for inotify syscalls
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)

    # Wait for source directory to exist
    while not stop_event.is_set() and not source_dir.exists():
        stop_event.wait(0.5)

    if stop_event.is_set():
        return

    source_dir.mkdir(parents=True, exist_ok=True)

    # Initialize inotify
    fd = libc.inotify_init1(os.O_NONBLOCK)
    if fd < 0:
        logger(f"Failed to initialize inotify: {ctypes.get_errno()}")
        return

    try:
        # Add watch for file creation and close-write events
        wd = libc.inotify_add_watch(
            fd,
            str(source_dir).encode(),
            IN_CREATE | IN_CLOSE_WRITE | IN_MOVED_TO
        )
        if wd < 0:
            logger(f"Failed to add inotify watch: {ctypes.get_errno()}")
            return

        # Process any existing files first
        _process_existing_files(source_dir, dest_dir, seen_checksums, lock, logger)

        # Main event loop
        while not stop_event.is_set():
            # Use select to wait for events with timeout
            readable, _, _ = select.select([fd], [], [], 0.5)

            if not readable:
                continue

            # Read events
            buf = os.read(fd, 4096)
            offset = 0

            while offset < len(buf):
                wd, mask, cookie, name_len = struct.unpack_from('iIII', buf, offset)
                offset += INOTIFY_EVENT_SIZE

                if name_len > 0:
                    name = buf[offset:offset + name_len].rstrip(b'\x00').decode()
                    offset += name_len

                    # Only process on CLOSE_WRITE or MOVED_TO (file is complete)
                    if mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
                        _process_file(source_dir / name, dest_dir, seen_checksums, lock, logger)

    finally:
        os.close(fd)


def _process_existing_files(
    source_dir: Path,
    dest_dir: Path,
    seen_checksums: set,
    lock: threading.Lock,
    logger: Callable[[str], None]
):
    """Process any files that already exist in the source directory."""
    try:
        for entry in source_dir.iterdir():
            if entry.is_file():
                _process_file(entry, dest_dir, seen_checksums, lock, logger)
    except Exception as e:
        logger(f"Error processing existing files: {e}")


def _process_file(
    path: Path,
    dest_dir: Path,
    seen_checksums: set,
    lock: threading.Lock,
    logger: Callable[[str], None]
):
    """Process a single file: check conditions and copy if unique."""
    try:
        if not path.exists() or not path.is_file():
            return

        # Skip hidden files
        if path.name.startswith('.'):
            return

        # Skip unwanted extensions
        if path.suffix in IGNORED_SUFFIXES:
            return

        # Read and checksum
        contents = path.read_bytes()
        checksum = hashlib.sha256(contents).hexdigest()

        with lock:
            if checksum in seen_checksums:
                return
            seen_checksums.add(checksum)

        # Copy to destination
        dest_path = dest_dir / path.name
        dest_path.write_bytes(contents)
        # logger(f"Copied {path.name} ({len(contents)} bytes) to {dest_dir.name}/")

    except Exception as e:
        logger(f"Error processing {path.name}: {e}")

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
            "ASAN_OPTIONS": "verbosity=0:abort_on_error=1:detect_leaks=0:print_stacktrace=0:print_legend=0",
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


ARTIFACTS_DIR = Path("/artifacts")

class LibAFLFuzzerSession(BaseFuzzerSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error_threshold = 100

        # Staging directories (where libafl writes to)
        self.staging_corpus_dir: Path = WORK_DIR / "staging_corpus"
        self.staging_povs_dir: Path = WORK_DIR / "staging_povs"

        # Artifact directories (where watcher copies unique files to)
        self.corpus_dir: Path = ARTIFACTS_DIR / "corpus"
        self.output_dir: Path = ARTIFACTS_DIR / "povs"

        self.fuzzer_config_path: Path = self.work_dir_path / f"fuzzer_config_{self.harness_id}.json"
        self.fuzzer_config = None
        self.fuzzer_log_path: Path = self.work_dir_path / "fuzzer.log"
        self.traceback_collection = False
        self.traceback_lines = 0
        self.traceback_limit = 100

        # Watcher state (shared between corpus and pov watchers)
        self.seen_checksums: set[str] = set()
        self.checksum_lock = threading.Lock()
        self.corpus_watcher_thread = None
        self.povs_watcher_thread = None

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

    def prepare_command(self) -> str:
        """Run the binary directly - it's linked with libfuzzer.so"""
        return str(self.binary)

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
            "output_dir": str(self.staging_povs_dir),
            "corpus_dir": str(self.staging_corpus_dir),
            "log_file": str(self.fuzzer_log_path),
            "cores": self.cores,
            "dictionary_files": [str(p) for p in self.dictionary_files],
            "seed_share_dir": "/seed_share_dir",
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

        # Create staging directories (where libafl writes)
        self.staging_corpus_dir.mkdir(parents=True, exist_ok=True)
        self.staging_povs_dir.mkdir(parents=True, exist_ok=True)

        # Truncate the fuzzer log file to avoid stale data from previous runs
        if self.fuzzer_log_path.exists():
            self.info(f"Truncating existing fuzzer log: {self.fuzzer_log_path}")
            self.fuzzer_log_path.unlink()
        self.fuzzer_log_path.touch()

        # Create artifact directories (where watcher copies to)
        self.corpus_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        fuzzer_env = {
            "LD_LIBRARY_PATH": str(self.artifacts_dir) if self.artifacts_dir else "",
            "FUZZER_CONFIG_PATH": str(self.fuzzer_config_path),
            "FUZZER_LOG_FILE": str(self.fuzzer_log_path),
            "RUST_BACKTRACE": "full",
        }
        if self.verbose:
            fuzzer_env["RUST_LOG"] = "error,libafl::executors::hooks::unix::unix_signal_handler=off"

        self.fuzzer_env.update(fuzzer_env)

        self.setup_fuzzer_config()

    def setup_monitoring(self):
        """Set up monitoring threads including inotify watchers for corpus and povs."""
        super().setup_monitoring()

        # Start corpus watcher thread
        self.corpus_watcher_thread = threading.Thread(
            target=watch_and_copy_directory,
            args=(
                self.staging_corpus_dir,
                self.corpus_dir,
                self.seen_checksums,
                self.checksum_lock,
                self.stop_event,
                self.info
            ),
            daemon=True
        )
        self.corpus_watcher_thread.start()

        # Start povs watcher thread
        self.povs_watcher_thread = threading.Thread(
            target=watch_and_copy_directory,
            args=(
                self.staging_povs_dir,
                self.output_dir,
                self.seen_checksums,
                self.checksum_lock,
                self.stop_event,
                self.info
            ),
            daemon=True
        )
        self.povs_watcher_thread.start()

        self.info(f"Started inotify watchers: {self.staging_corpus_dir} -> {self.corpus_dir}, {self.staging_povs_dir} -> {self.output_dir}")

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


def parse_cpuset(env_var_name='FUZZER_CPUS'):
    """Parse a cpuset environment variable and return 0-indexed core IDs.

    LibAFL's Launcher requires core IDs that are valid from the container's
    perspective. When Docker constrains to a cpuset (e.g., 9-11), the container
    still sees cores as 0, 1, 2 internally for affinity purposes.

    Uses FUZZER_CPUS (set by start.sh) or falls back to OSS_CRS_CPUSET (oss-crs-6) or CPUSET_CPUS (legacy).

    Args:
        env_var_name: Name of the environment variable to parse

    Returns:
        List of integers representing 0-indexed CPU IDs (0 to N-1)

    Raises:
        ValueError: If the env var contains invalid characters
    """
    # FUZZER_CPUS is set by start.sh with the fuzzer's share of CPUs
    # Fall back to OSS_CRS_CPUSET (set by oss-crs-6) or CPUSET_CPUS (legacy)
    cpuset_str = os.getenv("FUZZER_CPUS") or os.getenv("OSS_CRS_CPUSET") or os.getenv("CPUSET_CPUS", "0")

    # Strip whitespace
    cpuset_str = cpuset_str.strip()

    # Handle empty string
    if not cpuset_str:
        cpuset_str = "0"

    # Log for debugging
    logging.info(f"parse_cpuset: cpuset_str='{cpuset_str}' (len={len(cpuset_str)})")

    # Assert only contains numbers and commas
    if not all(c.isdigit() or c == ',' for c in cpuset_str):
        raise ValueError(f"FUZZER_CPUS must only contain numbers and commas, got: '{cpuset_str}' (chars: {[c for c in cpuset_str]})")

    # Count the number of cores specified
    num_cores = len(cpuset_str.split(','))

    # Return 0-indexed core IDs (0, 1, 2, ..., N-1)
    # This is required because LibAFL's Launcher uses these IDs for CPU affinity,
    # and inside a Docker container with cpuset constraints, cores are accessed
    # via their 0-indexed position, not their host core IDs
    return list(range(num_cores))
            
            
# TODO figure out whether input/output corpus is required as interface
def run(harness):
    work_dir_path = Path("/out")
    cores = parse_cpuset()
    logging.info(f"Fuzzer CPUs: {cores} ({len(cores)} cores)")
    fuzzer = LibAFLFuzzerSession(cores, harness, work_dir_path)
    fuzzer.run()
    # Monitor fuzzer process and restart if it exits
    restart_count = 0
    max_restarts = 10
    last_restart_time = time.time()
    min_restart_interval = 5  # seconds between restarts
    stable_time_to_reset = 300  # reset restart counter after 5 minutes of stable operation
    while True:
        if fuzzer.process is not None and fuzzer.process.poll() is not None:
            # Fuzzer process exited
            exit_code = fuzzer.process.returncode
            current_time = time.time()

            # Reset restart count if fuzzer ran stably for a while
            if current_time - last_restart_time > stable_time_to_reset:
                restart_count = 0

            logging.error(f"Fuzzer process exited with code {exit_code}")
            restart_count += 1
            if restart_count > max_restarts:
                logging.error(f"Fuzzer has restarted {max_restarts} times, giving up")
                break

            # Wait a bit before restarting to avoid rapid restart loops
            time_since_last = current_time - last_restart_time
            if time_since_last < min_restart_interval:
                time.sleep(min_restart_interval - time_since_last)

            logging.info(f"Restarting fuzzer (attempt {restart_count}/{max_restarts})...")
            last_restart_time = time.time()

            # Re-run the fuzzer
            fuzzer = LibAFLFuzzerSession(cores, harness, work_dir_path)
            fuzzer.run()
        time.sleep(1)


if __name__ == "__main__":
    # Read harness from environment (set by bin/run_fuzzer)
    harness = os.environ.get("HARNESS_NAME")
    if not harness:
        # Fallback to command-line arg for backward compatibility
        parser = ArgumentParser()
        parser.add_argument("harness")
        parser.add_argument("fuzzer_args", nargs="*")
        args = parser.parse_args()
        harness = args.harness

    run(harness)
