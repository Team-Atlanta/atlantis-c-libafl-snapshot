use std::collections::VecDeque;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use rdkafka::client::Client;
use rdkafka::config::{ClientConfig, FromClientConfig, FromClientConfigAndContext};
use rdkafka::consumer::base_consumer::PartitionQueue;
use rdkafka::consumer::{
    BaseConsumer, CommitMode, Consumer, ConsumerContext, ConsumerGroupMetadata,
    DefaultConsumerContext, RebalanceProtocol,
};
use rdkafka::error::KafkaResult;
use rdkafka::groups::GroupList;
use rdkafka::message::{BorrowedMessage, OwnedMessage};
use rdkafka::metadata::Metadata;
use rdkafka::topic_partition_list::{Offset, TopicPartitionList};
use rdkafka::util::Timeout;

/// The missing Consumer equivalent to `rdkafka::ThreadedProducer`.
#[must_use = "The threaded consumer will stop immediately if unused"]
pub struct ThreadedConsumer<C>
where
    C: ConsumerContext + 'static,
{
    consumer: Arc<BaseConsumer<C>>,
    // bleh
    message_queue: Arc<Mutex<VecDeque<KafkaResult<OwnedMessage>>>>,
    should_stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl FromClientConfig for ThreadedConsumer<DefaultConsumerContext> {
    fn from_config(config: &ClientConfig) -> KafkaResult<ThreadedConsumer<DefaultConsumerContext>> {
        ThreadedConsumer::from_config_and_context(config, DefaultConsumerContext)
    }
}

/// Poll the [`BaseConsumer`], and add whatever result we get to the
/// message queue.
fn do_poll<C, T>(
    consumer: &Arc<BaseConsumer<C>>,
    message_queue: &Arc<Mutex<VecDeque<KafkaResult<OwnedMessage>>>>,
    timeout: T,
) where
    C: ConsumerContext + 'static,
    T: Into<Timeout>,
{
    if let Some(result) = consumer.poll(timeout) {
        let mut queue = message_queue.lock().unwrap();
        match result {
            Ok(msg) => queue.push_back(Ok(msg.detach())),
            Err(e) => queue.push_back(Err(e)),
        }
    }
}

impl<C> FromClientConfigAndContext<C> for ThreadedConsumer<C>
where
    C: ConsumerContext + 'static,
{
    fn from_config_and_context(
        config: &ClientConfig,
        context: C,
    ) -> KafkaResult<ThreadedConsumer<C>> {
        let consumer = Arc::new(BaseConsumer::from_config_and_context(config, context)?);
        let message_queue = Arc::new(Mutex::new(VecDeque::new()));
        let should_stop = Arc::new(AtomicBool::new(false));
        let thread = {
            let consumer = Arc::clone(&consumer);
            let message_queue = Arc::clone(&message_queue);
            let should_stop = should_stop.clone();
            thread::Builder::new()
                .name("consumer polling thread".to_string())
                .spawn(move || {
                    // trace!("Polling thread loop started");
                    loop {
                        do_poll(&consumer, &message_queue, Duration::from_millis(100));
                        if should_stop.load(Ordering::Relaxed) {
                            // We received nothing and the thread should
                            // stop, so break the loop.
                            break;
                        }
                    }
                    // trace!("Polling thread loop terminated");
                })
                .expect("Failed to start polling thread")
        };
        Ok(ThreadedConsumer {
            consumer,
            message_queue,
            should_stop,
            handle: Some(thread),
        })
    }
}

impl<C> ThreadedConsumer<C>
where
    C: ConsumerContext + 'static,
{
    /// Polls the internal consumer.
    ///
    /// This is not normally required since the `ThreadedConsumer` has a
    /// thread dedicated to calling `poll` regularly.
    ///
    /// Returns the oldest message that hasn't been consumed yet, which
    /// is not necessarily the message that was received by this poll.
    pub fn poll<T: Into<Timeout>>(&self, timeout: T) -> Option<KafkaResult<OwnedMessage>> {
        do_poll(&self.consumer, &self.message_queue, timeout);
        self.message_queue.lock().unwrap().pop_front()
    }

    /// Returns an iterator over the available messages.
    ///
    /// Note that it's also possible to iterate over the consumer directly.
    pub fn iter(&self) -> Iter<'_, C> {
        Iter(self)
    }

    /// Splits messages for the specified partition into their own queue.
    pub fn split_partition_queue(
        self: &Arc<Self>,
        topic: &str,
        partition: i32,
    ) -> Option<PartitionQueue<C>> {
        self.consumer.split_partition_queue(topic, partition)
    }

    /// Close the queue used by a consumer.
    /// Only exposed for advanced usage of this API and should not be
    /// used under normal circumstances.
    pub fn close_queue(&self) -> KafkaResult<()> {
        self.consumer.close_queue()
    }

    /// Returns true if the consumer is closed, else false.
    pub fn closed(&self) -> bool {
        self.consumer.closed()
    }

    // /// Sets a callback that will be invoked whenever the queue becomes
    // /// nonempty.
    // pub fn set_nonempty_callback<F>(&mut self, f: F)
    // where
    //     F: Fn() + Send + Sync + 'static,
    // {
    //     self.consumer.set_nonempty_callback(f);
    // }
}

