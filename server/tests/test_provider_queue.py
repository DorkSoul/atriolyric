from __future__ import annotations

import threading
import time

from app import provider_queue
from app.metadata_providers._throttle import Throttle, bulk
from tests.conftest import wait_until


def test_interactive_call_gives_up_instead_of_queueing_for_minutes():
    throttle = Throttle(1.0)
    throttle._next_call = time.monotonic() + 60  # a long run of calls already scheduled
    start = time.monotonic()
    assert throttle.wait(max_wait=2) is False
    assert time.monotonic() - start < 1  # refused at once, no sleep, no slot taken
    assert throttle._next_call < time.monotonic() + 61


def test_bulk_waits_for_interactive_callers():
    throttle = Throttle(0.2)
    order: list[str] = []

    def background():
        with bulk():
            throttle.wait()
        order.append("bulk")

    with throttle._lock:
        throttle._interactive_waiting = 1  # a page request is waiting
    thread = threading.Thread(target=background)
    thread.start()
    time.sleep(0.5)
    assert order == []  # held back while a page request waits
    with throttle._lock:
        throttle._interactive_waiting = 0
    thread.join(3)
    assert order == ["bulk"]


def test_queue_runs_jobs_in_order_and_skips_duplicates():
    ran: list[str] = []
    gate = threading.Event()

    def slow():
        gate.wait(3)
        ran.append("a")

    assert provider_queue.submit("t-a", slow) is True
    assert provider_queue.submit("t-a", lambda: ran.append("dup")) is False
    assert provider_queue.submit("t-b", lambda: ran.append("b")) is True
    gate.set()
    wait_until(lambda: len(ran) >= 2, what="both jobs")
    assert ran == ["a", "b"]
    # Once finished, the same key can be queued again.
    wait_until(lambda: not provider_queue.pending("t-a"), what="the first job's key to be released")
    assert provider_queue.submit("t-a", lambda: ran.append("a2")) is True


def _stop_worker() -> None:
    worker = provider_queue._worker
    provider_queue.shutdown()
    if worker is not None:
        worker.join(5)
        assert not worker.is_alive()


def test_queue_worker_is_one_registered_background_thread(monkeypatch):
    from app import background

    _stop_worker()
    monkeypatch.setattr(background, "_threads", [])
    done = threading.Event()
    provider_queue.submit("reg-a", lambda: None)
    provider_queue.submit("reg-b", done.set)
    assert done.wait(5)
    names = [t.name for t in background._threads]
    assert names == ["provider-queue"]  # lazily started once, then reused
    _stop_worker()


def test_shutdown_ends_an_idle_worker_but_not_queued_work():
    _stop_worker()
    first = threading.Event()
    provider_queue.submit("shut-a", first.set)
    assert first.wait(5)
    worker = provider_queue._worker
    provider_queue.shutdown()
    worker.join(5)
    assert not worker.is_alive() and provider_queue._worker is None
    provider_queue.shutdown()  # no worker: nothing left behind for the next one
    assert not provider_queue._stopping and not provider_queue._jobs
    # Work queued behind the shutdown marker still runs.
    later = threading.Event()
    gate = threading.Event()
    provider_queue.submit("shut-b", gate.wait)
    provider_queue.shutdown()
    provider_queue.submit("shut-c", later.set)
    gate.set()
    assert later.wait(5)
    _stop_worker()


def test_interactive_lane_runs_beside_a_long_bulk_job_and_shares_keys():
    gate = threading.Event()
    ran = threading.Event()
    lanes: list[bool] = []

    def interactive():
        from app.metadata_providers._throttle import _bulk

        lanes.append(_bulk.get())
        ran.set()

    assert provider_queue.submit("lane-bulk", lambda: gate.wait(5)) is True
    assert provider_queue.submit_interactive("lane-bulk", interactive) is False  # same key: pending
    assert provider_queue.submit_interactive("lane-ui", interactive) is True
    assert ran.wait(5)  # did not wait for the bulk job
    assert lanes == [False]  # not marked as background work
    assert provider_queue.pending("lane-bulk")
    gate.set()
    wait_until(lambda: not provider_queue.pending("lane-bulk"), what="the bulk job")
    wait_until(lambda: not provider_queue.pending("lane-ui"), what="the interactive key")
    worker = provider_queue._interactive_worker
    _stop_worker()
    if worker is not None:
        worker.join(5)
        assert not worker.is_alive() and provider_queue._interactive_worker is None


