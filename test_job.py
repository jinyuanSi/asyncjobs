import asyncio
import concurrent.futures
from contextlib import contextmanager
import logging
import os
import pytest
import signal
import time

from scheduler import JobWithDeps, JobInWorker, Scheduler


logger = logging.getLogger('test_job')


class TJob(JobWithDeps):
    """A job with test instrumentation."""

    def __init__(self, name, deps=None, *, result=None, before=None, asleep=0):
        self.result = '{} done'.format(name) if result is None else result
        self.before = set() if before is None else before
        self.asleep = asleep
        super().__init__(name=name, deps=deps or set())

    async def __call__(self, scheduler):
        await super().__call__(scheduler)
        if self.asleep:
            await asyncio.sleep(self.asleep)
        for b in self.before:
            assert b in scheduler.tasks  # The other job has been started
            assert not scheduler.tasks[b].done()  # but is not yet finished
        if isinstance(self.result, Exception):
            raise self.result
        else:
            return self.result


class TWorkerJob(JobWithDeps, JobInWorker):
    """A job done in a worker, with test instrumentation."""

    def __init__(self, name, deps=None, *, result=None, before=None, sleep=0):
        self.result = '{} worked'.format(name) if result is None else result
        self.before = set() if before is None else before
        self.sleep = sleep
        super().__init__(name=name, deps=deps or set())

    async def __call__(self, scheduler):
        result = await super().__call__(scheduler)
        for b in self.before:
            assert b in scheduler.tasks  # The other job has been started
            assert not scheduler.tasks[b].done()  # but is not yet finished
        return result

    def do_work(self):
        if self.sleep:
            time.sleep(self.sleep)
        if isinstance(self.result, Exception):
            raise self.result
        else:
            return self.result


@pytest.fixture(params=[0, 1, 2, 4, -1, -2, -4])
def scheduler(request):
    if request.param == 0:  # run everything syncronously
        logger.info('Creating single-threaded/syncronous scheduler')
        workers = None
    elif request.param > 0:  # number of worker threads
        logger.info(f'Creating scheduler with {request.param} worker threads')
        workers = concurrent.futures.ThreadPoolExecutor(request.param)
    else:  # number of worker processes
        logger.info(f'Creating scheduler with {-request.param} worker procs')
        workers = concurrent.futures.ProcessPoolExecutor(-request.param)
    return Scheduler(workers)


@contextmanager
def abort_in(when=0):
    def handle_SIGALRM(signal_number, stack_frame):
        logger.warning('Raising SIGINT to simulate Ctrl+C...')
        os.kill(os.getpid(), signal.SIGINT)

    prev_handler = signal.signal(signal.SIGALRM, handle_SIGALRM)
    signal.setitimer(signal.ITIMER_REAL, when)
    try:
        yield
    except KeyboardInterrupt:
        logger.error('SIGINT/KeyboardInterrupt escaped the context!')
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev_handler)


@pytest.fixture
def run_jobs(scheduler):
    def _run_jobs(jobs, abort_after=0, **kwargs):
        for job in jobs:
            scheduler.add(job)
        with abort_in(abort_after):
            return asyncio.run(scheduler.run(**kwargs), debug=True)

    return _run_jobs


def assert_all_ok(tasks, jobs):
    assert len(jobs) == len(tasks)
    for job in jobs:
        assert tasks[job.name].result() == job.result


Cancelled = object()


def assert_tasks(tasks, expects):
    for job_name in tasks.keys() & expects.keys():
        expect = expects[job_name]
        task = tasks[job_name]
        if expect is Cancelled:
            assert task.cancelled()
        elif isinstance(expect, Exception):
            e = task.exception()
            assert isinstance(e, expect.__class__)
            assert e.args == expect.args
        else:
            assert task.result() == expect


def test_zero_jobs_does_nothing(run_jobs):
    done = run_jobs([])
    assert done == {}


def test_one_ok_job(run_jobs):
    todo = [TJob('foo')]
    done = run_jobs(todo)
    assert_all_ok(done, todo)


def test_two_independent_ok_jobs(run_jobs):
    todo = [TJob('foo'), TJob('bar')]
    done = run_jobs(todo)
    assert_all_ok(done, todo)


