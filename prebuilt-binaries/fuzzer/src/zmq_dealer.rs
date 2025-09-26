use byteorder::{ByteOrder, LittleEndian};
use chrono::Duration;
use clap::Parser;
use crossbeam_channel::{bounded, Sender};
use log::{debug, error, info, warn};
use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::convert::TryFrom;
use std::fs::File;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time;

// Adjusted imports for zeromq
use tokio::time::{interval, timeout as tokio_timeout, MissedTickBehavior};
use zeromq::util::PeerIdentity;
use zeromq::SocketRecv;
use zeromq::SocketSend;
use zeromq::{self, DealerSocket, Socket, SocketOptions, ZmqError, ZmqMessage};

const SHARED_MEMORY_BASE_PATH: &str = "/dev/shm";
const HEADER_SIZE: usize = 8; // sizeof(uint32_t) * 2 for item_size and item_num
const LEN_FIELD_SIZE: usize = 4; // sizeof(uint32_t) for data_len

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("ZMQ error: {0}")]
    Zmq(#[from] ZmqError),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("UTF8 error: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
    #[error("Channel send error: {0}")]
    SendError(String),
    #[error("Shared memory error: {0}")]
    ShmError(String),
    #[error("Signal setup error: {0}")]
    SignalError(String),
    #[error("Tokio timeout error")]
    TokioTimeout,
}

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Parser, Debug, Clone)]
#[clap(author, version, about, long_about = None)]
pub struct CliArgs {
    #[clap(long, default_value = "ipc:///tmp/ipc/haha")]
    pub router: String,
    #[clap(long, default_value = "RustHarness")]
    pub harness: String,
    #[clap(long, default_value_t = 5)]
    pub heartbeat: u64,
    #[clap(long, default_value_t = 10000)]
    pub queue_size: usize,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct SubmitBundle {
    pub script_id: i32,
    pub harness_name: String,
    pub shm_name: String,
    pub seed_ids: Vec<i32>,
}

#[derive(Debug)]
pub struct SeedShmemPoolConsumer {
    mmap: memmap2::Mmap,
    item_size: u32,
    item_num: u32,
}

impl SeedShmemPoolConsumer {
    pub fn new(shm_name: &str) -> Result<Self> {
        let shm_path_str = format!("{}/{}", SHARED_MEMORY_BASE_PATH, shm_name);
        let shm_path = PathBuf::from(&shm_path_str);

        info!(
            "Opening shared memory file for mapping: {}",
            shm_path.display()
        );

        let file = File::open(&shm_path).map_err(|e| {
            Error::ShmError(format!(
                "Failed to open shared memory file {}: {}",
                shm_path.display(),
                e
            ))
        })?;

        let mmap = unsafe { memmap2::MmapOptions::new().map(&file) }.map_err(|e| {
            Error::ShmError(format!(
                "Failed to map shared memory file {}: {}",
                shm_path.display(),
                e
            ))
        })?;

        let file_size = mmap.len();
        if file_size < HEADER_SIZE {
            return Err(Error::ShmError(format!(
                "Shared memory file {} is too small: {} bytes, expected at least {} for header",
                shm_path.display(),
                file_size,
                HEADER_SIZE
            )));
        }

        let item_size = LittleEndian::read_u32(&mmap[0..4]);
        let item_num = LittleEndian::read_u32(&mmap[4..8]);

        let expected_size = HEADER_SIZE + (item_size as usize * item_num as usize);
        if expected_size != file_size {
            return Err(Error::ShmError(format!(
                "Shared memory size mismatch for {}: expected={}, actual={}. Header: item_size={}, item_num={}",
                shm_path.display(),
                expected_size,
                file_size,
                item_size,
                item_num
            )));
        }

        info!(
            "Successfully mapped shared memory: {} ({} items of size {}, total size {})",
            shm_name, item_num, item_size, file_size
        );

        Ok(Self {
            mmap,
            item_size,
            item_num,
        })
    }

    fn item_offset(&self, idx: i32) -> Result<usize> {
        if idx < 0 || idx >= self.item_num as i32 {
            return Err(Error::ShmError(format!(
                "Seed index {} out of range (0-{})",
                idx,
                self.item_num - 1
            )));
        }
        Ok(HEADER_SIZE + (idx as usize * self.item_size as usize))
    }

