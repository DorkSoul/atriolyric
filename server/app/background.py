"""Every background thread the app starts goes through spawn(), so shutdown
can wait for them in one place (join_all) instead of the process cutting
off a worker mid-commit.

The threads stay daemons: a scan of a big library can outlast any
reasonable shutdown wait, and must not keep the process alive for ever."""

import threading
import time
from collections.abc import Callable

_lock = threading.Lock()
_threads: list[threading.Thread] = []


def spawn(name: str, target: Callable[..., object], *args: object) -> threading.Thread:
    """Start `target(*args)` in a named daemon thread and remember it."""
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    with _lock:
        # Forget the finished ones, so a long-running server does not keep
        # every thread it ever started.
        _threads[:] = [t for t in _threads if t.is_alive()]
        _threads.append(thread)
    thread.start()
    return thread


def join_all(timeout: float) -> list[str]:
    """Wait up to `timeout` seconds in total for every spawned thread.
    Returns the names of those still running."""
    deadline = time.monotonic() + timeout
    with _lock:
        threads = list(_threads)
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)
    return [t.name for t in threads if t.is_alive()]