def test_two_dependent_ok_jobs(run_jobs):
    todo = [
        TJob('foo', before={'bar'}),
        TJob('bar', {'foo'}),
    ]
    done = run_jobs(todo)
    assert_all_ok(done, todo)


def test_cannot_add_second_job_with_same_name(run_jobs):
    with pytest.raises(ValueError):
        run_jobs([TJob('foo'), TJob('foo')])


def test_job_with_nonexisting_dependency_raises_KeyError(run_jobs):
    done = run_jobs([TJob('foo', {'MISSING'})])
    with pytest.raises(KeyError, match='MISSING'):
        done['foo'].result()


def test_one_failed_job(run_jobs):
    done = run_jobs([TJob('foo', result=ValueError('UGH'))])
    assert len(done) == 1
    e = done['foo'].exception()
    assert isinstance(e, ValueError) and e.args == ('UGH',)
    with pytest.raises(ValueError, match='UGH'):
        done['foo'].result()


def test_one_ok_before_one_failed_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}),
        TJob('bar', {'foo'}, result=ValueError('UGH')),
    ]
    done = run_jobs(todo)
    assert_tasks(done, {'foo': 'foo done', 'bar': ValueError('UGH')})


def test_one_failed_job_before_one_ok_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}, result=ValueError('UGH')),
        TJob('bar', {'foo'}),
    ]
    done = run_jobs(todo)
    assert_tasks(done, {'foo': ValueError('UGH'), 'bar': Cancelled})


def test_one_failed_job_before_two_ok_jobs(run_jobs):
    todo = [
        TJob('foo', before={'bar', 'baz'}, result=ValueError('UGH')),
        TJob('bar', {'foo'}),
        TJob('baz', {'foo'}),
    ]
    done = run_jobs(todo)
    assert_tasks(
        done, {'foo': ValueError('UGH'), 'bar': Cancelled, 'baz': Cancelled}
    )


def test_one_failed_job_before_one_ok_job_before_one_ok_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}, result=ValueError('UGH')),
        TJob('bar', {'foo'}, before={'baz'}),
        TJob('baz', {'bar'}),
    ]
    done = run_jobs(todo)
    assert_tasks(
        done, {'foo': ValueError('UGH'), 'bar': Cancelled, 'baz': Cancelled}
    )


def test_one_failed_job_between_two_ok_jobs(run_jobs):
    todo = [
        TJob('foo', before={'bar'}),
        TJob('bar', {'foo'}, before={'baz'}, result=ValueError('UGH')),
        TJob('baz', {'bar'}),
    ]
    done = run_jobs(todo)
    assert_tasks(
        done, {'foo': 'foo done', 'bar': ValueError('UGH'), 'baz': Cancelled}
    )


def test_one_ok_and_one_failed_job_without_keep_going(run_jobs):
    todo = [
        TJob('foo', result=ValueError('UGH')),
        TJob('bar', asleep=0.01),
    ]
    done = run_jobs(todo, keep_going=False)
    assert_tasks(done, {'foo': ValueError('UGH'), 'bar': Cancelled})


def test_one_ok_and_one_failed_job_with_keep_going(run_jobs):
    todo = [
        TJob('foo', result=ValueError('UGH')),
        TJob('bar', asleep=0.01),
    ]
    done = run_jobs(todo, keep_going=True)
    assert_tasks(done, {'foo': ValueError('UGH'), 'bar': 'bar done'})


def test_one_ok_workerjob(run_jobs):
    todo = [TWorkerJob('foo')]
    done = run_jobs(todo)
    assert_tasks(done, {'foo': 'foo worked'})


def test_one_failed_workerjob_between_two_ok_workerjobs(run_jobs):
    todo = [
        TWorkerJob('foo', before={'bar'}),
        TWorkerJob('bar', {'foo'}, before={'baz'}, result=ValueError('UGH')),
        TWorkerJob('baz', {'bar'}),
    ]
    done = run_jobs(todo)
    assert_tasks(
        done, {'foo': 'foo worked', 'bar': ValueError('UGH'), 'baz': Cancelled}
    )


# TODO:
# - test timeout
# - test redirected and prefixed output from workers
# - test build stats
