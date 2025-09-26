use std::borrow::Cow;
use std::cmp::min;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::sync::Mutex;
use std::thread::{self, JoinHandle};

use crossbeam_channel::{bounded, Receiver};
use log::{debug, error, info, warn};

use crate::zmq_dealer::{calculate_sha256_hex, CliArgs, Dealer};
use libafl::{
    inputs::{HasMutatorBytes, Input, UsesInput},
    mutators::MultiMutator,
    state::{HasMaxSize, HasRand},
    HasMetadata,
};
use libafl_bolts::Named;

const SPSC_CHANNEL_SIZE: usize = 4096;

/// A mutator that, rather than mutating anything, asynchronously reads
/// new seeds from a ZMQ dealer based channel and adds them to the corpus.
pub struct ZmqConsumerMutator {
    seed_receiver: Receiver<Vec<u8>>,
    shutdown_flag: Arc<AtomicBool>,
    zmq_event_loop_thread: Option<JoinHandle<()>>,
}

impl ZmqConsumerMutator {
    pub fn new(harness_name: &str) -> Result<Self, libafl::Error> {
        // Hardcoded values for router, queue_size, and heartbeat_interval_secs
        // These will be used to construct CliArgs for the Dealer.
        // CliArgs in zmq_dealer.rs now has defaults, but we can override them here.
        let router_url: String = "ipc:///tmp/ipc/haha".to_string();
        let queue_size_val: usize = SPSC_CHANNEL_SIZE;
        let heartbeat_interval_secs: u64 = 10;

        let cli_args = CliArgs {
            router: router_url,
            harness: harness_name.to_string(),
            heartbeat: heartbeat_interval_secs,
            queue_size: queue_size_val,
        };

        let shutdown_flag = Arc::new(AtomicBool::new(false));
        let (seed_sender, seed_receiver) = bounded::<Vec<u8>>(cli_args.queue_size);
        let shm_consumers = Arc::new(Mutex::new(HashMap::new()));

        info!(
            "ZMQ Consumer Mutator: Initializing ZMQ dealer with harness: {}",
            harness_name
        );

        let dealer = Dealer::new(
            cli_args,
            seed_sender,
            shm_consumers,
            Arc::clone(&shutdown_flag),
        );

        // The dealer.run_zmq_event_loop() consumes dealer and returns a JoinHandle directly.
        let zmq_event_loop_thread_handle = dealer.run_zmq_event_loop();

        info!("ZMQ Consumer Mutator: ZMQ dealer event loop initiated.");

        Ok(Self {
            seed_receiver,
            shutdown_flag,
            zmq_event_loop_thread: Some(zmq_event_loop_thread_handle),
        })
    }
}

impl Named for ZmqConsumerMutator {
    fn name(&self) -> &Cow<'static, str> {
        &Cow::Borrowed("zmq_consumer")
    }
}

impl<I, S> MultiMutator<I, S> for ZmqConsumerMutator
where
    S: UsesInput + HasMetadata + HasRand + HasMaxSize,
    I: From<Vec<u8>> + Input,
{
    fn multi_mutate(
        &mut self,
        _state: &mut S,
        input: &I,
        _max_count: Option<usize>,
    ) -> Result<Vec<I>, libafl::Error> {
        let new_inputs: Vec<_> = self
            .seed_receiver
            .try_iter()
            .filter_map(|seed| {
                if seed.is_empty() {
                    // debug!(">>> MAIN CHECK: EMPTY SEED");
                    None
                } else {
                    // let sha256_hex = calculate_sha256_hex(&seed);
                    // let seed_string = String::from_utf8_lossy(&seed);
                    // info!(">>> MAIN CHECK: {} (decoded: {:?})",  sha256_hex, seed_string);
                    Some(I::from(seed))
                }
            })
            .collect();

        Ok(new_inputs)
    }
}

impl Drop for ZmqConsumerMutator {
    fn drop(&mut self) {
        info!("ZmqConsumerMutator: Shutting down ZMQ dealer event loop thread...");
        self.shutdown_flag.store(true, Ordering::Release);

        // TODO: join the zmq event loop thread
        info!("ZmqConsumerMutator: Shutdown complete.");
    }
}
