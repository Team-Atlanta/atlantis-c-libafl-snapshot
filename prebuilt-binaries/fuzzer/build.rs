use std::io::Result;

fn main() -> Result<()> {
    // compile src/libafl_main.c with -c and output should be libafl_main.c.o
    cc::Build::new()
        .file("src/libafl_main.c")
        .flag("-c")
        .compile("libafl_main.c.o");

    let proto_dir = std::env::var("PROTO_DIR").expect("PROTO_DIR is not set");

    let mut proto_file_1 = proto_dir.clone();
    proto_file_1.push_str("/fuzzer-corpus.proto");
    let mut proto_file_2 = proto_dir.clone();
    proto_file_2.push_str("/coverage-service.proto");

    prost_build::compile_protos(&[proto_file_1, proto_file_2], &[proto_dir])
        .expect("fuzzer: failed to compile .proto files");

    Ok(())
}