def test_a_failing_interactive_job_does_not_stop_the_lane():
    done = threading.Event()

    def boom():
        raise RuntimeError("provider down")

    provider_queue.submit_interactive("lane-boom", boom)
    provider_queue.submit_interactive("lane-after", done.set)
    assert done.wait(5)
    _stop_worker()


def _hold_worker(key: str) -> threading.Event:
    """Occupy the background worker until the returned event is set."""
    gate = threading.Event()
    started = threading.Event()

    def hold():
        started.set()
        gate.wait(5)

    assert provider_queue.submit(key, hold) is True
    assert started.wait(5)
    return gate


def _idle(*keys: str) -> None:
    for key in keys:
        wait_until(lambda key=key: not provider_queue.pending(key), what=f"job {key}")


def test_user_jobs_run_before_scheduled_ones_however_they_were_queued():
    ran: list[str] = []
    gate = _hold_worker("prio-hold")
    assert provider_queue.submit("prio-sched-1", lambda: ran.append("sched-1"), scheduled=True) is True
    assert provider_queue.submit("prio-user-1", lambda: ran.append("user-1")) is True
    assert provider_queue.submit("prio-sched-2", lambda: ran.append("sched-2"), scheduled=True) is True
    assert provider_queue.submit("prio-user-2", lambda: ran.append("user-2")) is True
    # Keys still dedupe across the two lists.
    assert provider_queue.submit("prio-sched-1", lambda: ran.append("dup")) is False
    assert provider_queue.submit("prio-user-1", lambda: ran.append("dup"), scheduled=True) is False
    gate.set()
    wait_until(lambda: len(ran) >= 4, what="all four jobs")
    assert ran == ["user-1", "user-2", "sched-1", "sched-2"]
    _idle("prio-sched-2")


def test_a_long_scheduled_job_yields_to_waiting_user_jobs_between_units():
    ran: list[str] = []
    in_unit = threading.Event()
    go_on = threading.Event()
    yielded: list[int] = []

    def scheduled():
        ran.append("unit-1")
        in_unit.set()
        go_on.wait(5)
        yielded.append(provider_queue.yield_to_user_jobs())
        ran.append("unit-2")
        yielded.append(provider_queue.yield_to_user_jobs())  # nothing waiting

    def user_job():
        from app.metadata_providers._throttle import _bulk

        # Still background work, and it cannot yield in its turn.
        ran.append(f"user bulk={_bulk.get()} yielded={provider_queue.yield_to_user_jobs()}")

    def failing():
        ran.append("boom")
        raise RuntimeError("provider down")

    assert provider_queue.submit("yield-sched", scheduled, scheduled=True) is True
    assert in_unit.wait(5)
    assert provider_queue.submit("yield-user", user_job) is True
    assert provider_queue.submit("yield-boom", failing) is True
    go_on.set()
    _idle("yield-sched")
    # A job that raises while yielded to does not end the scheduled job.
    assert ran == ["unit-1", "user bulk=True yielded=0", "boom", "unit-2"]
    assert yielded == [2, 0]
    assert not provider_queue.pending("yield-user") and not provider_queue.pending("yield-boom")


def test_yielding_does_nothing_outside_a_scheduled_job():
    assert provider_queue.yield_to_user_jobs() == 0  # not on the worker
    ran: list[str] = []
    in_job = threading.Event()
    go_on = threading.Event()
    yielded: list[int] = []

    def user_job():
        in_job.set()
        go_on.wait(5)
        yielded.append(provider_queue.yield_to_user_jobs())
        ran.append("first")

    assert provider_queue.submit("noyield-a", user_job) is True
    assert in_job.wait(5)
    assert provider_queue.submit("noyield-b", lambda: ran.append("second")) is True
    go_on.set()
    _idle("noyield-a", "noyield-b")
    assert ran == ["first", "second"] and yielded == [0]


def test_the_background_lane_refuses_jobs_over_its_limit(monkeypatch, caplog):
    monkeypatch.setattr(provider_queue, "MAX_PENDING", 3)
    ran: list[str] = []
    gate = _hold_worker("cap-hold")
    for n in range(3):
        assert provider_queue.submit(f"cap-{n}", lambda n=n: ran.append(f"cap-{n}")) is True
    caplog.set_level("WARNING", logger="app.provider_queue")
    assert provider_queue.submit("cap-over", lambda: ran.append("over")) is False
    assert provider_queue.submit("cap-over-2", lambda: ran.append("over")) is False
    assert not provider_queue.pending("cap-over")  # refused: nothing kept
    # Said once, not once per refused request.
    assert [r.getMessage() for r in caplog.records] == ["Provider queue (background) is full at 3 waiting jobs; refusing more"]
    assert len(provider_queue._jobs) == 3
    # Scheduled work has its own small limit, so a full user list does not
    # shut it out (and it cannot crowd users out either).
    assert provider_queue.submit("cap-sched", lambda: ran.append("sched"), scheduled=True) is True
    gate.set()
    _idle("cap-sched")
    assert ran == ["cap-0", "cap-1", "cap-2", "sched"]
    # Room again once the list has drained.
    assert provider_queue.submit("cap-over", lambda: ran.append("over")) is True
    _idle("cap-over")


