use std::{marker::PhantomData, sync::Arc};

use alloc::borrow::Cow;
use libafl::{
    corpus::{testcase::Testcase, HasCurrentCorpusId},
    events::EventFirer,
    executors::ExitKind,
    feedbacks::Feedback,
    inputs::{HasTargetBytes, Input},
    observers::ObserversTuple,
    state::State,
};
use libafl_bolts::Named;
use prost::Message as ProstMessage;
use rdkafka::{
    config::{ClientConfig, FromClientConfig},
    consumer::{Consumer, DefaultConsumerContext},
    error::KafkaError,
    message::Message as RdkafkaMessage,
    producer::{BaseRecord, DefaultProducerContext, ThreadedProducer},
};

use crate::{protobuf, threaded_consumer::ThreadedConsumer, util::map_kafka_err_to_libafl};

/// A [`Feedback`] that wraps another [`Feedback`] and sends coverage
/// updates to a Kafka topic whenever that feedback labels an input as
/// "interesting", as well as when requested by a message on another
/// Kafka topic.
#[derive(Clone)]
pub struct KafkaProducerFeedback<F, S> {
    name: Cow<'static, str>,
    wrapped: F,

    campaign: String,
    harness: String,

    kafka_consumer: Option<Arc<ThreadedConsumer<DefaultConsumerContext>>>,
    kafka_producer: Option<Arc<ThreadedProducer<DefaultProducerContext>>>,
    kafka_producer_topic: Option<String>,

    phantom: PhantomData<S>,
}

impl<F, S> KafkaProducerFeedback<F, S>
where
    F: Feedback<S>,
    S: State + HasCurrentCorpusId,
    S::Input: HasTargetBytes,
{
    pub fn new(
        wrapped: F,
        campaign: &str,
        harness: &str,
        consumer_config: Option<&ClientConfig>,
        consumer_topic: Option<&str>,
        producer_config: Option<&ClientConfig>,
        producer_topic: Option<&str>,
    ) -> Result<Self, libafl::Error> {
        let kafka_consumer = if let Some(consumer_config) = consumer_config {
            if let Some(consumer_topic) = consumer_topic {
                let kafka_consumer = ThreadedConsumer::from_config(consumer_config)
                    .map_err(map_kafka_err_to_libafl)?;

                kafka_consumer
                    .subscribe(&[consumer_topic])
                    .map_err(map_kafka_err_to_libafl)?;

                Some(Arc::new(kafka_consumer))
            } else {
                None
            }
        } else {
            None
        };

        let kafka_producer = if let Some(config) = producer_config {
            Some(Arc::new(config.create().map_err(map_kafka_err_to_libafl)?))
        } else {
            None
        };

        Ok(Self {
            name: Cow::from(format!("KafkaProducerFeedback ({})", wrapped.name())),
            wrapped,
            campaign: campaign.to_owned(),
            harness: harness.to_owned(),
            kafka_consumer,
            kafka_producer,
            kafka_producer_topic: producer_topic.map(std::borrow::ToOwned::to_owned),
            phantom: PhantomData,
        })
    }

    fn has_pending_request(&mut self) -> bool {
        if let Some(consumer) = &self.kafka_consumer {
            for msg in consumer.iter() {
                match msg {
                    Err(e) => println!("Kafka error: {e:?}"),
                    Ok(msg) => {
                        if let Some(payload) = msg.payload() {
                            match protobuf::FuzzerSeedRequest::decode(payload) {
                                Ok(proto) => {
                                    if proto.campaign_id == self.campaign
                                        && proto.harness_id == self.harness
                                    {
                                        return true;
                                    }
                                    // otherwise, ignore this request and keep iterating
                                }
                                Err(e) => {
                                    println!(
                                        "Failed to decode protobuf message {payload:?}: {e:?}"
                                    );
                                }
                            }
                        }
                    }
                }
            }
        }
        false
    }

    fn send_bytes(&self, data: &[u8]) -> Result<(), KafkaError> {
        if let Some(producer) = &self.kafka_producer {
            if let Some(topic) = &self.kafka_producer_topic {
                if let Err((err, _rec)) =
                    producer.send(BaseRecord::<str, _>::to(topic).payload(data))
                {
                    return Err(err);
                }
            }
        }
        Ok(())
    }

    fn send_update(
        &self,
        state: &S,
        input: &S::Input,
        is_interesting: bool,
    ) -> Result<(), libafl::Error> {
        let corpus_id = state.current_corpus_id()?; // e.g. Some(CorpusId(226))
        let seed_name = input.generate_name(corpus_id); // e.g. "de33420beec5b0c7"

        let msg = protobuf::FuzzerSeedUpdate {
            campaign_id: self.campaign.to_string(),
            harness_id: self.harness.to_string(),
            seed_name,
            is_interesting,
            data: input.target_bytes().to_vec(),
        };

        let mut buf = Vec::new();
        if let Err(e) = msg.encode(&mut buf) {
            return Err(libafl::Error::unknown(format!(
                "Failed to serialize protobuf message {msg:?}: {e:?}"
            )));
        }

        if let Err(err) = self.send_bytes(&buf) {
            // Don't crash, just print a warning
            println!("Kafka send() error: {err:?}");
        }

        Ok(())
    }

    fn process_new_result(
        &mut self,
        state: &S,
        input: &S::Input,
        is_interesting: bool,
    ) -> Result<(), libafl::Error> {
        if is_interesting || self.has_pending_request() {
            self.send_update(state, input, is_interesting)?;
        }

        Ok(())
    }
}

impl<F, S> Named for KafkaProducerFeedback<F, S> {
    fn name(&self) -> &Cow<'static, str> {
        &self.name
    }
}

impl<F, S> Feedback<S> for KafkaProducerFeedback<F, S>
where
    F: Feedback<S>,
    S: State,
    S::Input: HasTargetBytes,
{
    #[inline]
    fn init_state(&mut self, state: &mut S) -> Result<(), libafl::Error> {
        self.wrapped.init_state(state)
    }

    #[inline]
    fn is_interesting<EM, OT>(
        &mut self,
        state: &mut S,
        manager: &mut EM,
        input: &S::Input,
        observers: &OT,
        exit_kind: &ExitKind,
    ) -> Result<bool, libafl::Error>
    where
        EM: EventFirer<State = S>,
        OT: ObserversTuple<S>,
    {
        let res = self
            .wrapped
            .is_interesting(state, manager, input, observers, exit_kind)?;

        if self.kafka_producer.is_some() {
            self.process_new_result(state, input, res)?;
        }

        Ok(res)
    }

    #[inline]
    fn append_metadata<EM, OT>(
        &mut self,
        state: &mut S,
        manager: &mut EM,
        observers: &OT,
        testcase: &mut Testcase<S::Input>,
    ) -> Result<(), libafl::Error>
    where
        OT: ObserversTuple<S>,
        EM: EventFirer<State = S>,
    {
        self.wrapped
            .append_metadata(state, manager, observers, testcase)
    }

    #[inline]
    fn discard_metadata(&mut self, state: &mut S, input: &S::Input) -> Result<(), libafl::Error> {
        self.wrapped.discard_metadata(state, input)
    }
}
