extern crate alloc;

use core::time::Duration;
use log::{debug, error, info, warn};
use std::{
    boxed::Box,
    env,
    path::PathBuf,
};

use alloc::borrow::Cow;
use libafl::{
    corpus::{Corpus, InMemoryOnDiskCorpus, OnDiskCorpus},
    events::{EventConfig, Launcher, LlmpRestartingEventManager},
    executors::{inprocess, ExitKind, InProcessExecutor, ShadowExecutor},
    feedback_and_fast, feedback_or, feedback_or_fast,
    feedbacks::{CrashFeedback, MaxMapFeedback, NewHashFeedback, TimeFeedback, TimeoutFeedback},
    fuzzer::{Fuzzer, StdFuzzer},
    generators::RandBytesGenerator,
    inputs::{BytesInput, HasTargetBytes},
    monitors::{NopMonitor, OnDiskJsonMonitor},
    mutators::{
        havoc_mutations, tokens_mutations, I2SRandReplace, StdMOptMutator, StdScheduledMutator,
        Tokens,
    },
    observers::{BacktraceObserver, CanTrack, TimeObserver},
    schedulers::{
        powersched::PowerSchedule, IndexesLenTimeMinimizerScheduler, StdWeightedScheduler,
    },
    stages::{
        calibrate::CalibrationStage, mutational::MultiMutationalStage,
        power::StdPowerMutationalStage, ShadowTracingStage, StdMutationalStage,
    },
    state::{HasCorpus, StdState},
    Error, HasMetadata,
};
use libafl_bolts::{
    core_affinity::{CoreId, Cores},
    rands::StdRand,
    shmem::{ShMemProvider, StdShMemProvider},
    tuples::{tuple_list, Merge},
    AsSlice,
};
#[cfg(any(target_os = "linux", target_vendor = "apple"))]
use libafl_targets::autotokens;
use libafl_targets::{std_edges_map_observer, CmpLogObserver, libfuzzer_initialize, libfuzzer_test_one_input};
use mimalloc::MiMalloc;
use rdkafka::ClientConfig;
use serde::{Deserialize, Serialize};

use crate::{
    kafka_consumer_mutator::KafkaConsumerMutator, kafka_producer_feedback::KafkaProducerFeedback,
    truncate_mutator::TruncateMutator,
    zmq_consumer_mutator::ZmqConsumerMutator,
    pseudofuzzer::{LLVMFuzzerTestOneInput, pseudo_main},
};

// for Kafka
mod kafka_consumer_mutator;
mod kafka_producer_feedback;
mod truncate_mutator;
mod protobuf;
mod pseudofuzzer;
mod threaded_consumer;
mod util;
// for ZeroMQ
mod zmq_consumer_mutator;
mod zmq_dealer;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

#[derive(Serialize, Deserialize)]
struct Config {
    /// Centralized broker is responsible for forwarding LLMP messages to all clients
    centralized_broker_port: u16,

    /// For inprocess, this broker is responsible for all of the above and also restarting the
    /// fuzzer
    broker_port: u16,

    /// Harness id of the current fuzzing campaign
    harness_id: String,

    /// Path to the initial corpus directory
    initial_corpus_dir: Option<String>,

    /// Path to corpus directory
    corpus_dir: String,

    /// Output directory to store crashes
    output_dir: String,

    /// Address of the Kafka broker
    /// e.g. "localhost:9092"
    kafka_broker_addr: Option<String>,

    /// Name of Kafka topic on which to listen for new seeds to add to the corpus
    kafka_seed_additions_topic: Option<String>,

    /// Name of Kafka topic on which to listen for requests for corpus seeds
    kafka_seed_requests_topic: Option<String>,

    /// Name of Kafka topic on which to send corpus seeds when requested
    kafka_seed_updates_topic: Option<String>,

    /// Log monitor output to a file
    log_file: Option<String>,

    /// Maximum length of seed
    max_len: Option<usize>,

    /// Maximum length of seed
    timeout: Option<u64>,

