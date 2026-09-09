"""
Tests for the attribution queue.

Modelled on liferay-docker's `templates/job-runner`: cron registers a marker,
a separate serial loop drains it. The two properties worth pinning are the ones
that make a short cron safe next to a long job — idempotent enqueue, and a key
that groups by build pair rather than by signature.
"""

from testray_analytics.analysis.queue import FileQueue, Job

SIG_A = "v2:aaaaaaaaaaaaaaaa"
SIG_B = "v2:bbbbbbbbbbbbbbbb"


def test_enqueue_is_idempotent_like_register_job(tmp_path):
    q = FileQueue(tmp_path / "queue")
    job = Job(routine_id=79529, baseline_build=1, target_build=2, signatures=[SIG_A])
    assert q.register(job) is True
    assert q.register(job) is False, "a tick landing mid-job must not duplicate"
    assert len(q.pending()) == 1


def test_job_name_keys_on_the_build_pair_not_the_signature(tmp_path):
    """Several new signatures in one build share one bundle, diff and prompt."""
    q = FileQueue(tmp_path / "queue")
    a = Job(routine_id=79529, baseline_build=1, target_build=2, signatures=[SIG_A])
    b = Job(routine_id=79529, baseline_build=1, target_build=2, signatures=[SIG_B])
    assert a.name == b.name
    assert q.register(a) is True and q.register(b) is False
    assert len(q.pending()) == 1, "one build pair, one job, one diff paid for"


def test_different_pairs_are_different_jobs(tmp_path):
    q = FileQueue(tmp_path / "queue")
    assert q.register(Job(79529, 1, 2, [SIG_A])) is True
    assert q.register(Job(79529, 2, 3, [SIG_A])) is True
    assert len(q.pending()) == 2


def test_release_drops_the_marker_so_a_rescan_can_requeue(tmp_path):
    q = FileQueue(tmp_path / "queue")
    job = Job(79529, 1, 2, [SIG_A])
    q.register(job)
    q.release(job)
    assert q.pending() == []
    assert q.register(job) is True


def test_pending_is_oldest_first_and_survives_a_corrupt_marker(tmp_path):
    d = tmp_path / "queue"
    q = FileQueue(d)
    q.register(Job(1, 1, 2, []))
    q.register(Job(1, 2, 3, []))
    (d / "corrupt.json").write_text("{not json")
    assert [j.target_build for j in q.pending()] == [2, 3]


def test_queue_path_precedence(monkeypatch, tmp_path):
    from testray_analytics.analysis.config import project_root
    from testray_analytics.analysis.queue import QUEUE_ENV, queue_path
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    assert queue_path({}) == project_root() / "state/queue"
    assert str(queue_path({"queue": {"path": "/var/q"}})) == "/var/q"
    monkeypatch.setenv(QUEUE_ENV, str(tmp_path / "q"))
    assert queue_path({"queue": {"path": "/var/q"}}) == tmp_path / "q"


def test_relative_queue_path_anchors_to_project_root_not_cwd(monkeypatch, tmp_path):
    from testray_analytics.analysis.config import project_root
    from testray_analytics.analysis.queue import QUEUE_ENV, queue_path
    monkeypatch.delenv(QUEUE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert queue_path({"queue": {"path": "state/q"}}) == project_root() / "state/q"


# --- completion records ----------------------------------------------------
#
# The file backend is chosen exactly when the TriageRun Object is absent, and
# that same absence means /o/c/triageresults is absent too — so nothing in
# Testray remembers a pair was analysed. Without a local record the scanner
# re-queues the same pair on every tick, forever, and with --classify that is
# unbounded spend on one analysis.

def test_a_completed_pair_is_not_queued_again(tmp_path):
    q = FileQueue(tmp_path / "queue")
    job = Job(79529, 1, 2, [SIG_A])

    q.register(job)
    q.complete(job)

    assert q.pending() == [], "completing takes it off the pending list"
    assert q.register(job) is False, "the pair has been analysed here already"
    assert [j.name for j in q.completed()] == [job.name]


def test_a_released_pair_is_eligible_again(tmp_path):
    """Release is for a job that ended badly — a retry must stay possible."""
    q = FileQueue(tmp_path / "queue")
    job = Job(79529, 1, 2, [SIG_A])

    q.register(job)
    q.release(job)

    assert q.register(job) is True
    assert q.completed() == []


def test_completion_records_a_pair_that_was_never_pending(tmp_path):
    """A hand-run pair, or one whose marker was cleaned up mid-run."""
    q = FileQueue(tmp_path / "queue")
    job = Job(79529, 1, 2, [SIG_A])

    q.complete(job)

    assert q.register(job) is False
    assert [j.name for j in q.completed()] == [job.name]


def test_done_records_do_not_look_like_pending_work(tmp_path):
    """`done/` lives inside the queue directory; it must not be drained."""
    q = FileQueue(tmp_path / "queue")
    q.register(Job(79529, 1, 2, [SIG_A]))
    q.complete(Job(79529, 1, 2, [SIG_A]))
    q.register(Job(79529, 2, 3, [SIG_B]))

    assert [j.target_build for j in q.pending()] == [3]
    assert len(q) == 1