impl<C> Consumer<C> for ThreadedConsumer<C>
where
    C: ConsumerContext + 'static,
{
    fn client(&self) -> &Client<C> {
        self.consumer.client()
    }

    fn group_metadata(&self) -> Option<ConsumerGroupMetadata> {
        self.consumer.group_metadata()
    }

    fn subscribe(&self, topics: &[&str]) -> KafkaResult<()> {
        self.consumer.subscribe(topics)
    }

    fn unsubscribe(&self) {
        self.consumer.unsubscribe();
    }

    fn assign(&self, assignment: &TopicPartitionList) -> KafkaResult<()> {
        self.consumer.assign(assignment)
    }

    fn unassign(&self) -> KafkaResult<()> {
        self.consumer.unassign()
    }

    fn incremental_assign(&self, assignment: &TopicPartitionList) -> KafkaResult<()> {
        self.consumer.incremental_assign(assignment)
    }

    fn incremental_unassign(&self, assignment: &TopicPartitionList) -> KafkaResult<()> {
        self.consumer.incremental_unassign(assignment)
    }

    fn seek<T: Into<Timeout>>(
        &self,
        topic: &str,
        partition: i32,
        offset: Offset,
        timeout: T,
    ) -> KafkaResult<()> {
        self.consumer.seek(topic, partition, offset, timeout)
    }

    fn seek_partitions<T: Into<Timeout>>(
        &self,
        topic_partition_list: TopicPartitionList,
        timeout: T,
    ) -> KafkaResult<TopicPartitionList> {
        self.consumer.seek_partitions(topic_partition_list, timeout)
    }

    fn commit(
        &self,
        topic_partition_list: &TopicPartitionList,
        mode: CommitMode,
    ) -> KafkaResult<()> {
        self.consumer.commit(topic_partition_list, mode)
    }

    fn commit_consumer_state(&self, mode: CommitMode) -> KafkaResult<()> {
        self.consumer.commit_consumer_state(mode)
    }

    fn commit_message(&self, message: &BorrowedMessage<'_>, mode: CommitMode) -> KafkaResult<()> {
        self.consumer.commit_message(message, mode)
    }

    fn store_offset(&self, topic: &str, partition: i32, offset: i64) -> KafkaResult<()> {
        self.consumer.store_offset(topic, partition, offset)
    }

    fn store_offset_from_message(&self, message: &BorrowedMessage<'_>) -> KafkaResult<()> {
        self.consumer.store_offset_from_message(message)
    }

    fn store_offsets(&self, tpl: &TopicPartitionList) -> KafkaResult<()> {
        self.consumer.store_offsets(tpl)
    }

    fn subscription(&self) -> KafkaResult<TopicPartitionList> {
        self.consumer.subscription()
    }

    fn assignment(&self) -> KafkaResult<TopicPartitionList> {
        self.consumer.assignment()
    }

    fn assignment_lost(&self) -> bool {
        self.consumer.assignment_lost()
    }

    fn committed<T: Into<Timeout>>(&self, timeout: T) -> KafkaResult<TopicPartitionList> {
        self.consumer.committed(timeout)
    }

    fn committed_offsets<T: Into<Timeout>>(
        &self,
        tpl: TopicPartitionList,
        timeout: T,
    ) -> KafkaResult<TopicPartitionList> {
        self.consumer.committed_offsets(tpl, timeout)
    }

    fn offsets_for_timestamp<T: Into<Timeout>>(
        &self,
        timestamp: i64,
        timeout: T,
    ) -> KafkaResult<TopicPartitionList> {
        self.consumer.offsets_for_timestamp(timestamp, timeout)
    }

    fn offsets_for_times<T: Into<Timeout>>(
        &self,
        timestamps: TopicPartitionList,
        timeout: T,
    ) -> KafkaResult<TopicPartitionList> {
        self.consumer.offsets_for_times(timestamps, timeout)
    }

    fn position(&self) -> KafkaResult<TopicPartitionList> {
        self.consumer.position()
    }

    fn fetch_metadata<T: Into<Timeout>>(
        &self,
        topic: Option<&str>,
        timeout: T,
    ) -> KafkaResult<Metadata> {
        self.consumer.fetch_metadata(topic, timeout)
    }

    fn fetch_watermarks<T: Into<Timeout>>(
        &self,
        topic: &str,
        partition: i32,
        timeout: T,
    ) -> KafkaResult<(i64, i64)> {
        self.consumer.fetch_watermarks(topic, partition, timeout)
    }

    fn fetch_group_list<T: Into<Timeout>>(
        &self,
        group: Option<&str>,
        timeout: T,
    ) -> KafkaResult<GroupList> {
        self.consumer.fetch_group_list(group, timeout)
    }

    fn pause(&self, partitions: &TopicPartitionList) -> KafkaResult<()> {
        self.consumer.pause(partitions)
    }

    fn resume(&self, partitions: &TopicPartitionList) -> KafkaResult<()> {
        self.consumer.resume(partitions)
    }

    fn rebalance_protocol(&self) -> RebalanceProtocol {
        self.consumer.rebalance_protocol()
    }
}

