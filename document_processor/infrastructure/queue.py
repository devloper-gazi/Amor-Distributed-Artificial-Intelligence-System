"""
Kafka-based message queue for distributed document processing.
Provides reliable message delivery with partitioning and consumer groups.
"""

import json
import asyncio
from typing import Optional, Callable, Any
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from aiokafka.errors import KafkaError
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.models import SourceDocument
from ..core.exceptions import QueueError
from ..infrastructure.monitoring import monitor


class QueueManager:
    """
    Kafka queue manager for async message processing.

    Handles both producing and consuming messages from Kafka topics.
    """

    def __init__(self):
        """Initialize queue manager."""
        self.producer: Optional[AIOKafkaProducer] = None
        self.consumer: Optional[AIOKafkaConsumer] = None
        self._producer_connected = False
        self._consumer_connected = False

    async def connect_producer(self):
        """Connect Kafka producer."""
        if self._producer_connected:
            return

        try:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                compression_type="gzip",
                max_batch_size=16384,
                linger_ms=10,
            )

            await self.producer.start()
            self._producer_connected = True

            logger.info(
                "kafka_producer_connected",
                bootstrap_servers=settings.kafka_bootstrap_servers,
            )

        except KafkaError as e:
            logger.error("kafka_producer_connection_failed", error=str(e))
            raise QueueError(f"Failed to connect Kafka producer: {e}")

    async def connect_consumer(
        self,
        topic: str,
        group_id: Optional[str] = None,
        auto_offset_reset: str = "earliest",
    ):
        """
        Connect Kafka consumer.

        Args:
            topic: Topic to consume from
            group_id: Consumer group ID
            auto_offset_reset: Auto offset reset strategy
        """
        if self._consumer_connected:
            return

        try:
            group_id = group_id or settings.kafka_group_id

            self.consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id=group_id,
                auto_offset_reset=auto_offset_reset,
                enable_auto_commit=True,
                max_poll_records=settings.kafka_max_poll_records,
                session_timeout_ms=settings.kafka_session_timeout_ms,
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            )

            await self.consumer.start()
            self._consumer_connected = True

            logger.info(
                "kafka_consumer_connected",
                topic=topic,
                group_id=group_id,
                bootstrap_servers=settings.kafka_bootstrap_servers,
            )

        except KafkaError as e:
            logger.error("kafka_consumer_connection_failed", error=str(e))
            raise QueueError(f"Failed to connect Kafka consumer: {e}")

    async def disconnect(self):
        """Disconnect producer and consumer."""
        if self.producer and self._producer_connected:
            await self.producer.stop()
            self._producer_connected = False
            logger.info("kafka_producer_disconnected")

        if self.consumer and self._consumer_connected:
            await self.consumer.stop()
            self._consumer_connected = False
            logger.info("kafka_consumer_disconnected")

    async def publish(
        self,
        message: Any,
        topic: Optional[str] = None,
        key: Optional[str] = None,
    ) -> bool:
        """
        Publish message to Kafka topic.

        Args:
            message: Message to publish (will be JSON serialized)
            topic: Topic to publish to (default: settings.kafka_topic)
            key: Optional message key for partitioning

        Returns:
            True if successful
        """
        if not self._producer_connected:
            await self.connect_producer()

        topic = topic or settings.kafka_topic

        try:
            # Convert Pydantic models to dict
            if hasattr(message, "model_dump"):
                message_dict = message.model_dump()
            elif hasattr(message, "dict"):
                message_dict = message.dict()
            else:
                message_dict = message

            # Publish message
            future = await self.producer.send(
                topic,
                value=message_dict,
                key=key.encode("utf-8") if key else None,
            )

            # Wait for acknowledgment
            record_metadata = await future

            monitor.record_queue_publish(topic)

            logger.debug(
                "message_published",
                topic=topic,
                partition=record_metadata.partition,
                offset=record_metadata.offset,
            )

            return True

        except Exception as e:
            logger.error(
                "message_publish_failed",
                topic=topic,
                error=str(e),
            )
            return False

    async def publish_batch(
        self,
        messages: list,
        topic: Optional[str] = None,
    ) -> int:
        """
        Publish batch of messages to Kafka.

        Args:
            messages: List of messages to publish
            topic: Topic to publish to

        Returns:
            Number of successfully published messages
        """
        if not self._producer_connected:
            await self.connect_producer()

        topic = topic or settings.kafka_topic
        successful = 0

        try:
            for message in messages:
                # Convert Pydantic models to dict
                if hasattr(message, "model_dump"):
                    message_dict = message.model_dump()
                elif hasattr(message, "dict"):
                    message_dict = message.dict()
                else:
                    message_dict = message

                # Send without waiting
                await self.producer.send(topic, value=message_dict)
                successful += 1

            # Flush to ensure all messages are sent
            await self.producer.flush()

            logger.info(
                "batch_published",
                topic=topic,
                count=successful,
            )

            return successful

        except Exception as e:
            logger.error(
                "batch_publish_failed",
                topic=topic,
                successful=successful,
                total=len(messages),
                error=str(e),
            )
            return successful

    async def consume(
        self,
        processor: Callable,
        topic: str,
        group_id: Optional[str] = None,
        max_messages: Optional[int] = None,
    ):
        """
        Consume messages from Kafka and process them.

        Args:
            processor: Async function to process each message
            topic: Topic to consume from
            group_id: Consumer group ID
            max_messages: Maximum messages to consume (None for infinite)
        """
        if not self._consumer_connected:
            await self.connect_consumer(topic, group_id)

        messages_processed = 0

        try:
            async for msg in self.consumer:
                try:
                    # Process message
                    await processor(msg.value)

                    monitor.record_queue_consume(topic, "success")

                    logger.debug(
                        "message_consumed",
                        topic=msg.topic,
                        partition=msg.partition,
                        offset=msg.offset,
                    )

                    messages_processed += 1

                    if max_messages and messages_processed >= max_messages:
                        break

                except Exception as e:
                    monitor.record_queue_consume(topic, "failed")

                    logger.error(
                        "message_processing_failed",
                        topic=msg.topic,
                        partition=msg.partition,
                        offset=msg.offset,
                        error=str(e),
                    )

        except Exception as e:
            logger.error("consumer_error", error=str(e))
            raise QueueError(f"Consumer error: {e}")

    async def get_topic_partitions(self, topic: str) -> int:
        """
        Get number of partitions for topic.

        Args:
            topic: Topic name

        Returns:
            Number of partitions
        """
        if not self._producer_connected:
            await self.connect_producer()

        try:
            partitions = await self.producer.partitions_for(topic)
            return len(partitions) if partitions else 0
        except Exception as e:
            logger.error("get_partitions_failed", topic=topic, error=str(e))
            return 0

    async def get_consumer_lag(self) -> dict:
        """
        Get consumer lag information.

        Returns:
            Dictionary with lag information
        """
        if not self._consumer_connected:
            return {}

        try:
            lag_info = {}

            for topic_partition in self.consumer.assignment():
                # Get current position
                position = await self.consumer.position(topic_partition)

                # Get end offset (latest)
                end_offsets = await self.consumer.end_offsets([topic_partition])
                end_offset = end_offsets[topic_partition]

                lag = end_offset - position

                lag_info[f"{topic_partition.topic}-{topic_partition.partition}"] = {
                    "position": position,
                    "end_offset": end_offset,
                    "lag": lag,
                }

            return lag_info

        except Exception as e:
            logger.error("get_consumer_lag_failed", error=str(e))
            return {}


# Global queue manager instance
queue_manager = QueueManager()
