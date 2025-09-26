use fuzzer_args::remove_fuzzer_args;
use std::env;
use std::path::{Path, PathBuf};
use std::process::{self, Command};

use file_lock::{FileLock, FileOptions};
use libafl_cc::{ClangWrapper, CompilerWrapper, ToolWrapper};
use tempfile::NamedTempFile;
use walkdir::WalkDir;
use which::which;

/// The max version of `LLVM` we're looking for
#[cfg(not(target_vendor = "apple"))]
const LLVM_VERSION_MAX: u32 = 33;

/// The min version of `LLVM` we're looking for
#[cfg(not(target_vendor = "apple"))]
const LLVM_VERSION_MIN: u32 = 6;

const FILE_LOCK_PATH: &str = "/tmp/cc_wrapper.lock";

mod fuzzer_args;

#[derive(Debug)]
enum InstrumentationMode {
    None,
    SymCC,
    SymCCClangCov,
    LibAflCC,
    CustomPass,
    SymQemuHarness,
    SingleInput,
    SingleInputSBCC,
    LibFuzzerSBCC,
}

fn find_llvm_config() -> Result<String, String> {
    if let Ok(var) = env::var("LLVM_CONFIG") {
        return Ok(var);
    }

    for version in (LLVM_VERSION_MIN..=LLVM_VERSION_MAX).rev() {
        let llvm_config_name: String = format!("llvm-config-{version}");
        if which(&llvm_config_name).is_ok() {
            return Ok(llvm_config_name);
        }
    }

    if which("llvm-config").is_ok() {
        return Ok("llvm-config".to_owned());
    }

    Err("could not find llvm-config".to_owned())
}

fn exec_llvm_config(args: &[&str]) -> String {
    let llvm_config = find_llvm_config().expect("Unexpected error");
    match Command::new(llvm_config).args(args).output() {
        Ok(output) => String::from_utf8(output.stdout)
            .expect("Unexpected llvm-config output")
            .trim()
            .to_string(),
        Err(e) => panic!("Could not execute llvm-config: {e}"),
    }
}


fn get_llvm_major() -> usize {
    let llvm_version = exec_llvm_config(&["--version"]);
    if let Some(llvm_version_major) = llvm_version.split(".").next() {
        if let Ok(llvm_version_major) = llvm_version_major.parse::<u32>() {
            return llvm_version_major as usize;
        }
    };
    panic!(
        "Failed to parse LLVM major, llvm-config --version ==> {}",
        llvm_version
    );
}

fn is_x64(path: &PathBuf) -> bool {
    // run readelf -h
    let output = Command::new("readelf")
        .arg("-h")
        .arg(path)
        .output()
        .expect("Failed to execute readelf -h");
    let stdout = String::from_utf8(output.stdout).expect("Failed to convert stdout to string");
    return stdout.contains("ELF64");
}

/// e.g. "/work/libfuzzer.so" -> "-lfuzzer"
fn lib_path_to_link_arg(path: &str) -> String {
    let name_start = match path.rfind("/lib") {
        Some(n) => n + 4,
        None => match path.rfind("lib") {
            Some(n) => n + 3,
            None => 0,
        },
    };
    let name_end = match path[name_start..].find(".so") {
        Some(n) => name_start + n,
        None => path.len(),
    };
    let name = &path[name_start..name_end];

    let mut arg = String::with_capacity(2 + name.len());
    arg.push_str("-l");
    arg.push_str(name);

    arg
}

const LIBRARIES_SEARCH_STRING: &str = "libraries: =";

