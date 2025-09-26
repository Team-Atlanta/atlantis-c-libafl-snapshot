use std::{
    cell::RefCell,
    env,
    fs::File,
    io::Write,
    path::{Path, PathBuf},
    process::exit,
};

use regex::{Regex, RegexBuilder};
use symcc_libafl::clone_symcc;

const SYMCC_RUNTIME_FUNCTION_NAME_PREFIX: &str = "_cpp_";

thread_local! {
    static FUNCTION_NAME_REGEX: RefCell<Regex> = RefCell::new(Regex::new(r"pub fn (\w+)").unwrap());
    static EXPORTED_FUNCTION_REGEX: RefCell<Regex> = RefCell::new(RegexBuilder::new(r"(pub fn \w+\s*\([^\)]*\)[^;]*);")
        .multi_line(true)
        .build()
        .unwrap());
}

fn main() {
    let out_path = PathBuf::from(env::var("OUT_DIR").unwrap());
    let intree_symcc_path = PathBuf::from("./symcc");
    let symcc_src_path = checkout_symcc(&intree_symcc_path, &out_path);

    write_rust_runtime_macro_file(&out_path, &symcc_src_path);

    if env::var("TARGET").unwrap().contains("linux") {
        let cpp_bindings = bindgen::Builder::default()
            .clang_arg(format!(
                "-I{}",
                symcc_src_path.join("runtime").to_str().unwrap()
            ))
            .clang_arg(format!(
                "-I{}",
                symcc_src_path
                    .join("runtime")
                    .join("rust_backend")
                    .to_str()
                    .unwrap()
            ))
            .clang_args(["-x", "c++", "-std=c++17"].iter())
            .header(
                symcc_src_path
                    .join("runtime")
                    .join("rust_backend")
                    .join("Runtime.h")
                    .to_str()
                    .unwrap(),
            )
            .header(
                symcc_src_path
                    .join("runtime")
                    .join("LibcWrappers.cpp")
                    .to_str()
                    .unwrap(),
            )
            .allowlist_type("SymExpr")
            .allowlist_function("(_sym_.*)|(.*_symbolized)")
            .opaque_type("_.*")
            .size_t_is_usize(true)
            .generate()
            .expect("Unable to generate bindings");

        write_symcc_runtime_bindings_file(&out_path, &cpp_bindings);
        write_cpp_function_export_macro(&out_path, &cpp_bindings);

        if std::env::var("CARGO_FEATURE_NO_CPP_RUNTIME").is_err()
            && std::env::var("DOCS_RS").is_err()
        {
            let rename_header_path = out_path.join("rename.h");
            write_symcc_rename_header(&rename_header_path, &cpp_bindings);
            build_and_link_symcc_runtime(&symcc_src_path, &rename_header_path);
        }
    } else {
        println!("cargo:warning=Building SymCC is only supported on Linux");
    }
}