def test_scheduled_jobs_are_bounded_too(monkeypatch):
    monkeypatch.setattr(provider_queue, "MAX_SCHEDULED_PENDING", 1)
    gate = _hold_worker("schedcap-hold")
    assert provider_queue.submit("schedcap-a", lambda: None, scheduled=True) is True
    assert provider_queue.submit("schedcap-b", lambda: None, scheduled=True) is False
    gate.set()
    _idle("schedcap-a")


def test_one_user_cannot_fill_the_background_lane(monkeypatch):
    monkeypatch.setattr(provider_queue, "MAX_PENDING_PER_USER", 2)
    ran: list[str] = []
    gate = _hold_worker("user-hold")
    assert provider_queue.submit("user-1a", lambda: ran.append("1a"), user_id=1) is True
    assert provider_queue.submit("user-1b", lambda: ran.append("1b"), user_id=1) is True
    assert provider_queue.submit("user-1c", lambda: ran.append("1c"), user_id=1) is False
    assert not provider_queue.pending("user-1c")
    # Other users, and jobs with no known submitter, are not affected.
    assert provider_queue.submit("user-2a", lambda: ran.append("2a"), user_id=2) is True
    assert provider_queue.submit("user-anon", lambda: ran.append("anon")) is True
    # A duplicate is not counted against the user.
    assert provider_queue.submit("user-2a", lambda: ran.append("dup"), user_id=2) is False
    assert provider_queue._per_user[("background", 2)] == 1
    gate.set()
    _idle("user-anon")
    assert ran == ["1a", "1b", "2a", "anon"]
    # Finished jobs free the user's share, and no count is left behind.
    assert not provider_queue._per_user and not provider_queue._owners
    assert provider_queue.submit("user-1c", lambda: ran.append("1c"), user_id=1) is True
    _idle("user-1c")


def test_a_failed_job_frees_its_users_share(monkeypatch):
    monkeypatch.setattr(provider_queue, "MAX_PENDING_PER_USER", 1)

    def boom():
        raise RuntimeError("provider down")

    assert provider_queue.submit("share-boom", boom, user_id=7) is True
    _idle("share-boom")
    done = threading.Event()
    assert provider_queue.submit("share-next", done.set, user_id=7) is True
    assert done.wait(5)
    _idle("share-next")


def test_the_interactive_lane_is_bounded_in_total_and_per_user(monkeypatch):
    monkeypatch.setattr(provider_queue, "MAX_INTERACTIVE_PENDING", 3)
    monkeypatch.setattr(provider_queue, "MAX_INTERACTIVE_PER_USER", 2)
    gate = threading.Event()
    started = threading.Event()
    ran: list[str] = []

    def hold():
        started.set()
        gate.wait(5)

    assert provider_queue.submit_interactive("ilane-hold", hold, user_id=1) is True
    assert started.wait(5)
    # User 1's running job counts towards their share.
    assert provider_queue.submit_interactive("ilane-1a", lambda: ran.append("1a"), user_id=1) is True
    assert provider_queue.submit_interactive("ilane-1b", lambda: ran.append("1b"), user_id=1) is False
    assert provider_queue.submit_interactive("ilane-2a", lambda: ran.append("2a"), user_id=2) is True
    assert provider_queue.submit_interactive("ilane-3a", lambda: ran.append("3a"), user_id=3) is True
    assert provider_queue.submit_interactive("ilane-4a", lambda: ran.append("4a"), user_id=4) is False  # full
    assert not provider_queue.pending("ilane-1b") and not provider_queue.pending("ilane-4a")
    # The background lane's share is counted separately.
    done = threading.Event()
    assert provider_queue.submit("ilane-bg", done.set, user_id=1) is True
    assert done.wait(5)
    gate.set()
    _idle("ilane-3a", "ilane-bg")
    assert ran == ["1a", "2a", "3a"]
    assert not provider_queue._per_user
