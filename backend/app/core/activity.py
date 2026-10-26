"""What the AI side of the backend is doing right now, in this process.

- WorkerActivity: a background worker's busy/idle state. A worker polls its
  queue every few seconds; logging every poll would drown the log, logging
  nothing hides whether it still runs. Only transitions are logged - "busy"
  when a queue starts being processed, "idle" with the item count and the
  duration once it is empty - so the log shows when the GPU/CPU should be
  quiet again after a batch.
- In-flight inferences per model (model_provider counts every call): the
  only reliable "BUSY" signal. llama-server's /slots would tell too, but
  calling it wakes a sleeping model (verified on build b11474).
- Foreground turns: chat requests in progress. The vision queue does not
  start a new image while one runs, so a report never shares the GPU with
  background work.

The backend runs as one uvicorn process: this state is complete.
"""

import logging
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime

logger = logging.getLogger("orbitia.activity")

_lock = threading.Lock()
_workers: dict[str, "WorkerActivity"] = {}
_in_flight: dict[str, int] = {}
_last_inference: dict[str, float] = {}
_foreground = 0


def _now_iso(monotonic_value: float | None) -> str | None:
    if monotonic_value is None:
        return None
    return datetime.fromtimestamp(time.time() - (time.monotonic() - monotonic_value), UTC).isoformat()


class WorkerActivity:
    def __init__(self, name: str) -> None:
        self.name = name
        self.busy_since: float | None = None
        self.items = 0
        self.total_items = 0
        self.last_item_at: float | None = None
        self.state = "idle"  # idle | busy | paused | waiting_model
        with _lock:
            _workers[name] = self

    def record(self, worked: bool) -> None:
        now = time.monotonic()
        if worked:
            if self.busy_since is None:
                self.busy_since, self.items = now, 0
                logger.info("worker %s: busy", self.name)
            self.items += 1
            self.total_items += 1
            self.last_item_at = now
            self.state = "busy"
            return
        if self.busy_since is not None:
            logger.info(
                "worker %s: idle - queue empty after %d item(s) in %.1fs", self.name, self.items, now - self.busy_since
            )
            self.busy_since = None
        elif self.state != "idle":
            logger.info("worker %s: idle", self.name)
        self.state = "idle"

    def set_state(self, state: str) -> None:
        """paused / waiting_model: logged once when entered, like busy/idle."""
        if state != self.state:
            if self.busy_since is not None:
                logger.info("worker %s: %s after %d item(s)", self.name, state, self.items)
                self.busy_since = None
            else:
                logger.info("worker %s: %s", self.name, state)
        self.state = state

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "items_this_batch": self.items if self.busy_since is not None else 0,
            "items_since_start": self.total_items,
            "last_item_at": _now_iso(self.last_item_at),
        }


def workers_snapshot() -> list[dict]:
    with _lock:
        workers = list(_workers.values())
    return [w.snapshot() for w in sorted(workers, key=lambda w: w.name)]


def inference_started(model: str) -> None:
    with _lock:
        _in_flight[model] = _in_flight.get(model, 0) + 1


def inference_finished(model: str) -> None:
    with _lock:
        _in_flight[model] = max(0, _in_flight.get(model, 0) - 1)
        _last_inference[model] = time.monotonic()


def in_flight(model: str) -> int:
    with _lock:
        return _in_flight.get(model, 0)


def seconds_since_last_inference(model: str) -> float | None:
    with _lock:
        last = _last_inference.get(model)
    return None if last is None else time.monotonic() - last


@contextmanager
def foreground_turn():
    global _foreground
    with _lock:
        _foreground += 1
    try:
        yield
    finally:
        with _lock:
            _foreground -= 1


def foreground_turns() -> int:
    with _lock:
        return _foreground
