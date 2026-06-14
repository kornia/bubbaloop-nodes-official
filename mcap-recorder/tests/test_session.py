"""Tests for session-level helpers — the thread-safe drop counter and the
shared sample-decode helper."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder.session import _DropCounter


def test_drop_counter_is_thread_safe():
    counter = _DropCounter()

    def bump():
        for _ in range(2000):
            counter.record()

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No lost updates across 8 concurrent threads (the bug a bare `+= 1` had).
    assert counter.count == 8 * 2000


def test_drop_counter_returns_running_total():
    counter = _DropCounter()
    assert counter.record() == 1
    assert counter.record() == 2
    assert counter.count == 2
