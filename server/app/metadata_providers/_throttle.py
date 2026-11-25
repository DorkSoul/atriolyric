import contextlib
import contextvars
import threading
import time

_bulk = contextvars.ContextVar("provider_bulk", default=False)

# Longest an interactive (page) request will queue for a provider slot before
# giving up, so a backed-up provider never parks request threads for minutes.
INTERACTIVE_MAX_WAIT = 15.0


@contextlib.contextmanager
def bulk():
    """Mark provider calls made inside as background work: they yield to
    page requests instead of competing with them for the rate limit."""
    token = _bulk.set(True)
    try:
        yield
    finally:
        _bulk.reset(token)


class Throttle:
    """Schedule request starts on a fixed cadence across the process.

    MusicBrainz enforces roughly one request per second. Keeping the next
    scheduled slot, rather than measuring from the previous call each time,
    avoids adding drift from small bits of application work between requests.
    A slow HTTP request still limits throughput naturally; this limiter never
    starts requests closer together than the configured interval.

    Callers inside `bulk()` (background fetches) wait while any interactive
    caller is waiting for a slot, so a page request is never queued behind a
    run of background calls. An interactive caller gives up (wait() returns
    False, no slot taken) when its slot would be more than `max_wait` away.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_call = 0.0
        self._interactive_waiting = 0

    def pause(self, seconds: float) -> None:
        """Start no call for `seconds` (a provider's Retry-After). Pushing
        the next slot out is all it takes: background callers wait it out,
        page requests give up at once when it is further than they wait."""
        with self._lock:
            self._next_call = max(self._next_call, time.monotonic() + seconds)

    def paused_for(self) -> float:
        """Seconds until the next call may start (0 when it may now)."""
        with self._lock:
            return max(0.0, self._next_call - time.monotonic())

    def wait(self, max_wait: float | None = None) -> bool:
        is_bulk = _bulk.get()
        if is_bulk:
            while True:
                with self._lock:
                    if self._interactive_waiting == 0:
                        now = time.monotonic()
                        scheduled = max(now, self._next_call)
                        self._next_call = scheduled + self._min_interval
                        break
                time.sleep(0.2)
        else:
            if max_wait is None:
                max_wait = INTERACTIVE_MAX_WAIT
            with self._lock:
                now = time.monotonic()
                scheduled = max(now, self._next_call)
                if scheduled - now > max_wait:
                    return False
                self._next_call = scheduled + self._min_interval
                self._interactive_waiting += 1
        try:
            remaining = scheduled - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        finally:
            if not is_bulk:
                with self._lock:
                    self._interactive_waiting -= 1
        return True