impl<C> Drop for ThreadedConsumer<C>
where
    C: ConsumerContext + 'static,
{
    fn drop(&mut self) {
        // trace!("Destroy ThreadedConsumer");
        if let Some(handle) = self.handle.take() {
            // trace!("Stopping polling");
            self.should_stop.store(true, Ordering::Relaxed);
            // trace!("Waiting for polling thread termination");
            match handle.join() {
                Ok(()) => (),  //trace!("Polling stopped"),
                Err(_e) => (), //warn!("Failure while terminating thread: {:?}", e),
            };
        }
        // trace!("ThreadedConsumer destroyed");
    }
}

/// A convenience iterator over the messages in a [`ThreadedConsumer`].
///
/// Each call to [`Iter::next`] simply pops one message from the
/// internal message queue.
pub struct Iter<'a, C>(&'a ThreadedConsumer<C>)
where
    C: ConsumerContext + 'static;

impl<C> Iterator for Iter<'_, C>
where
    C: ConsumerContext + 'static,
{
    type Item = KafkaResult<OwnedMessage>;

    fn next(&mut self) -> Option<Self::Item> {
        self.0.message_queue.lock().unwrap().pop_front()
    }
}

impl<'a, C> IntoIterator for &'a ThreadedConsumer<C>
where
    C: ConsumerContext,
{
    type Item = KafkaResult<OwnedMessage>;
    type IntoIter = Iter<'a, C>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}