fn write_cpp_function_export_macro(out_path: &Path, cpp_bindings: &bindgen::Bindings) {
    let mut macro_file = File::create(out_path.join("cpp_exports_macro.rs")).unwrap();
    writeln!(
        macro_file,
        "#[doc(hidden)]
        #[macro_export]
        macro_rules! export_cpp_runtime_functions {{
            () => {{",
    )
    .unwrap();
    EXPORTED_FUNCTION_REGEX.with(|x| {
        x.borrow()
            .captures_iter(&cpp_bindings.to_string())
            .for_each(|captures| {
                writeln!(
                    macro_file,
                    "    symcc_runtime::export_c_symbol!({});",
                    &captures[1]
                )
                .unwrap();
            });
    });
    writeln!(
        macro_file,
        " }};
        }}",
    )
    .unwrap();
}

fn checkout_symcc(intree_symcc_path: &Path, out_path: &Path) -> PathBuf {
    let repo_dir = out_path.join("libafl_symcc_src");
    if repo_dir.exists() {
        // delete the old repo
        std::fs::remove_dir_all(&repo_dir).unwrap();
    }
    clone_symcc(intree_symcc_path, &repo_dir);
    repo_dir
}

fn write_rust_runtime_macro_file(out_path: &Path, symcc_src_path: &Path) {
    let rust_bindings = bindgen::Builder::default()
        .clang_arg(format!(
            "-I{}",
            symcc_src_path.join("runtime").to_str().unwrap()
        ))
        .clang_arg(format!(
            "-I{}",
            symcc_src_path
                .join("runtime")
                .join("rust_backend")
                .to_str()
                .unwrap()
        ))
        .clang_args(["-x", "c++", "-std=c++17"].iter())
        .header(
            symcc_src_path
                .join("runtime")
                .join("rust_backend")
                .join("RustRuntime.h")
                .to_str()
                .unwrap(),
        )
        .allowlist_type("RSymExpr")
        .allowlist_function("_rsym_.*")
        .opaque_type("_.*")
        .size_t_is_usize(true)
        .generate()
        .expect("Unable to generate bindings");
    let mut rust_runtime_macro = File::create(out_path.join("rust_exports_macro.rs")).unwrap();
    writeln!(
        rust_runtime_macro,
        "#[doc(hidden)]
        #[macro_export]
        macro_rules! invoke_macro_with_rust_runtime_exports {{
            ($macro:path; $($extra_ident:path),*) => {{",
    )
    .unwrap();
    EXPORTED_FUNCTION_REGEX.with(|x| {
        x.borrow()
            .captures_iter(&rust_bindings.to_string())
            .for_each(|captures| {
                writeln!(
                    rust_runtime_macro,
                    "    $macro!({},{}; $($extra_ident),*);",
                    &captures[1].replace("_rsym_", ""),
                    &FUNCTION_NAME_REGEX
                        .with(|x| x.borrow().captures(&captures[1]))
                        .unwrap()[1]
                )
                .unwrap();
            });
    });
    writeln!(
        rust_runtime_macro,
        " }};
        }}",
    )
    .unwrap();
}

fn write_symcc_runtime_bindings_file(out_path: &Path, cpp_bindings: &bindgen::Bindings) {
    let mut bindings_file = File::create(out_path.join("bindings.rs")).unwrap();
    cpp_bindings.to_string().lines().for_each(|l| {
        if let Some(captures) = FUNCTION_NAME_REGEX.with(|x| x.borrow().captures(l)) {
            let function_name = &captures[1];
            writeln!(
                bindings_file,
                "#[link_name=\"{SYMCC_RUNTIME_FUNCTION_NAME_PREFIX}{function_name}\"]"
            )
            .unwrap();
        }
        writeln!(bindings_file, "{l}").unwrap();
    });
}

fn write_symcc_rename_header(rename_header_path: &Path, cpp_bindings: &bindgen::Bindings) {
    let mut rename_header_file = File::create(rename_header_path).unwrap();
    writeln!(
        rename_header_file,
        "#ifndef PREFIX_EXPORTS_H
        #define PREFIX_EXPORTS_H",
    )
    .unwrap();

    cpp_bindings
        .to_string()
        .lines()
        .filter_map(|l| FUNCTION_NAME_REGEX.with(|x| x.borrow().captures(l)))
        .map(|captures| captures[1].to_string())
        .for_each(|val| {
            writeln!(
                rename_header_file,
                "#define {} {SYMCC_RUNTIME_FUNCTION_NAME_PREFIX}{}",
                &val, &val
            )
            .unwrap();
        });

    writeln!(rename_header_file, "#endif").unwrap();
}

fn build_and_link_symcc_runtime(symcc_src_path: &Path, rename_header_path: &Path) {
    build_dep_check(&["cmake"]);
    let cpp_lib = cmake::Config::new(symcc_src_path.join("runtime"))
        .define("RUST_BACKEND", "ON")
        // 2022: Deprecations break -Werror for our symcc build...
        // We want to build it anyway!
        .define("CMAKE_CXX_COMPILER", "clang++")
        .define("CMAKE_C_COMPILER", "clang")
        .define("Z3_TRUST_SYSTEM_VERSION", "on")
        .define("CMAKE_EXPORT_COMPILE_COMMANDS", "on")
        .cxxflag("-Wno-error=deprecated-declarations")
        .cxxflag(format!(
            "-include \"{}\"",
            rename_header_path.to_str().unwrap()
        ))
        .cxxflag("-stdlib=libc++")
        .build()
        .join("lib");
    link_with_cpp_stdlib();
    println!("cargo:rustc-link-search=native={}", cpp_lib.display());
    println!("cargo:rustc-link-lib=static=SymRuntime");
}

fn link_with_cpp_stdlib() {
    let target = env::var("TARGET").unwrap();
    if target.contains("apple") {
        println!("cargo:rustc-link-lib=dylib=c++");
    } else if target.contains("linux") {
        println!("cargo:rustc-link-lib=dylib=lc++");
    } else {
        unimplemented!();
    }
}

fn build_dep_check(tools: &[&str]) {
    for tool in tools {
        println!("Checking for build tool {tool}...");

        if let Ok(path) = which::which(tool) {
            println!("Found build tool {}", path.to_str().unwrap());
        } else {
            println!("ERROR: missing build tool {tool}");
            exit(1);
        };
    }
}