fn find_profile_runtime_path(clang_path: &PathBuf) -> Option<PathBuf> {
    // execute clang --print-search-dirs and get stdout
    let output = Command::new(clang_path)
        .arg("--print-search-dirs")
        .output()
        .expect("Failed to execute clang --print-search-dirs");
    let stdout = String::from_utf8(output.stdout).expect("Failed to convert stdout to string");
    let libraries_index = stdout
        .find(LIBRARIES_SEARCH_STRING)
        .expect(&format!("Failed to find {}", LIBRARIES_SEARCH_STRING));
    let library_paths: Vec<&str> = stdout[libraries_index + LIBRARIES_SEARCH_STRING.len()..]
        .split(":")
        .map(|s| s.trim())
        .collect();
    for _library_path in library_paths {
        let library_path = PathBuf::from(_library_path);
        // as in python's os.walk, iterate over all files

        for entry in WalkDir::new(library_path)
            .into_iter()
            .filter_map(Result::ok)
        {
            let path = entry.path();
            let path_file_name = path.file_name().unwrap().to_str().unwrap();
            if path_file_name.ends_with(".a") && path_file_name.contains("libclang_rt.profile") {
                if is_x64(&path.to_path_buf()) {
                    return Some(path.to_path_buf());
                }
            }
        }
    }
    None
}

fn does_sudo_exist() -> bool {
    // Better to run a test command than to try to use something like
    // `whereis`, since `whereis` itself may or may not be available
    match Command::new("sudo").arg("-V").spawn() {
        Ok(mut child) => {
            // waiting is required; see https://doc.rust-lang.org/std/process/struct.Child.html#warning
            child.wait().ok();
            true
        },
        Err(e) => false,
    }
}

fn make_sudo_command(name: &str) -> Command {
    if does_sudo_exist() {
        let mut c = Command::new("sudo");
        c.arg(name);
        c
    } else {
        Command::new(name)
    }
}

struct PatchedProfileRuntime {
    copied_file: NamedTempFile,
    restore_path: PathBuf,
    file_lock: FileLock,
}

fn patch_runtime(profile_runtime_path: &PathBuf, wrapped_symbols: &Vec<String>) {
    let copied_file = NamedTempFile::new().expect("Failed to create temporary file");
    std::fs::copy(&profile_runtime_path, copied_file.path())
        .expect("Failed to copy profile runtime");
    let mut redefine_sym_args = Vec::new();
    for s in wrapped_symbols {
        redefine_sym_args.push("--redefine-sym".to_string());
        redefine_sym_args.push(format!("{}=__real_{}", s, s));
    }
    let mut process = make_sudo_command("objcopy")
        .args(&redefine_sym_args)
        .arg(profile_runtime_path)
        .arg(profile_runtime_path)
        .spawn()
        .expect("Failed to execute patch script");
    if !process
        .wait()
        .expect("Failed to wait for patch script")
        .success()
    {
        panic!("Failed to patch profile runtime");
    }
}

fn patch_profile_runtime(
    args: &Vec<String>,
    clang_path: &PathBuf,
) -> Option<PatchedProfileRuntime> {
    // get list of wrapped symbols
    let mut wrapped_symbols = vec![];
    for arg in args.iter() {
        if arg.starts_with("-Wl,--wrap=") {
            wrapped_symbols.push(arg.replace("-Wl,--wrap=", ""));
        }
    }
    if wrapped_symbols.is_empty() {
        return None;
    }
    let file_options = FileOptions::new().create(true).write(true).read(true);
    let file_lock =
        FileLock::lock(FILE_LOCK_PATH, true, file_options).expect("Failed to lock profile runtime");

    // locate profile runtime path
    let profile_runtime_path =
        find_profile_runtime_path(clang_path).expect("Failed to find profile runtime path");

    let copied_file = NamedTempFile::new().expect("Failed to create temporary file");
    std::fs::copy(&profile_runtime_path, copied_file.path())
        .expect("Failed to copy profile runtime");

    patch_runtime(&profile_runtime_path, &wrapped_symbols);
    Some(PatchedProfileRuntime {
        copied_file,
        restore_path: profile_runtime_path,
        file_lock,
    })
}

impl Drop for PatchedProfileRuntime {
    fn drop(&mut self) {
        let mut process = make_sudo_command("cp")
            .arg(self.copied_file.path())
            .arg(&self.restore_path)
            .spawn()
            .expect("Failed to execute patch script");
        if !process
            .wait()
            .expect("Failed to wait for patch script")
            .success()
        {
            panic!("Failed to restore profile runtime");
        }
        self.file_lock
            .unlock()
            .expect("Failed to unlock profile runtime");
    }
}

