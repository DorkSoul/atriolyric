"""One background worker for slow, rate-limited provider work (MusicBrainz
tracklists, ListenBrainz info, cover art): jobs run one at a time inside `bulk()`,
so page requests always get the next provider slot. A job already queued or
running under the same key is not queued again. Jobs open their own database
session.

The worker has two lists. Jobs someone's request caused (`submit`: a catalog
cover, a wanted entry's metadata, an artist re-check) run in the order
submitted. Scheduled upkeep (`submit(..., scheduled=True)`: the want-list
catalog refresh and metadata fill) runs only when none of those is waiting,
and a long scheduled job hands the worker over between its units of work
(`yield_to_user_jobs`), so it never keeps a cover or tracklist someone is
looking at waiting for more than the request it is in the middle of.

`submit_interactive` is a second, separate lane for look-ups someone is
waiting to see (an info dialog, lyrics, the first look at a catalog page)
that a request answered from what is stored (app.deferred): they never wait
behind a long bulk job (a whole artist's tracklists), and they run outside
`bulk()`, with a page request's priority for the provider slot.

Both lanes are bounded, because their keys come from what callers send
(names, MBIDs): each holds at most a fixed number of waiting jobs, and at
most a fixed number of queued-or-running jobs per submitting user where the
submitter is known. A job over a limit is refused (`submit` returns False
and nothing is kept); the caller answers from what is stored, as it does
when nothing is cached, and a later request tries again.

`_lock` guards everything below it: the three job lists, the pending keys,
the per-user counts, both worker handles, their stop flags and the yield
marker. `_wake` is its condition; the workers sleep on it."""

import logging
import threading
from collections import Counter, deque
from collections.abc import Callable

from app.background import spawn
from app.metadata_providers._throttle import bulk

logger = logging.getLogger(__name__)

# Waiting user-triggered jobs on the background lane. A catalog page asks
# for up to 150 covers at once, so the per-user limit leaves room for one
# such page; at about a request a second the whole list is still minutes,
# not hours, of work.
MAX_PENDING = 1000
MAX_PENDING_PER_USER = 200
# Scheduled jobs have fixed keys, so de-duplication already bounds them.
MAX_SCHEDULED_PENDING = 20
# The interactive lane: each job is one dialog's or page's look-ups.
MAX_INTERACTIVE_PENDING = 200
MAX_INTERACTIVE_PER_USER = 50

_BACKGROUND = "background"
_SCHEDULED = "scheduled"
_INTERACTIVE = "interactive"

_Job = tuple[str, Callable[[], None]]

_lock = threading.Lock()
_wake = threading.Condition(_lock)
_keys: set[str] = set()
_jobs: deque[_Job] = deque()  # user-triggered, background lane
_scheduled_jobs: deque[_Job] = deque()
_interactive_jobs: deque[_Job] = deque()
# key -> (lane, user id) of each pending job whose submitter is known, and
# how many each (lane, user id) has queued or running.
_owners: dict[str, tuple[str, int]] = {}
_per_user: Counter[tuple[str, int]] = Counter()
_worker: threading.Thread | None = None
_interactive_worker: threading.Thread | None = None
# Lanes whose "full" warning has been logged since they last took a job.
_reported_full: set[str] = set()
# shutdown() was called: the worker ends once it has nothing left to run.
_stopping = False
_interactive_stopping = False
# The worker thread while it is inside a scheduled job (and not inside a
# job that one yielded to): only then does yield_to_user_jobs do anything.
_may_yield: threading.Thread | None = None


def _run_job(key: str, job: Callable[[], None], *, background: bool) -> None:
    try:
        if background:
            with bulk():
                job()
        else:
            job()
    except Exception:  # background work must never kill the worker
        logger.exception("Provider job %s failed", key)
    finally:
        with _lock:
            _keys.discard(key)
            owner = _owners.pop(key, None)
            if owner is not None:
                _per_user[owner] -= 1
                if _per_user[owner] <= 0:
                    del _per_user[owner]


def _run() -> None:
    global _worker, _stopping, _may_yield
    while True:
        with _wake:
            while not _jobs and not _scheduled_jobs:
                if _stopping:  # shutdown(), and everything queued has run
                    _stopping = False
                    _worker = None
                    return
                _wake.wait()
            scheduled = not _jobs  # user-triggered work goes first
            key, job = (_scheduled_jobs if scheduled else _jobs).popleft()
            if scheduled:
                _may_yield = threading.current_thread()
        try:
            _run_job(key, job, background=True)
        finally:
            if scheduled:
                with _lock:
                    _may_yield = None


