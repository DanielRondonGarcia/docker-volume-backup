import copy
import queue
import threading
from typing import Any, Dict, Optional, Set


class JobEventSubscription:
    """A bounded, coalescing queue for one SSE client."""

    def __init__(self, broker: "JobEventBroker", job_id: str, max_queue_size: int):
        self._broker = broker
        self.job_id = job_id
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._closed = threading.Event()

    def get(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        self._broker.unsubscribe(self.job_id, self)

    def _offer(self, event: Dict[str, Any]) -> None:
        if self._closed.is_set():
            return
        payload = copy.deepcopy(event)
        try:
            self._queue.put_nowait(payload)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            pass


class JobEventBroker:
    """Thread-safe in-process job projection fan-out for live viewers."""

    DEFAULT_QUEUE_SIZE = 8

    def __init__(self, max_queue_size: int = DEFAULT_QUEUE_SIZE):
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        self.max_queue_size = max_queue_size
        self._lock = threading.RLock()
        self._subscribers: Dict[str, Set[JobEventSubscription]] = {}

    def subscribe(self, job_id: str) -> JobEventSubscription:
        subscription = JobEventSubscription(self, job_id, self.max_queue_size)
        with self._lock:
            self._subscribers.setdefault(job_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, job_id: str, subscription: JobEventSubscription) -> None:
        with self._lock:
            subscribers = self._subscribers.get(job_id)
            if subscribers is not None:
                subscribers.discard(subscription)
                if not subscribers:
                    self._subscribers.pop(job_id, None)
            subscription._closed.set()

    def publish(self, job_id: str, event: Dict[str, Any]) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.get(job_id, ()))
        for subscription in subscribers:
            subscription._offer(event)

    def subscriber_count(self, job_id: Optional[str] = None) -> int:
        with self._lock:
            if job_id is not None:
                return len(self._subscribers.get(job_id, ()))
            return sum(len(subscribers) for subscribers in self._subscribers.values())