    pub fn get_seed_content(&self, seed_id: i32) -> Result<Vec<u8>> {
        if seed_id < 0 || seed_id >= self.item_num as i32 {
            warn!(
                "Invalid seed_id {} requested for shm with {} items",
                seed_id, self.item_num
            );
            return Ok(Vec::new());
        }

        let offset = self.item_offset(seed_id)?;

        if offset + LEN_FIELD_SIZE > self.mmap.len() {
            return Err(Error::ShmError(format!(
                "Offset for seed_id {} data length (offset {}) is out of bounds (total size {})",
                seed_id,
                offset,
                self.mmap.len()
            )));
        }

        let data_len_slice = &self.mmap[offset..(offset + LEN_FIELD_SIZE)];
        let data_len = LittleEndian::read_u32(data_len_slice);

        if data_len == 0 {
            warn!("Seed ID {} has zero length data", seed_id);
            return Ok(Vec::new());
        }

        if data_len > self.item_size - LEN_FIELD_SIZE as u32 {
            return Err(Error::ShmError(format!(
                "Seed ID {} data_len {} exceeds item_size {} (max content {}). Item offset: {}",
                seed_id,
                data_len,
                self.item_size,
                self.item_size - LEN_FIELD_SIZE as u32,
                offset
            )));
        }

        let payload_offset = offset + LEN_FIELD_SIZE;
        let payload_end = payload_offset + data_len as usize;

        if payload_end > self.mmap.len() {
            return Err(Error::ShmError(format!(
                "Payload for seed_id {} (offset {}, len {}) is out of bounds (total size {})",
                seed_id,
                payload_offset,
                data_len,
                self.mmap.len()
            )));
        }

        let payload_slice = &self.mmap[payload_offset..payload_end];
        Ok(payload_slice.to_vec())
    }
}

pub struct Dealer {
    args: CliArgs,
    seed_sender: Sender<Vec<u8>>,
    shm_consumers: Arc<Mutex<HashMap<String, Arc<SeedShmemPoolConsumer>>>>,
    stop_flag: Arc<AtomicBool>,
}

impl Dealer {
    pub fn new(
        args: CliArgs,
        seed_sender: Sender<Vec<u8>>,
        shm_consumers: Arc<Mutex<HashMap<String, Arc<SeedShmemPoolConsumer>>>>,
        stop_flag: Arc<AtomicBool>,
    ) -> Self {
        Self {
            args,
            seed_sender,
            shm_consumers,
            stop_flag,
        }
    }

    pub fn run_zmq_event_loop(self) -> thread::JoinHandle<()> {
        let args = self.args.clone();
        let seed_sender = self.seed_sender;
        let shm_consumers = Arc::clone(&self.shm_consumers);
        let stop_flag = Arc::clone(&self.stop_flag);

        info!("Spawning ZMQ event loop thread...");
        thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("Failed to create Tokio runtime for ZMQ operations");

            rt.block_on(async {
                if let Err(e) = zmq_loop_async(args, seed_sender, shm_consumers, stop_flag).await {
                    error!("ZMQ event loop exited with error: {}", e);
                }
            });
            info!("ZMQ event loop thread finished.");
        })
    }
}

fn get_consumer_from_pool(
    shm_consumers_mutex: &Arc<Mutex<HashMap<String, Arc<SeedShmemPoolConsumer>>>>,
    shm_name: &str,
) -> Result<Arc<SeedShmemPoolConsumer>> {
    let mut consumers = shm_consumers_mutex
        .lock()
        .map_err(|_| Error::ShmError("Mutex poisoned".to_string()))?;
    if let Some(consumer) = consumers.get(shm_name) {
        Ok(Arc::clone(consumer))
    } else {
        info!("Creating new consumer for shared memory: {}", shm_name);
        let consumer = Arc::new(SeedShmemPoolConsumer::new(shm_name)?);
        consumers.insert(shm_name.to_string(), Arc::clone(&consumer));
        Ok(consumer)
    }
}

