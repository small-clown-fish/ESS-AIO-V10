from __future__ import annotations

import queue
import threading
from typing import Any


class AsyncRecorderProxy:
    """Small write queue in front of CsvRecorder / AlarmRecorder.

    UI thread enqueues writes quickly; a background writer flushes to disk.
    The writer drains items in batches so Windows/Defender/Excel lock delays do
    not cause one context switch per CSV row. close() drains the queue before
    closing the wrapped recorder.
    """

    def __init__(self, recorder: Any, max_queue: int = 20000, max_batch: int = 256) -> None:
        self.recorder = recorder
        self.queue: queue.Queue[tuple[str, tuple[Any, ...], dict[str, Any]] | None] = queue.Queue(maxsize=max_queue)
        self.running = True
        self.dropped_rows = 0
        self.max_batch = max(1, int(max_batch))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def write_row(self, *args: Any, **kwargs: Any) -> None:
        if not self.running:
            return
        try:
            self.queue.put_nowait(("write_row", args, kwargs))
        except queue.Full:
            self.dropped_rows += 1

    def _run(self) -> None:
        while True:
            first = self.queue.get()
            batch = [first]
            # Drain a bounded batch without blocking. This reduces thread wakeups
            # when many BMS devices write CSV at the same sampling tick.
            for _ in range(self.max_batch - 1):
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break

            stop_after_batch = False
            for item in batch:
                if item is None:
                    stop_after_batch = True
                    self.queue.task_done()
                    continue
                method_name, args, kwargs = item
                try:
                    getattr(self.recorder, method_name)(*args, **kwargs)
                except Exception:
                    # Recorder errors should not crash sampling/UI.
                    pass
                finally:
                    self.queue.task_done()

            if stop_after_batch:
                break

    def close(self) -> None:
        if not self.running:
            return
        self.running = False
        self.queue.put(None)
        self.queue.join()
        self.thread.join(timeout=5.0)
        try:
            self.recorder.close()
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self.recorder, name)