pub fn main() {
    let mut args: Vec<String> = env::args().collect();

    let instrumentation_mode = match env::var("ATLANTIS_CC_INSTRUMENTATION_MODE")
        .unwrap_or("none".to_string())
        .as_str()
    {
        "none" => InstrumentationMode::None,
        "symcc" => InstrumentationMode::SymCC,
        "symcc_clang_cov" => InstrumentationMode::SymCCClangCov,
        "libafl" => InstrumentationMode::LibAflCC,
        "custom" => InstrumentationMode::CustomPass,
        "symqemu_harness" => InstrumentationMode::SymQemuHarness,
        "single_input" => InstrumentationMode::SingleInput,
        "single_input_sbcc" => InstrumentationMode::SingleInputSBCC,
        "libfuzzer_sbcc" => InstrumentationMode::LibFuzzerSBCC,
        "libfuzzer" => panic!("Don't use atlantis_cc for standard libfuzzer builds"),
        _ => panic!("Unknown instrumentation mode"),
    };

    let linked_to_libfuzzer = match instrumentation_mode {
        InstrumentationMode::LibAflCC
        | InstrumentationMode::SingleInput
        | InstrumentationMode::SingleInputSBCC
        | InstrumentationMode::LibFuzzerSBCC => remove_fuzzer_args(&mut args, false),
        InstrumentationMode::None
        | InstrumentationMode::SymCC
        | InstrumentationMode::SymCCClangCov
        | InstrumentationMode::SymQemuHarness => remove_fuzzer_args(&mut args, true),
        _ => false,
    };

    //println!("args: {:?}", args);

    let mut is_harness = false;
    let keywords = env::var("CP_HARNESS").unwrap_or("pov_harness".to_string());

    for word in keywords.split(":") {
        for arg in args.iter() {
            if arg.contains(&word) {
                is_harness = true;
                break;
            }
        }
    }

    if args.len() > 1 {
        let mut profile_runtime_recovery: Option<PatchedProfileRuntime> = None;
        let mut dir = env::current_exe().unwrap();
        let wrapper_name = dir.file_name().unwrap().to_str().unwrap();

        let is_cpp = match wrapper_name {
            "cc_wrapper" => false,
            "cxx_wrapper" => true,
            _ => panic!("Wrapper name should be either cc_wrapper or cxx_wrapper"),
        };

        dir.pop();

        // set clang and clang++ paths
        let llvm_bindir = exec_llvm_config(&["--bindir"]);
        let bindir_path = Path::new(&llvm_bindir);
        let mut clang = bindir_path.join("clang");
        let mut clangcpp = bindir_path.join("clang++");

        if !clang.exists() {
            clang = PathBuf::from("/usr/local/bin/clang".to_string());
        }

        if !clangcpp.exists() {
            clangcpp = PathBuf::from("/usr/local/bin/clang++".to_string());
        }

        let mut cc = ClangWrapper::new();
        cc.wrapped_cc(clang.clone().to_str().unwrap().to_string());
        cc.wrapped_cxx(clangcpp.clone().to_str().unwrap().to_string());

        if matches!(instrumentation_mode, InstrumentationMode::LibFuzzerSBCC) {
            // This prevents libafl_cc from adding an argument to try to
            // link to libno-link-rt.a, which is a stub library included
            // with libafl_cc that's absent and irrelevant in
            // libfuzzer_sbcc mode
            args.push("--libafl".to_owned());
        }

        cc.cpp(is_cpp)
            // silence the compiler wrapper output, needed for some configure scripts.
            .silence(true)
            .parse_args(&args)
            .expect("Failed to parse the command line");

        let llvm_major = get_llvm_major();

        match instrumentation_mode {
            InstrumentationMode::None => {
                // Instead of using LibAFL's cc, let's just execute the arguments naively
                // One problem with using LibAFL's cc is that it removes -fsanitize=fuzzer to link
                // with its own library, but we don't need that here
                let plain_args = args.clone();
                let compiler = if is_cpp {
                    clangcpp.clone().to_str().unwrap().to_string()
                } else {
                    clang.clone().to_str().unwrap().to_string()
                };
                let mut process = Command::new(&compiler)
                    .args(&plain_args[1..])
                    .spawn()
                    .expect("Failed to execute compiler");
                if let Ok(exit_status) = process.wait() {
                    process::exit(exit_status.code().unwrap());
                } else {
                    panic!("Failed to wait for compiler process");
                }
            }
            InstrumentationMode::CustomPass => todo!(),
            InstrumentationMode::SymCC | InstrumentationMode::SymCCClangCov => {
                let symcc_pass_path = if let Ok(path) = env::var("SYMCC_PASS_PATH") {
                    path
                } else {
                    "/work/libsymcc.so".to_string()
                };
                let arg = format!("-fpass-plugin={}", symcc_pass_path);
                let clang_load_pass_args = if llvm_major < 13 {
                    vec!["-Xclang", "-load", "-Xclang", symcc_pass_path.as_str()]
                } else {
                    vec![arg.as_str()]
                };
                cc.add_args(&clang_load_pass_args);

                let libsymcc_rt_path = if let Ok(path) = env::var("LIBSYMCC_RT_PATH") {
                    path
                } else {
                    "/work/libsymcc-rt.so".to_string()
                };
                let libsymcc_rt_parent_path = Path::new(&libsymcc_rt_path)
                    .parent()
                    .expect("Failed to get parent path")
                    .to_str()
                    .expect("Failed to convert parent path to string")
                    .to_string();

                let arg0 = format!("-L{}", libsymcc_rt_parent_path);
                let arg1 = format!("-Wl,-rpath,{}", libsymcc_rt_parent_path);
                let mut cc_link_args = vec![
                    "-lpthread",
                    "-lm",
                    "-lrt",
                    "-ldl",
                    arg0.as_str(),
                    "-lsymcc-rt",
                    arg1.as_str(),
                    "-Qunused-arguments",
                ];
                if let InstrumentationMode::SymCCClangCov = instrumentation_mode {
                    cc_link_args.push("-fprofile-instr-generate");
                    cc_link_args.push("-fcoverage-mapping");
                    if cc.is_linking() {
                        profile_runtime_recovery = patch_profile_runtime(&args, &clang);
                    }
                }
                cc.add_args(&cc_link_args);
            }
            InstrumentationMode::LibAflCC => {
                cc.add_arg("-fsanitize-coverage=trace-pc-guard,trace-cmp");
                let libfuzzer_path = if let Ok(path) = env::var("LIBFUZZER_PATH") {
                    path
                } else {
                    "/work/libfuzzer.so".to_string()
                };
                let libfuzzer_parent_path = Path::new(&libfuzzer_path)
                    .parent()
                    .expect("Failed to get parent path")
                    .to_str()
                    .expect("Failed to convert parent path to string")
                    .to_string();

                let should_link = is_harness || linked_to_libfuzzer;
                let arg0 = format!("-L{}", libfuzzer_parent_path);
                let arg1 = format!("-Wl,-rpath,{}", libfuzzer_parent_path);
                if should_link {
                    let mut cc_link_args = vec![
                        "-lpthread",
                        "-lm",
                        "-lrt",
                        "-ldl",
                        arg0.as_str(),
                        "-lfuzzer",
                        arg1.as_str(),
                        "-Qunused-arguments",
                    ];

                    let symbols_to_wrap = vec![
                        "__sanitizer_cov_trace_pc_guard_init",
                        "__sanitizer_cov_trace_pc_guard",
                        "__sanitizer_cov_trace_cmp1",
                        "__sanitizer_cov_trace_cmp2",
                        "__sanitizer_cov_trace_cmp4",
                        "__sanitizer_cov_trace_cmp8",
                        "__sanitizer_cov_trace_const_cmp1",
                        "__sanitizer_cov_trace_const_cmp2",
                        "__sanitizer_cov_trace_const_cmp4",
                        "__sanitizer_cov_trace_const_cmp8",
                        "__sanitizer_cov_trace_switch",
                    ];
                    let wrap_args: Vec<String> = symbols_to_wrap
                        .iter()
                        .map(|s| format!("-Wl,--wrap={}", s))
                        .collect();

                    cc_link_args.append(
                        wrap_args
                            .iter()
                            .map(|x| x.as_str())
                            .collect::<Vec<&str>>()
                            .as_mut(),
                    );
                    cc.add_link_args(&cc_link_args);
                }
            }
            InstrumentationMode::SymQemuHarness => {
                let libfuzzer_path = if let Ok(path) = env::var("LIBFUZZER_SYMQEMU_PATH") {
                    path
                } else {
                    "/work/libfuzzer-symqemu.so".to_string()
                };
                let libfuzzer_parent_path = Path::new(&libfuzzer_path)
                    .parent()
                    .expect("Failed to get parent path")
                    .to_str()
                    .expect("Failed to convert parent path to string")
                    .to_string();

                let should_link = is_harness || linked_to_libfuzzer;
                let arg0 = format!("-L{}", libfuzzer_parent_path);
                let arg1 = format!("-Wl,-rpath,{}", libfuzzer_parent_path);
                if should_link {
                    let cc_link_args = vec![
                        arg0.as_str(),
                        "-lfuzzer-symqemu",
                        arg1.as_str(),
                        "-Qunused-arguments",
                    ];
                    cc.add_link_args(&cc_link_args);
                }
            }
            InstrumentationMode::SingleInput | InstrumentationMode::SingleInputSBCC => {
                if matches!(instrumentation_mode, InstrumentationMode::SingleInputSBCC) {
                    cc.add_arg("-fprofile-instr-generate");
                    cc.add_arg("-fcoverage-mapping");
                }
                let libfuzzer_path = if let Ok(path) = env::var("LIBFUZZER_PATH") {
                    path
                } else {
                    "/work/libpseudofuzzer.so".to_string()
                };
                let libfuzzer_parent_path = Path::new(&libfuzzer_path)
                    .parent()
                    .expect("Failed to get parent path")
                    .to_str()
                    .expect("Failed to convert parent path to string")
                    .to_string();

                let should_link = is_harness || linked_to_libfuzzer;
                let arg0 = format!("-L{}", libfuzzer_parent_path);
                let arg1 = format!("-Wl,-rpath,{}", libfuzzer_parent_path);
                let mut libfuzzer_arg = lib_path_to_link_arg(&libfuzzer_path);
                if should_link {
                    let mut cc_link_args = vec![
                        "-lpthread",
                        "-lm",
                        "-lrt",
                        "-ldl",
                        arg0.as_str(),
                        libfuzzer_arg.as_str(),
                        arg1.as_str(),
                        "-Qunused-arguments",
                    ];
                    cc.add_link_args(&cc_link_args);
                }
                if matches!(instrumentation_mode, InstrumentationMode::SingleInputSBCC) {
                    if cc.is_linking() {
                        profile_runtime_recovery = patch_profile_runtime(&args, &clang);
                    }
                }
            }
            InstrumentationMode::LibFuzzerSBCC => {
                cc.add_arg("-fprofile-instr-generate");
                cc.add_arg("-fcoverage-mapping");

                let should_link = is_harness || linked_to_libfuzzer;
                if should_link {
                    cc.add_arg("-fsanitize=fuzzer");

                    // this is probably not needed, but just in case...?
                    let mut cc_link_args = vec![
                        "-Qunused-arguments",
                    ];
                    cc.add_link_args(&cc_link_args);
                } else {
                    cc.add_arg("-fsanitize=fuzzer-no-link");
                }

                if cc.is_linking() {
                    profile_runtime_recovery = patch_profile_runtime(&args, &clang);
                }
            }
        }

        if let Some(code) = cc.run().expect("Failed to run the wrapped compiler") {
            drop(profile_runtime_recovery);
            std::process::exit(code);
        }
    } else {
        panic!("cc: no arguments given");
    }
}
