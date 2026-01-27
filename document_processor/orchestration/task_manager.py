"""
Distributed Task Manager.

Provides Kafka-based distributed task orchestration with:
- Async worker pool management
- Fault tolerance with automatic retry
- Progress tracking via WebSocket
- Health monitoring
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Callable, Awaitable, Set
from uuid import uuid4

logger = logging.getLogger(__name__)

# Optional Kafka imports
try:
    from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
    HAS_KAFKA = True
except ImportError:
    HAS_KAFKA = False
    logger.warning("aiokafka not installed. Kafka task management disabled.")

try:
    import redis.asyncio as redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


class TaskState(Enum):
    """Task execution states."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class TaskPriority(Enum):
    """Task priority levels."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


@dataclass
class TaskConfig:
    """Configuration for task manager."""
    # Kafka settings
    kafka_enabled: bool = True
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_prefix: str = "tasks"
    kafka_consumer_group: str = "task_workers"
    
    # Redis settings (for state management)
    redis_url: str = "redis://localhost:6379"
    
    # Worker settings
    max_workers: int = 10
    worker_timeout: float = 300.0  # 5 minutes
    
    # Retry settings
    max_retries: int = 3
    retry_delay: float = 5.0
    retry_multiplier: float = 2.0
    
    # Batch settings
    batch_size: int = 100
    batch_timeout: float = 10.0
    
    # Health check
    health_check_interval: float = 30.0


@dataclass
class Task:
    """A single task."""
    id: str
    task_type: str
    payload: Dict[str, Any]
    state: TaskState = TaskState.PENDING
    priority: TaskPriority = TaskPriority.NORMAL
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    retry_count: int = 0
    worker_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "task_type": self.task_type,
            "payload": self.payload,
            "state": self.state.value,
            "priority": self.priority.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "worker_id": self.worker_id,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        """Deserialize from dictionary."""
        return cls(
            id=data["id"],
            task_type=data["task_type"],
            payload=data["payload"],
            state=TaskState(data["state"]),
            priority=TaskPriority(data["priority"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            result=data.get("result"),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            worker_id=data.get("worker_id"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class TaskStats:
    """Task manager statistics."""
    total_tasks: int = 0
    pending_tasks: int = 0
    running_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    retried_tasks: int = 0
    active_workers: int = 0
    tasks_per_second: float = 0.0
    average_task_time: float = 0.0


class TaskManager:
    """
    Distributed task manager with Kafka-based queue.
    
    Features:
    - Kafka-based task queue for distributed processing
    - Redis state management for task tracking
    - Async worker pool with health monitoring
    - Automatic retry with exponential backoff
    - Progress tracking and WebSocket updates
    """
    
    def __init__(self, config: Optional[TaskConfig] = None):
        """
        Initialize task manager.
        
        Args:
            config: Task manager configuration
        """
        self.config = config or TaskConfig()
        
        # Task handlers (type -> handler function)
        self._handlers: Dict[str, Callable[[Task], Awaitable[Any]]] = {}
        
        # Components
        self._kafka_producer = None
        self._kafka_consumer = None
        self._redis = None
        
        # Workers
        self._workers: Set[asyncio.Task] = set()
        self._worker_semaphore = asyncio.Semaphore(self.config.max_workers)
        
        # State
        self._running = False
        self._initialized = False
        
        # Statistics
        self.stats = TaskStats()
        self._task_times: List[float] = []
    
    async def initialize(self):
        """Initialize task manager components."""
        if self._initialized:
            return
        
        logger.info("Initializing task manager...")
        
        # Initialize Redis
        if HAS_REDIS:
            try:
                self._redis = redis.from_url(
                    self.config.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
                await self._redis.ping()
                logger.info("Redis connected")
            except Exception as e:
                logger.warning(f"Redis not available: {e}")
        
        # Initialize Kafka
        if self.config.kafka_enabled and HAS_KAFKA:
            try:
                self._kafka_producer = AIOKafkaProducer(
                    bootstrap_servers=self.config.kafka_bootstrap_servers,
                    value_serializer=lambda v: json.dumps(v).encode(),
                )
                await self._kafka_producer.start()
                logger.info("Kafka producer connected")
            except Exception as e:
                logger.warning(f"Kafka not available: {e}")
        
        self._initialized = True
        logger.info("Task manager initialized")
    
    async def start(self):
        """Start task processing."""
        if self._running:
            return
        
        await self.initialize()
        self._running = True
        
        # Start consumer if Kafka is available
        if self._kafka_producer and HAS_KAFKA:
            asyncio.create_task(self._start_consumer())
        
        logger.info("Task manager started")
    
    async def stop(self):
        """Stop task manager."""
        self._running = False
        
        # Wait for workers
        if self._workers:
            logger.info(f"Waiting for {len(self._workers)} workers to complete...")
            await asyncio.gather(*self._workers, return_exceptions=True)
        
        # Close Kafka
        if self._kafka_producer:
            await self._kafka_producer.stop()
        if self._kafka_consumer:
            await self._kafka_consumer.stop()
        
        # Close Redis
        if self._redis:
            await self._redis.close()
        
        logger.info("Task manager stopped")
    
    def register_handler(
        self,
        task_type: str,
        handler: Callable[[Task], Awaitable[Any]],
    ):
        """
        Register a handler for a task type.
        
        Args:
            task_type: Task type identifier
            handler: Async function to handle the task
        """
        self._handlers[task_type] = handler
        logger.info(f"Registered handler for task type: {task_type}")
    
    async def submit_task(
        self,
        task_type: str,
        payload: Dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Submit a task for processing.
        
        Args:
            task_type: Task type identifier
            payload: Task payload data
            priority: Task priority
            metadata: Optional metadata
            
        Returns:
            Task ID
        """
        if not self._initialized:
            await self.initialize()
        
        task = Task(
            id=str(uuid4()),
            task_type=task_type,
            payload=payload,
            priority=priority,
            metadata=metadata or {},
        )
        
        # Save task state
        await self._save_task(task)
        
        # Submit to Kafka or process locally
        if self._kafka_producer:
            topic = f"{self.config.kafka_topic_prefix}_{task_type}"
            await self._kafka_producer.send_and_wait(topic, task.to_dict())
            task.state = TaskState.QUEUED
            await self._save_task(task)
        else:
            # Process locally
            asyncio.create_task(self._process_task(task))
        
        self.stats.total_tasks += 1
        self.stats.pending_tasks += 1
        
        logger.debug(f"Task submitted: {task.id} ({task_type})")
        return task.id
    
    async def submit_batch(
        self,
        task_type: str,
        payloads: List[Dict[str, Any]],
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> List[str]:
        """
        Submit multiple tasks as a batch.
        
        Args:
            task_type: Task type identifier
            payloads: List of task payloads
            priority: Task priority
            
        Returns:
            List of task IDs
        """
        task_ids = []
        
        for payload in payloads:
            task_id = await self.submit_task(
                task_type=task_type,
                payload=payload,
                priority=priority,
            )
            task_ids.append(task_id)
        
        return task_ids
    
    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        if not self._redis:
            return None
        
        data = await self._redis.get(f"task:{task_id}")
        if data:
            return Task.from_dict(json.loads(data))
        return None
    
    async def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get task status."""
        task = await self.get_task(task_id)
        if task:
            return {
                "id": task.id,
                "state": task.state.value,
                "result": task.result,
                "error": task.error,
                "retry_count": task.retry_count,
                "created_at": task.created_at.isoformat(),
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            }
        return None
    
    async def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a pending task.
        
        Returns:
            True if cancelled, False if task not found or already running
        """
        task = await self.get_task(task_id)
        
        if not task:
            return False
        
        if task.state not in (TaskState.PENDING, TaskState.QUEUED):
            return False
        
        task.state = TaskState.CANCELLED
        task.completed_at = datetime.utcnow()
        await self._save_task(task)
        
        self.stats.pending_tasks -= 1
        
        return True
    
    async def _save_task(self, task: Task):
        """Save task state to Redis."""
        if self._redis:
            await self._redis.setex(
                f"task:{task.id}",
                86400 * 7,  # 7 days TTL
                json.dumps(task.to_dict()),
            )
    
    async def _start_consumer(self):
        """Start Kafka consumer for task processing."""
        if not HAS_KAFKA:
            return
        
        # Get all registered task types
        topics = [
            f"{self.config.kafka_topic_prefix}_{task_type}"
            for task_type in self._handlers.keys()
        ]
        
        if not topics:
            logger.warning("No task handlers registered, consumer not started")
            return
        
        self._kafka_consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self.config.kafka_bootstrap_servers,
            group_id=self.config.kafka_consumer_group,
            value_deserializer=lambda v: json.loads(v.decode()),
            enable_auto_commit=False,
        )
        
        await self._kafka_consumer.start()
        logger.info(f"Kafka consumer started for topics: {topics}")
        
        try:
            async for message in self._kafka_consumer:
                if not self._running:
                    break
                
                task_data = message.value
                task = Task.from_dict(task_data)
                
                # Process task with worker semaphore
                asyncio.create_task(self._process_task(task))
                
                # Commit offset
                await self._kafka_consumer.commit()
                
        except Exception as e:
            logger.error(f"Consumer error: {e}")
        finally:
            await self._kafka_consumer.stop()
    
    async def _process_task(self, task: Task):
        """Process a single task."""
        async with self._worker_semaphore:
            worker_task = asyncio.create_task(self._execute_task(task))
            self._workers.add(worker_task)
            worker_task.add_done_callback(self._workers.discard)
    
    async def _execute_task(self, task: Task):
        """Execute task with retry logic."""
        handler = self._handlers.get(task.task_type)
        
        if not handler:
            logger.error(f"No handler for task type: {task.task_type}")
            task.state = TaskState.FAILED
            task.error = f"No handler registered for task type: {task.task_type}"
            task.completed_at = datetime.utcnow()
            await self._save_task(task)
            self.stats.failed_tasks += 1
            return
        
        # Update state
        task.state = TaskState.RUNNING
        task.started_at = datetime.utcnow()
        task.worker_id = str(uuid4())[:8]
        await self._save_task(task)
        
        self.stats.pending_tasks -= 1
        self.stats.running_tasks += 1
        self.stats.active_workers += 1
        
        start_time = time.time()
        
        try:
            # Execute handler with timeout
            result = await asyncio.wait_for(
                handler(task),
                timeout=self.config.worker_timeout,
            )
            
            # Success
            task.state = TaskState.COMPLETED
            task.result = result
            task.completed_at = datetime.utcnow()
            
            self.stats.completed_tasks += 1
            
            # Track execution time
            execution_time = time.time() - start_time
            self._task_times.append(execution_time)
            if len(self._task_times) > 1000:
                self._task_times = self._task_times[-1000:]
            
            logger.debug(f"Task {task.id} completed in {execution_time:.2f}s")
            
        except asyncio.TimeoutError:
            logger.warning(f"Task {task.id} timed out")
            await self._handle_failure(task, "Task timed out")
            
        except Exception as e:
            logger.error(f"Task {task.id} failed: {e}")
            await self._handle_failure(task, str(e))
        
        finally:
            self.stats.running_tasks -= 1
            self.stats.active_workers -= 1
            await self._save_task(task)
    
    async def _handle_failure(self, task: Task, error: str):
        """Handle task failure with retry logic."""
        task.error = error
        task.retry_count += 1
        
        if task.retry_count < self.config.max_retries:
            # Schedule retry
            task.state = TaskState.RETRYING
            self.stats.retried_tasks += 1
            
            # Calculate retry delay with exponential backoff
            delay = self.config.retry_delay * (
                self.config.retry_multiplier ** (task.retry_count - 1)
            )
            
            logger.info(f"Task {task.id} will retry in {delay:.1f}s (attempt {task.retry_count})")
            
            await asyncio.sleep(delay)
            
            # Re-queue task
            await self._process_task(task)
        else:
            # Max retries reached
            task.state = TaskState.FAILED
            task.completed_at = datetime.utcnow()
            self.stats.failed_tasks += 1
            
            logger.error(f"Task {task.id} failed after {task.retry_count} retries")
    
    def get_stats(self) -> TaskStats:
        """Get task manager statistics."""
        # Calculate tasks per second
        recent_times = [t for t in self._task_times[-100:]]
        if recent_times:
            self.stats.average_task_time = sum(recent_times) / len(recent_times)
            self.stats.tasks_per_second = 1.0 / self.stats.average_task_time if self.stats.average_task_time > 0 else 0.0
        
        return self.stats
    
    async def health_check(self) -> Dict[str, Any]:
        """Perform health check."""
        health = {
            "status": "healthy",
            "kafka": "unknown",
            "redis": "unknown",
            "workers": {
                "active": self.stats.active_workers,
                "max": self.config.max_workers,
            },
        }
        
        # Check Redis
        if self._redis:
            try:
                await self._redis.ping()
                health["redis"] = "connected"
            except Exception as e:
                health["redis"] = f"error: {e}"
                health["status"] = "degraded"
        
        # Check Kafka
        if self._kafka_producer:
            health["kafka"] = "connected"
        else:
            health["kafka"] = "not configured"
        
        return health
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
