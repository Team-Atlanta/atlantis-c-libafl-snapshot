use std::borrow::Cow;
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::PathBuf;

use log::{debug, info, warn};

use libafl::{
    inputs::{Input, UsesInput},
    mutators::MultiMutator,
    state::{HasMaxSize, HasRand},
    HasMetadata,
};
use libafl_bolts::Named;

// Poll every 1000 cycles by default
const DEFAULT_POLL_INTERVAL: usize = 1000;

/// A mutator that polls a directory for new seed files and adds them to the corpus.
/// State is maintained in the struct between cycles.
pub struct FilePollerMutator {
    seed_share_dir: PathBuf,
    seen_files: HashSet<OsString>,
    poll_interval: usize,
    cycles_since_poll: usize,
}

impl FilePollerMutator {
    pub fn new(seed_share_dir: PathBuf, poll_interval: Option<usize>) -> Self {
        let poll_interval = poll_interval.unwrap_or(DEFAULT_POLL_INTERVAL);
        info!(
            "FilePollerMutator: Watching {:?} every {} cycles",
            seed_share_dir, poll_interval
        );
        Self {
            seed_share_dir,
            seen_files: HashSet::new(),
            poll_interval,
            cycles_since_poll: 0,
        }
    }

    fn poll_directory(&mut self) -> Vec<Vec<u8>> {
        let mut new_seeds = Vec::new();

        let entries = match fs::read_dir(&self.seed_share_dir) {
            Ok(entries) => entries,
            Err(e) => {
                debug!("FilePollerMutator: Directory not ready: {}", e);
                return new_seeds;
            }
        };

        for entry in entries.flatten() {
            let file_name = entry.file_name();

            if self.seen_files.contains(&file_name) {
                continue;
            }

            let path = entry.path();
            if !path.is_file() {
                continue;
            }

            match fs::read(&path) {
                Ok(contents) => {
                    if !contents.is_empty() {
                        info!(
                            "FilePollerMutator: New seed {:?} ({} bytes)",
                            file_name,
                            contents.len()
                        );
                        new_seeds.push(contents);
                    }
                    self.seen_files.insert(file_name);
                }
                Err(e) => {
                    warn!("FilePollerMutator: Failed to read {:?}: {}", path, e);
                }
            }
        }

        if !new_seeds.is_empty() {
            info!(
                "FilePollerMutator: Loaded {} new seeds (total seen: {})",
                new_seeds.len(),
                self.seen_files.len()
            );
        }

        new_seeds
    }
}

impl Named for FilePollerMutator {
    fn name(&self) -> &Cow<'static, str> {
        &Cow::Borrowed("file_poller")
    }
}

impl<I, S> MultiMutator<I, S> for FilePollerMutator
where
    S: UsesInput + HasMetadata + HasRand + HasMaxSize,
    I: From<Vec<u8>> + Input,
{
    fn multi_mutate(
        &mut self,
        _state: &mut S,
        _input: &I,
        _max_count: Option<usize>,
    ) -> Result<Vec<I>, libafl::Error> {
        self.cycles_since_poll += 1;

        if self.cycles_since_poll < self.poll_interval {
            return Ok(Vec::new());
        }

        self.cycles_since_poll = 0;

        let new_seeds = self.poll_directory();
        let new_inputs: Vec<I> = new_seeds.into_iter().map(I::from).collect();

        Ok(new_inputs)
    }
}