async fn zmq_loop_async(
    args: CliArgs,
    seed_sender: Sender<Vec<u8>>,
    shm_consumers: Arc<Mutex<HashMap<String, Arc<SeedShmemPoolConsumer>>>>,
    stop_flag: Arc<AtomicBool>,
) -> Result<()> {
    info!("ZMQ event loop (async) starting...");
    let dealer_id = generate_dealer_id();
    let heartbeat_duration = time::Duration::from_secs(args.heartbeat);
    let select_poll_timeout = time::Duration::from_millis(1);

    while !stop_flag.load(Ordering::Acquire) {
        let mut opts = SocketOptions::default();
        opts.peer_identity(PeerIdentity::try_from(dealer_id.as_bytes())?);
        let mut socket = DealerSocket::with_options(opts);

        info!(
            "Dealer connecting to router: {} with ID: {}",
            args.router, dealer_id
        );
        if let Err(e) = socket.connect(&args.router).await {
            error!(
                "Failed to connect to router {}: {}. Retrying in 1s...",
                args.router, e
            );
            tokio::time::sleep(time::Duration::from_secs(1)).await;
            continue;
        }
        info!("Dealer connected to router at {}", args.router);

        let mut heartbeat_timer = interval(heartbeat_duration);
        heartbeat_timer.set_missed_tick_behavior(MissedTickBehavior::Delay);
        let mut heartbeat_count = 0;
        let mut msg_count: u64 = 0;

        loop {
            if stop_flag.load(Ordering::Acquire) {
                info!("Shutdown requested, exiting ZMQ event loop.");
                break;
            }
            tokio::select! {
                recv_res = socket.recv() => {
                    match recv_res {
                        Ok(message) => {
                            let frames: Vec<Vec<u8>> = message.into_vec().into_iter().map(|b| b.as_ref().to_vec()).collect();
                            if frames.is_empty() {
                                warn!("ZMQ recv returned empty frames.");
                                continue;
                            }
                            let cmd = String::from_utf8_lossy(&frames[0]);
                            if cmd == "SEED" && frames.len() >= 3 {
                                let msg_id: &[u8] = frames[1].as_ref();
                                let bundle_data: &[u8] = frames[2].as_ref();
                                debug!("Received SEED BATCH msg_id={} len={}", String::from_utf8_lossy(msg_id), bundle_data.len());

                                let bundle: SubmitBundle = match serde_json::from_slice(bundle_data) {
                                    Ok(b) => b,
                                    Err(e) => {
                                        error!("Failed to parse SubmitBundle: {}", e);
                                        continue;
                                    }
                                };

                                let consumer = match get_consumer_from_pool(&shm_consumers, &bundle.shm_name) {
                                    Ok(c) => c,
                                    Err(e) => {
                                        error!("Error getting/creating consumer for {}: {}", bundle.shm_name, e);
                                        continue;
                                    }
                                };

                                for seed_id in bundle.seed_ids {
                                    match consumer.get_seed_content(seed_id) {
                                        Ok(content) if !content.is_empty() => {
                                            if seed_sender.send(content).is_err() {
                                                error!("Seed queue closed, stopping.");
                                                stop_flag.store(true, Ordering::Release);
                                                return Err(Error::SendError("Seed queue closed".into()));
                                            }
                                        }
                                        Ok(_) => {
                                            warn!(
                                                "Empty seed content for shm: {}, seed_id: {}",
                                                bundle.shm_name, seed_id
                                            );
                                        }
                                        Err(e) => {
                                            error!(
                                                "Error getting seed content for seed_id {} from {}: {}",
                                                seed_id, bundle.shm_name, e
                                            );
                                        }
                                    }
                                }

                                let mut ack_msg = ZmqMessage::from("ACK".as_bytes().to_vec());
                                ack_msg.push_back(msg_id.to_vec().into());
                                ack_msg.push_back(bundle_data.to_vec().into());

                                if let Err(e) = socket.send(ack_msg).await {
                                    error!("Error sending ACK: {}. Reconnecting...", e);
                                    break;
                                }
                                msg_count += 1;
                                if msg_count % 10000 == 0 {
                                    debug!("Processed {} SEED messages", msg_count);
                                }
                            } else {
                                warn!("Unknown or malformed command: {} ({} frames)", cmd, frames.len());
                                 for (i, frame) in frames.iter().enumerate() {
                                    debug!("Frame {}: {}", i, String::from_utf8_lossy(frame));
                                }
                            }
                        }
                        Err(e) => {
                            error!("Error in ZMQ recv loop: {}...", e);
                            break;
                        }
                    }
                }
                _ = heartbeat_timer.tick() => {
                    if stop_flag.load(Ordering::Acquire) {
                        info!("Shutdown requested before heartbeat.");
                        return Ok(());
                    }
                    let mut hb_msg = ZmqMessage::from("HEARTBEAT".as_bytes().to_vec());
                    hb_msg.push_back(args.harness.as_bytes().to_vec().into());

                    if let Err(e) = socket.send(hb_msg).await {
                        error!("Error sending HEARTBEAT: {}. Reconnecting...", e);
                        break;
                    }
                    heartbeat_count += 1;
                    if heartbeat_count % 10 == 0 {
                        debug!("Sent {} heartbeats so far", heartbeat_count);
                    }
                }
                _ = tokio::time::sleep(time::Duration::from_millis(10)), if stop_flag.load(Ordering::Acquire) => {
                    info!("Shutdown requested, exiting ZMQ event loop.");
                    break;
                }
            }
        }
        if stop_flag.load(Ordering::Acquire) {
            break;
        }
        info!("Reconnecting to router in 1s...");
        tokio::time::sleep(time::Duration::from_secs(1)).await;
    }
    info!("ZMQ event loop (async) exited.");
    Ok(())
}

fn generate_dealer_id() -> String {
    let mut rng = rand::thread_rng();
    let random_num: u16 = rng.gen();
    format!("SC-{:04x}", random_num)
}

pub fn calculate_sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let result = hasher.finalize();
    hex::encode(result)
}
