use std::borrow::Cow;
use std::sync::Arc;

use libafl::{
    inputs::{HasMutatorBytes, Input, UsesInput},
    mutators::MultiMutator,
    state::{HasMaxSize, HasRand},
    HasMetadata,
};
use libafl_bolts::Named;
use prost::Message as ProstMessage;
use rdkafka::{
    config::{ClientConfig, FromClientConfig},
    consumer::{Consumer, DefaultConsumerContext},
    message::Message as RdkafkaMessage,
};

use crate::protobuf;
use crate::threaded_consumer::ThreadedConsumer;
use crate::util::map_kafka_err_to_libafl;

/// A mutator that, rather than mutating anything, asynchronously reads
/// new seeds from a Kafka topic and adds them to the corpus.
pub struct KafkaConsumerMutator {
    kafka_consumer: Arc<ThreadedConsumer<DefaultConsumerContext>>,
    harness: String,
    name: Cow<'static, str>,
}

impl KafkaConsumerMutator {
    pub fn new(
        config: &ClientConfig,
        topic: Cow<'static, str>,
        harness: &str,
        name: &'static str,
    ) -> Result<Self, libafl::Error> {
        let kafka_consumer =
            ThreadedConsumer::from_config(config).map_err(map_kafka_err_to_libafl)?;

        kafka_consumer
            .subscribe(&[&topic])
            .map_err(map_kafka_err_to_libafl)?;

        Ok(Self {
            kafka_consumer: Arc::new(kafka_consumer),
            harness: harness.to_owned(),
            name: Cow::from(name),
        })
    }
}

impl Named for KafkaConsumerMutator {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<I, S> MultiMutator<I, S> for KafkaConsumerMutator
where
    S: UsesInput + HasMetadata + HasRand + HasMaxSize,
    I: HasMutatorBytes + Input,
{
    fn multi_mutate(
        &mut self,
        _state: &mut S,
        input: &I,
        max_count: Option<usize>,
    ) -> Result<Vec<I>, libafl::Error> {
        let max_count = max_count.unwrap_or(usize::MAX);

        let mut new_inputs = vec![];
        for msg in self.kafka_consumer.iter().take(max_count) {
            match msg {
                Err(e) => println!("[KafkaConsumerMutator] Kafka error: {e:?}"),
                Ok(msg) => {
                    if let Some(payload) = msg.payload() {
                        match protobuf::FuzzerSeeds::decode(payload) {
                            Ok(proto) => {
                                if proto.harness_id == self.harness {
                                    for data in &proto.data {
                                        let mut input_clone = input.clone();
                                        input_clone.resize(0, 0);
                                        input_clone.extend(data);
                                        new_inputs.push(input_clone);
                                    }
                                }
                            }
                            Err(e) => {
                                println!("[KafkaConsumerMutator] Failed to decode protobuf message {payload:?}: {e:?}");
                            }
                        }
                    }
                }
            }
        }

        Ok(new_inputs)
    }
}