    /// List of dictionary files, also known as tokens
    dictionary_files: Vec<String>,

    /// Number of cores to use
    cores: Vec<usize>,
}

type FuzzerState<C, SC> = StdState<BytesInput, C, StdRand, SC>;
type CorpusType = InMemoryOnDiskCorpus<BytesInput>;
type CrashesType = OnDiskCorpus<BytesInput>;
type LibAFLFuzzState = FuzzerState<CorpusType, CrashesType>;

type LibAFLNoForkFuzzManager = LlmpRestartingEventManager<(), LibAFLFuzzState, StdShMemProvider>;

fn run_client_inprocess(
    state: Option<LibAFLFuzzState>,
    mut mgr: LibAFLNoForkFuzzManager,
    config: &Config,
    core_id: CoreId,
) -> Result<(), libafl::Error> {
    let kafka_group_id = format!("libafl_{}_{}", config.harness_id, core_id.0);
    let campaign_id = "main";
    let timeout = Duration::from_secs(config.timeout.unwrap_or(50));

    let edges_observer = unsafe { std_edges_map_observer("edges").track_indices() };
    let time_observer = TimeObserver::new("time");
    let cmplog_observer = CmpLogObserver::new("cmplog", true);

    let map_feedback = MaxMapFeedback::new(&edges_observer);
    let calibration = CalibrationStage::new(&map_feedback);

    let mut feedback = feedback_or!(
        // New maximization map feedback linked to the edges observer and the feedback state
        map_feedback,
        // Time feedback, this one does not need a feedback state
        TimeFeedback::new(&time_observer)
    );

    // A feedback to choose if an input is a solution or not
    let backtrace_observer = BacktraceObserver::owned(
        "BacktraceObserver",
        libafl::observers::HarnessType::InProcess,
    );
    let objective = feedback_or_fast!(
        feedback_and_fast!(
            CrashFeedback::new(),
            NewHashFeedback::new(&backtrace_observer)
        ),
        TimeoutFeedback::new()
    );

    let mut objective = if config.kafka_broker_addr.is_none()
        || config.kafka_seed_requests_topic.is_none()
        || config.kafka_seed_updates_topic.is_none()
    {
        // Dummy feedback wrapper that doesn't add anything (just to
        // avoid type errors)
        KafkaProducerFeedback::new(
            objective,
            &campaign_id,
            &config.harness_id,
            None,
            None,
            None,
            None,
        )?
    } else {
        // these are all safe to unwrap() thanks to the `if` we're in
        let broker_addr = config.kafka_broker_addr.as_ref().unwrap();
        let requests_topic = config.kafka_seed_requests_topic.as_ref().unwrap();
        let updates_topic = config.kafka_seed_updates_topic.as_ref().unwrap();

        let mut kafka_consumer_config = ClientConfig::new();
        kafka_consumer_config.set("group.id", &kafka_group_id);
        kafka_consumer_config.set("bootstrap.servers", broker_addr);
        kafka_consumer_config.set("enable.partition.eof", "false");
        kafka_consumer_config.set("session.timeout.ms", "6000");
        kafka_consumer_config.set("enable.auto.commit", "true");
        // kafka_consumer_config.set_log_level(RDKafkaLogLevel::Debug);

        let mut kafka_producer_config = ClientConfig::new();
        kafka_producer_config.set("bootstrap.servers", broker_addr);
        kafka_producer_config.set("message.timeout.ms", "5000");
        // kafka_producer_config.set("compression.type", "lz4");  // enable in broker instead

        KafkaProducerFeedback::new(
            objective,
            &campaign_id,
            &config.harness_id,
            Some(&kafka_consumer_config),
            Some(requests_topic),
            Some(&kafka_producer_config),
            Some(updates_topic),
        )?
    };

    let std_rand = StdRand::new();
    let crashes = OnDiskCorpus::new(&config.output_dir)?;

    let corpus = InMemoryOnDiskCorpus::new(&config.corpus_dir)?;
    let mut state = state.unwrap_or_else(|| {
        StdState::new(std_rand, corpus, crashes, &mut feedback, &mut objective)
            // Error handling becomes too difficult to propagate to parent function
            .expect("Failed to create state")
    });

    // A minimization+queue policy to get testcasess from the corpus
    let scheduler = IndexesLenTimeMinimizerScheduler::new(
        &edges_observer,
        StdWeightedScheduler::with_schedule(&mut state, &edges_observer, Some(PowerSchedule::FAST)),
    );

    // A fuzzer with feedbacks and a corpus scheduler
    let mut fuzzer = StdFuzzer::new(scheduler, feedback, objective);

    // The wrapped harness function, calling out to the LLVM-style harness
    let mut harness = |bytes_input: &BytesInput| {
        let target = bytes_input.target_bytes();
        let input = target.as_slice();
        unsafe {
            LLVMFuzzerTestOneInput(input.as_ptr(), input.len());
        }
        ExitKind::Ok
    };

    let mut executor = ShadowExecutor::new(
        InProcessExecutor::with_timeout(
            &mut harness,
            tuple_list!(edges_observer, time_observer, backtrace_observer),
            &mut fuzzer,
            &mut state,
            &mut mgr,
            timeout,
        )?,
        tuple_list!(cmplog_observer),
    );

    // Call LLVMFUzzerInitialize() if present.
    let args: Vec<String> = env::args().collect();
    if unsafe { libfuzzer_initialize(&args) } == -1 {
        println!("Warning: LLVMFuzzerInitialize failed with -1");
    }

    if state.must_load_initial_inputs() {
        if config.initial_corpus_dir.is_none() {
            info!("No initial corpus directory specified, generating 8 initial inputs");
            let mut generator = RandBytesGenerator::new(32);
            // Generate 8 initial inputs
            state
                .generate_initial_inputs(&mut fuzzer, &mut executor, &mut generator, &mut mgr, 8)
                .expect("Failed to generate the initial corpus");
            info!("Generated {} initial inputs", state.corpus().count());
        } else {
            let initial_corpus_path = PathBuf::from(&config.initial_corpus_dir.as_ref().unwrap());
            if !initial_corpus_path.exists() {
                return Err(libafl::Error::invalid_corpus(format!(
                    "Invalid initial corpus dir: {}",
                    initial_corpus_path.to_str().unwrap(),
                )));
            }
            let initial_inputs_vec = vec![initial_corpus_path];
            state.load_initial_inputs(&mut fuzzer, &mut executor, &mut mgr, &initial_inputs_vec)?;
            info!("Loaded {} initial inputs from disk", state.corpus().count());
        }
    } else {
        info!("No initial inputs loaded, starting from saved state");
    }

    if state.metadata_map().get::<Tokens>().is_none() {
        let mut tokens = Tokens::default();

        if !config.dictionary_files.is_empty() {
            tokens = tokens.add_from_files_ignore_errors(&config.dictionary_files);
        }

        #[cfg(any(target_os = "linux", target_vendor = "apple"))]
        {
            tokens += autotokens()?;
        }

        if !tokens.is_empty() {
            state.add_metadata(tokens);
            info!("Loaded tokens from {:?}", &config.dictionary_files);
        }
    }

    let tracing = ShadowTracingStage::new(&mut executor);
    let i2s = StdMutationalStage::new(StdScheduledMutator::new(tuple_list!(I2SRandReplace::new())));

    let mutator = StdMOptMutator::new(
        &mut state,
        havoc_mutations().merge(tokens_mutations()),
        7,
        5,
    )?;
    let power = StdPowerMutationalStage::new(mutator);

    let zmq_mutator = ZmqConsumerMutator::new(&config.harness_id)?;
    let zmq_mutational_stage = MultiMutationalStage::new(zmq_mutator);

    let truncate_mutator = TruncateMutator::new(config.max_len);
    let truncate_mutational_stage = StdMutationalStage::new(truncate_mutator);

    // this is a bit gross without if-let chains
    if config.kafka_broker_addr.is_none() || config.kafka_seed_additions_topic.is_none() {
        let mut stages = tuple_list!(zmq_mutational_stage, truncate_mutational_stage, calibration, tracing, i2s, power);
        fuzzer.fuzz_loop(&mut stages, &mut executor, &mut state, &mut mgr)?;
    } else {
        let broker_addr = config.kafka_broker_addr.as_ref().unwrap().clone();
        let topic = config.kafka_seed_additions_topic.as_ref().unwrap().clone();

        let mut kafka_consumer_config = ClientConfig::new();
        kafka_consumer_config.set("group.id", &kafka_group_id);
        kafka_consumer_config.set("bootstrap.servers", &broker_addr);
        kafka_consumer_config.set("enable.partition.eof", "false");
        kafka_consumer_config.set("session.timeout.ms", "6000");
        kafka_consumer_config.set("enable.auto.commit", "true");
        // kafka_consumer_config.set_log_level(RDKafkaLogLevel::Debug);

        let kafka_mutator = KafkaConsumerMutator::new(
            &kafka_consumer_config,
            Cow::from(topic),
            &config.harness_id,
            "kafka_mutator",
        )?;
        let kafka_mutational_stage = MultiMutationalStage::new(kafka_mutator);

        let mut stages = tuple_list!(
            kafka_mutational_stage,
            zmq_mutational_stage,
            truncate_mutational_stage,
            calibration,
            tracing,
            i2s,
            power
        );
        fuzzer.fuzz_loop(&mut stages, &mut executor, &mut state, &mut mgr)?;
    }

    Ok(())
}

