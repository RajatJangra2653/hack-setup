"""Async worker infrastructure — task queues, worker pools, concurrency.

Provides:
  - AsyncWorkerPool: bounded pool of concurrent workers
  - TaskQueue: priority task queue with retry
  - WorkerRegistry: global registry of background workers
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine
from uuid import uuid4

logger = logging.getLogger(__name__)


class TaskPriority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class WorkerTask:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.PENDING
    handler: Callable[..., Coroutine] | None = None
    args: tuple = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str = ""
    retry_count: int = 0
    max_retries: int = 3

    @property
    def _priority_order(self) -> int:
        return {"high": 0, "normal": 1, "low": 2}[self.priority.value]

    def __lt__(self, other: WorkerTask) -> bool:
        return self._priority_order < other._priority_order


class AsyncWorkerPool:
    """Bounded async worker pool for concurrent task execution."""

    def __init__(
        self,
        max_workers: int = 10,
        *,
        name: str = "default",
    ) -> None:
        self._name = name
        self._semaphore = asyncio.Semaphore(max_workers)
        self._queue: asyncio.PriorityQueue[WorkerTask] = asyncio.PriorityQueue()
        self._active: dict[str, WorkerTask] = {}
        self._completed: list[WorkerTask] = []
        self._running = False
        self._workers: list[asyncio.Task] = []

    async def submit(self, task: WorkerTask) -> str:
        """Submit a task for execution. Returns task ID."""
        await self._queue.put(task)
        logger.debug("Task %s submitted to pool '%s'", task.id, self._name)
        return task.id

    async def start(self, num_workers: int = 3) -> None:
        """Start worker consumers."""
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i))
            for i in range(num_workers)
        ]
        logger.info("Worker pool '%s' started with %d workers", self._name, num_workers)

    async def stop(self) -> None:
        """Stop all workers gracefully."""
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Worker pool '%s' stopped", self._name)

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            async with self._semaphore:
                await self._execute(task, worker_id)

    async def _execute(self, task: WorkerTask, worker_id: int) -> None:
        task.status = TaskStatus.RUNNING
        self._active[task.id] = task

        try:
            if task.handler:
                task.result = await task.handler(*task.args, **task.kwargs)
            task.status = TaskStatus.COMPLETED
            logger.debug("Worker %d completed task %s", worker_id, task.id)
        except Exception as exc:
            task.error = str(exc)
            task.retry_count += 1
            if task.retry_count < task.max_retries:
                task.status = TaskStatus.PENDING
                await self._queue.put(task)
                logger.warning("Worker %d: task %s failed, retry %d/%d", worker_id, task.id, task.retry_count, task.max_retries)
            else:
                task.status = TaskStatus.FAILED
                logger.error("Worker %d: task %s permanently failed: %s", worker_id, task.id, exc)
        finally:
            self._active.pop(task.id, None)
            self._completed.append(task)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def active(self) -> int:
        return len(self._active)

    def stats(self) -> dict[str, Any]:
        return {
            "pool": self._name,
            "pending": self.pending,
            "active": self.active,
            "completed": sum(1 for t in self._completed if t.status == TaskStatus.COMPLETED),
            "failed": sum(1 for t in self._completed if t.status == TaskStatus.FAILED),
        }


class WorkerRegistry:
    """Global registry of worker pools."""

    def __init__(self) -> None:
        self._pools: dict[str, AsyncWorkerPool] = {}

    def register(self, name: str, pool: AsyncWorkerPool) -> None:
        self._pools[name] = pool

    def get(self, name: str) -> AsyncWorkerPool | None:
        return self._pools.get(name)

    async def start_all(self) -> None:
        for name, pool in self._pools.items():
            await pool.start()

    async def stop_all(self) -> None:
        for pool in self._pools.values():
            await pool.stop()

    def stats(self) -> dict[str, Any]:
        return {name: pool.stats() for name, pool in self._pools.items()}


_registry = WorkerRegistry()


def get_worker_registry() -> WorkerRegistry:
    return _registry