def _run_interactive() -> None:
    global _interactive_worker, _interactive_stopping
    while True:
        with _wake:
            while not _interactive_jobs:
                if _interactive_stopping:
                    _interactive_stopping = False
                    _interactive_worker = None
                    return
                _wake.wait()
            key, job = _interactive_jobs.popleft()
        _run_job(key, job, background=False)


def yield_to_user_jobs() -> int:
    """For a long scheduled job, between its units of work (and with no
    database transaction open): run the user-triggered jobs that are waiting,
    here on the worker, then return to the scheduled job. Returns how many
    ran. Does nothing anywhere else - in a user-triggered job, in a job this
    yielded to, or off the worker thread (the same code called directly)."""
    global _may_yield
    me = threading.current_thread()
    ran = 0
    while True:
        with _lock:
            if _may_yield is not me or not _jobs:
                return ran
            key, job = _jobs.popleft()
            _may_yield = None  # the job it runs must not yield in turn
        try:
            _run_job(key, job, background=True)
        finally:
            with _lock:
                _may_yield = me
        ran += 1


def _accept(key: str, lane: str, user_id: int | None, waiting: int, limit: int, user_limit: int) -> bool:
    """Record `key` as pending unless it already is or a limit is reached.
    Call with `_lock` held."""
    if key in _keys:
        return False
    if waiting >= limit:
        if lane not in _reported_full:  # once per time it fills, not per refusal
            _reported_full.add(lane)
            logger.warning("Provider queue (%s) is full at %d waiting jobs; refusing more", lane, waiting)
        return False
    if user_id is not None:
        owner = (lane, user_id)
        if _per_user[owner] >= user_limit:
            logger.debug("Provider queue (%s): user %s has %d jobs pending; refused", lane, user_id, user_limit)
            return False
        _owners[key] = owner
        _per_user[owner] += 1
    _reported_full.discard(lane)
    _keys.add(key)
    return True


def submit(
    key: str, job: Callable[[], None], *, user_id: int | None = None, scheduled: bool = False
) -> bool:
    """Queue `job` on the background lane. False, with nothing queued, when
    a job with this key is already pending or the lane is full (MAX_PENDING
    waiting, or MAX_PENDING_PER_USER queued or running for `user_id`, the
    user whose request caused it). `scheduled` jobs wait for every
    user-triggered one."""
    global _worker
    with _wake:
        if scheduled:
            accepted = _accept(key, _SCHEDULED, None, len(_scheduled_jobs), MAX_SCHEDULED_PENDING, 0)
        else:
            accepted = _accept(key, _BACKGROUND, user_id, len(_jobs), MAX_PENDING, MAX_PENDING_PER_USER)
        if not accepted:
            return False
        if _worker is None or not _worker.is_alive():
            _worker = spawn("provider-queue", _run)
        # Under the lock, so the worker's shutdown check sees it.
        (_scheduled_jobs if scheduled else _jobs).append((key, job))
        _wake.notify_all()
    return True


def submit_interactive(key: str, job: Callable[[], None], *, user_id: int | None = None) -> bool:
    """Queue `job` on the interactive lane. False, with nothing queued, when
    a job with this key is already pending (on either lane) or the lane is
    full (MAX_INTERACTIVE_PENDING waiting, or MAX_INTERACTIVE_PER_USER queued
    or running for `user_id`)."""
    global _interactive_worker
    with _wake:
        if not _accept(
            key, _INTERACTIVE, user_id, len(_interactive_jobs),
            MAX_INTERACTIVE_PENDING, MAX_INTERACTIVE_PER_USER,
        ):
            return False
        if _interactive_worker is None or not _interactive_worker.is_alive():
            _interactive_worker = spawn("provider-queue-interactive", _run_interactive)
        _interactive_jobs.append((key, job))
        _wake.notify_all()
    return True


def shutdown() -> None:
    """Let the workers finish what is queued and end, so the app's shutdown
    (background.join_all) is not kept waiting on an idle worker."""
    global _stopping, _interactive_stopping
    with _wake:
        if _worker is not None and _worker.is_alive():
            _stopping = True
        if _interactive_worker is not None and _interactive_worker.is_alive():
            _interactive_stopping = True
        _wake.notify_all()


def pending(key: str) -> bool:
    """Whether a job with this key is queued or running."""
    with _lock:
        return key in _keys
