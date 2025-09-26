use alloc::borrow::Cow;
use libafl::{
    mutators::{Mutator, MutationResult},
    Error,
};
use libafl_bolts::Named;

use log::{debug, error, info, warn};

/// A mutator that truncates the input to a maximum length.
pub struct TruncateMutator {
    max_len: Option<usize>,
    name: Cow<'static, str>,
}

impl TruncateMutator {
    pub fn new(max_len: Option<usize>) -> Self {
        Self {
            max_len,
            name: Cow::from("truncate_mutator"),
        }
    }
}

impl Named for TruncateMutator {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<I, S> Mutator<I, S> for TruncateMutator
where
    I: libafl::inputs::HasMutatorBytes,
{
    fn mutate(&mut self, _state: &mut S, input: &mut I) -> Result<MutationResult, Error> {
        if let Some(max_len) = self.max_len {
            if input.bytes().len() > max_len {
                input.resize(max_len, 0);
                info!("Resizing input to {}", max_len);
                return Ok(MutationResult::Mutated);
            }
        }
        Ok(MutationResult::Skipped)
    }
}
