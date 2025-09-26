fn is_lib_fuzzing_engine(maybe_lib: &str) -> bool {
    return maybe_lib == "/usr/lib/LibFuzzingEngine.a"
        || maybe_lib == "/usr/lib/libFuzzingEngine.a";
}

fn remove_fuzzer_sanitizer(arg: &mut String) -> bool {
    let sanitizers_string = arg.split("-fsanitize=").collect::<Vec<&str>>()[1];
    let sanitizers = sanitizers_string.split(",").collect::<Vec<&str>>();
    if sanitizers.len() == 1 {
        if sanitizers[0] == "fuzzer" {
            *arg = "".to_string();
            return true;
        } 
        else if sanitizers[0] == "fuzzer-no-link" {
            *arg = "".to_string();
            return false;
        }
        else {
            return false;
        }
    } else {
        // There is no case where *arg becomes "-fsanitize=" because it is already handled in the
        // previous case
        let new_sanitizers = sanitizers
            .iter()
            .filter(|s| **s != "fuzzer" && **s != "fuzzer-no-link")
            .map(|s| *s)
            .collect::<Vec<&str>>();
        let has_fuzzer = sanitizers.iter().any(|s| **s == *"fuzzer");
        *arg = format!("-fsanitize={}", new_sanitizers.join(","));
        return has_fuzzer;
    }
}

/// Remove fuzzer related arguments from the list of arguments
/// If remove_sanitizers is true, then it will remove all sanitizer arguments
/// Otherwise, it would only remove the fuzzer related arguments, including -fsanitize=fuzzer
pub fn remove_fuzzer_args(args: &mut Vec<String>, remove_sanitizers: bool) -> bool {
    let mut linked_to_libfuzzer = false;

    // Find pattern `-I /usr/lib/libFuzzingEngine.a` or
    // `-lFuzzingEngine` inside `args` and remove it
    let mut i = 0;
    while i < args.len() {
        if args[i] == "-I" && i + 1 < args.len() && is_lib_fuzzing_engine(&args[i + 1]) {
            linked_to_libfuzzer = true;
            args.remove(i); // Remove "-I"
            args.remove(i); // Remove the next element (LibFuzzingEngine.a or libFuzzingEngine.a)
            break;
        } else if is_lib_fuzzing_engine(&args[i]) {
            linked_to_libfuzzer = true;
            args.remove(i);
            break;
        } else if args[i] == "-lFuzzingEngine" {
            linked_to_libfuzzer = true;
            args.remove(i);
            break;
        } else {
            i += 1;
        }
    }

    // Now remove sanitizer arguments
    for arg in args.iter_mut() {
        if arg.contains("-fsanitize=") {
            if remove_sanitizers {
                linked_to_libfuzzer |= arg.contains("fuzzer");
                *arg = "".to_string();
            } else {
                linked_to_libfuzzer |= remove_fuzzer_sanitizer(arg);
            }
        }
    }
    args.retain(|arg| !arg.is_empty());
    return linked_to_libfuzzer;
}

#[cfg(test)]
mod tests {

    fn arg_removal_test(
        cmd_before: &str,
        cmd_after: &str,
        remove_sanitizers: bool,
        linked_to_libfuzzer: bool,
    ) {
        let mut args_before = cmd_before
            .split(" ")
            .map(|s| s.to_string())
            .collect::<Vec<String>>();
        let actual_linked_to_libfuzzer =
            super::remove_fuzzer_args(&mut args_before, remove_sanitizers);
        assert_eq!(
            args_before,
            cmd_after
                .split(" ")
                .map(|s| s.to_string())
                .collect::<Vec<String>>(),
        );
        assert_eq!(
            actual_linked_to_libfuzzer, linked_to_libfuzzer,
            "linked_to_libfuzzer({}) != {}",
            cmd_before, linked_to_libfuzzer
        );
    }

    #[test]
    fn test_remove_fuzzer_args() {
        arg_removal_test(
            "clang -fsanitize=fuzzer -fsanitize=address -I /usr/lib/libFuzzingEngine.a -fsanitize=undefined -o fuzzer fuzzer.cc",
            "clang -fsanitize=address -fsanitize=undefined -o fuzzer fuzzer.cc",
            false,
            true);
        arg_removal_test(
            "clang -fsanitize=fuzzer -fsanitize=address -I /usr/lib/libFuzzingEngine.a -fsanitize=undefined -o fuzzer fuzzer.cc",
            "clang -o fuzzer fuzzer.cc",
            true,
            true);
        arg_removal_test(
            "clang -fsanitize=fuzzer -fsanitize=address -lFuzzingEngine -fsanitize=undefined -o fuzzer fuzzer.cc",
            "clang -o fuzzer fuzzer.cc",
            true,
            true);
        arg_removal_test(
            "clang -fsanitize=address,undefined,fuzzer -o fuzzer fuzzer.cc",
            "clang -fsanitize=address,undefined -o fuzzer fuzzer.cc",
            false,
            true,
        );
        arg_removal_test(
            "clang -fsanitize=address,undefined,fuzzer -o fuzzer fuzzer.cc",
            "clang -o fuzzer fuzzer.cc",
            true,
            true,
        );
        arg_removal_test(
            "clang -fsanitize=address,undefined -o fuzzer fuzzer.cc",
            "clang -fsanitize=address,undefined -o fuzzer fuzzer.cc",
            false,
            false,
        );
        arg_removal_test(
            "clang -fsanitize=address,undefined -o fuzzer fuzzer.cc",
            "clang -o fuzzer fuzzer.cc",
            true,
            false,
        );
        arg_removal_test(
            "clang -fsanitize=fuzzer -o fuzzer fuzzer.cc",
            "clang -o fuzzer fuzzer.cc",
            true,
            true,
        );
        arg_removal_test(
            "clang -fsanitize=fuzzer -o fuzzer fuzzer.cc",
            "clang -o fuzzer fuzzer.cc",
            false,
            true,
        );
        arg_removal_test(
            "clang -fsanitize=fuzzer-no-link fuzzer.cc",
            "clang fuzzer.cc",
            false,
            false,
        );
    }
}