fn execute_fuzzer(config: &Config) -> anyhow::Result<()> {
    let shmem_provider = StdShMemProvider::new()?;
    let monitor = OnDiskJsonMonitor::new(
        config.log_file.clone().unwrap_or("/dev/null".to_string()),
        NopMonitor::new(),
        |_s| true,
    );
    let main_client_closure =
        |state: Option<_>, mgr, core_id: CoreId| run_client_inprocess(state, mgr, config, core_id);
    let res = Launcher::builder()
        .shmem_provider(shmem_provider)
        .configuration(EventConfig::from_name("default"))
        .monitor(monitor)
        .run_client(main_client_closure)
        .cores(&Cores {
            cmdline: String::new(),
            ids: config.cores.iter().map(|c| CoreId(*c)).collect(),
        })
        .broker_port(config.broker_port)
        .remote_broker_addr(None)
        .build()
        .launch();

    match res {
        Ok(()) | Err(Error::ShuttingDown) => Ok(()),
        Err(err) => panic!("Failed to set up the launcher: {err}"),
    }
}

#[no_mangle]
pub extern "C" fn cleanup() {
    std::process::abort();
}

fn register_cleanup() {
    extern "C" {
        fn atexit(func: extern "C" fn()) -> i32;
    }

    unsafe {
        atexit(cleanup);
    }
}

#[no_mangle]
pub extern "C" fn exit(_code: i32) -> ! {
    // Replace all exit() calls with abort
    std::process::abort();
}

fn exit_on_panic() {
    let orig_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        // invoke the default handler and exit the process
        orig_hook(panic_info);
        std::process::exit(1);
    }));
}

#[no_mangle]
pub extern "C" fn libafl_main(argc: i32, argv: *const *const u8) -> i32 {
    let config_path_result = std::env::var("FUZZER_CONFIG_PATH");
    if config_path_result.is_err() {
        // pseudo fuzzer single input
        return pseudo_main(argc, argv);
    }
    let config_path = config_path_result.unwrap();
    let config_str = std::fs::read_to_string(config_path).expect("Failed to read config file");
    let config = serde_json::from_str(&config_str).expect("Failed to parse config file");
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn")).init();

    register_cleanup();
    exit_on_panic();

    execute_fuzzer(&config).expect("Failed to execute fuzzer");

    0
}
