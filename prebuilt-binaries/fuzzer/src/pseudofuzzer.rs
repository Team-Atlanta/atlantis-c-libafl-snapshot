use std::{
    ffi::{OsStr, OsString},
    os::unix::ffi::OsStrExt,
    path::PathBuf,
};
use clap::Parser;

/// A simple frontend that allows you to run the target application's
/// `LLVMFuzzerTestOneInput()` with input data from a file.
#[derive(Parser)]
#[command(version, about, long_about = None)]
struct PseudoCli {
    /// Files to process (last one is used as input to LLVMFuzzerTestOneInput)
    #[arg(required = true)]
    files: Vec<PathBuf>,
}

extern "C" {
    pub fn LLVMFuzzerTestOneInput(data: *const u8, size: usize);
}

fn parse_args(argc: i32, argv: *const *const u8) -> Vec<OsString> {
    // SAFETY: This function should be safe as long as argc and argv
    // conform to their usual structures.
    let argc = argc.max(0) as usize;
    let mut args = Vec::with_capacity(argc);
    for i in 0..argc {
        let ptr = unsafe { *argv.add(i) };
        let mut length = 0;
        while unsafe { *ptr.add(length) } != 0 {
            length += 1;
        }
        let slice = unsafe { std::slice::from_raw_parts(ptr, length) };
        let os_string = OsStr::from_bytes(slice).to_owned();
        args.push(os_string);
    }
    args
}

// From pseudofuzzer
pub fn pseudo_main(argc: i32, argv: *const *const u8) -> i32 {
    let cli = PseudoCli::parse_from(parse_args(argc, argv));
    let input_file = cli.files.last()
        .expect("No input files provided");
    let input = std::fs::read(input_file)
        .unwrap_or_else(|e| panic!("failed to read input file \"{}\": {e}", input_file.display()));

    unsafe {
        LLVMFuzzerTestOneInput(input.as_ptr(), input.len());
    }

    println!("Execution finished");
    0
}
