use rdkafka::error::KafkaError;

pub(crate) fn map_kafka_err_to_libafl(err: KafkaError) -> libafl::Error {
    libafl::Error::unknown(format!("KafkaError: {err:?}"))
}
