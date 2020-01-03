import asyncio
from functools import partial
import logging
import pytest
from subprocess import CalledProcessError
import time

from conftest import abort_in, assert_elapsed_time_within
from scheduler import (
    Job,
    ExternalWorkScheduler,
    SignalHandlingScheduler,
)


logger = logging.getLogger('test_job')

# Wildcard used for don't-care values in expected event fields
Whatever = object()


def verify_event(expect, actual):
    expect_keys = set(expect.keys()) - {'ALLOW_CANCEL'}
    assert expect_keys == set(actual.keys())
    keys = sorted(expect_keys)
    for e, a in zip((expect[k] for k in keys), (actual[k] for k in keys)):
        if e is not Whatever:
            assert e == a
    return True


class TJob(Job):
    """A job with test instrumentation."""

    def __init__(
        self,
        name,
        deps=None,
        *,
        result=None,
        before=None,
        async_sleep=0,
        thread_sleep=0,
        subproc_sleep=0,
        thread=None,
        subproc=None,
    ):
        super().__init__(name=name, deps=deps)
        self.result = '{} done'.format(name) if result is None else result
        self.before = set() if before is None else set(before)
        self.async_sleep = async_sleep
        self.thread_sleep = thread_sleep
        self.subproc_sleep = subproc_sleep
        self.thread = thread
        self.subproc = subproc

        self._expect_events = []
        self._add_event('add'),
        self._add_event('start'),

    def _add_event(self, event, *, allow_cancel=False, **kwargs):
        d = {'event': event, 'job': self.name, 'timestamp': Whatever}
        d.update(kwargs)
        if allow_cancel:
            d['ALLOW_CANCEL'] = True
        self._expect_events.append(d)

    def expect_events(self):
        if self._expect_events[-1]['event'] != 'finish':  # we were cancelled
            self._add_event('finish', fate='cancelled')
        return self._expect_events

    async def __call__(self, scheduler):
        if self.deps:
            if self.deps == {'MISSING'}:
                self._add_event('finish', fate='failed')  # expect KeyError
            else:
                self._add_event(
                    'await results', jobs=list(self.deps), pending=Whatever
                )
        dep_results = await super().__call__(scheduler)
        if self.deps:
            self._add_event('awaited results')
        self.logger.debug(f'Results from deps: {dep_results}')

        thread_result, subproc_result = None, None
        if self.async_sleep:
            self.logger.info(f'Async sleep for {self.async_sleep} seconds…')
            await asyncio.sleep(self.async_sleep)
            self.logger.info(f'Finished async sleep')
        if self.thread_sleep:
            self.logger.debug(f'time.sleep({self.thread_sleep}) in thread…')
            self._add_event('await worker slot')
            self._add_event('awaited worker slot', allow_cancel=True)
            self._add_event(
                'await worker thread', allow_cancel=True, func=Whatever
            )
            try:
                thread_result = await scheduler.call_in_thread(
                    partial(time.sleep, self.thread_sleep)
                )
                self._add_event('awaited worker thread', fate='success')
            except asyncio.CancelledError:
                self._add_event(
                    'awaited worker thread',
                    allow_cancel=True,
                    fate='cancelled',
                )
                raise
            self.logger.debug(f'Finished thread sleep: {thread_result}')
        if self.subproc_sleep:
            argv = ['sleep', str(self.subproc_sleep)]
            self.logger.debug(f'sleep {self.subproc_sleep} in subprocess…')
            self._add_event('await worker slot')
            self._add_event('awaited worker slot', allow_cancel=True)
            self._add_event('await worker proc', allow_cancel=True, argv=argv)
            try:
                subproc_result = await scheduler.run_in_subprocess(argv)
                self._add_event('awaited worker proc', exit=0)
            except asyncio.CancelledError:
                self._add_event(
                    'awaited worker proc', allow_cancel=True, exit=-15
                )
                raise
            self.logger.debug(f'Finished subproc sleep: {subproc_result}')
        if self.thread:
            self.logger.debug(f'Await call {self.thread} in thread…')
            self._add_event('await worker slot')
            self._add_event('awaited worker slot', allow_cancel=True)
            self._add_event(
                'await worker thread', allow_cancel=True, func=Whatever
            )
            try:
                thread_result = await scheduler.call_in_thread(self.thread)
                self._add_event('awaited worker thread', fate='success')
            except Exception as e:
                thread_result = e
                self._add_event('awaited worker thread', fate='failed')
            self.logger.debug(f'Finished thread call: {thread_result}')
        if self.subproc:
            self.logger.debug(f'Await run {self.subproc} in subprocess…')
            self._add_event('await worker slot')
            self._add_event('awaited worker slot', allow_cancel=True)
            self._add_event(
                'await worker proc', allow_cancel=True, argv=self.subproc
            )
            try:
                subproc_result = await scheduler.run_in_subprocess(
                    self.subproc
                )
                self._add_event('awaited worker proc', exit=0)
            except Exception as e:
                subproc_result = e
                if isinstance(e, CalledProcessError):
                    self._add_event('awaited worker proc', exit=e.returncode)
            self.logger.debug(f'Finished subprocess run: {subproc_result}')

        for b in self.before:
            assert b in scheduler.tasks  # The other job has been started
            assert not scheduler.tasks[b].done()  # but is not yet finished

        if isinstance(thread_result, Exception):
            self.logger.info(f'Raising thread exception: {thread_result}')
            self._add_event('finish', fate='failed')
            raise thread_result
        elif isinstance(subproc_result, Exception):
            self.logger.info(f'Raising subproc exception: {subproc_result}')
            self._add_event('finish', fate='failed')
            raise subproc_result
        elif isinstance(self.result, Exception):
            self.logger.info(f'Raising exception: {self.result}')
            self._add_event('finish', fate='failed')
            raise self.result
        elif thread_result is not None:
            self.logger.info(f'Returning thread result: {thread_result}')
            self._add_event('finish', fate='success')
            return thread_result
        elif subproc_result is not None:
            self.logger.info(f'Returning subproc result: {subproc_result}')
            self._add_event('finish', fate='success')
            return subproc_result
        else:
            self.logger.info(f'Returning result: {self.result}')
            self._add_event('finish', fate='success')
            return self.result


class TScheduler(SignalHandlingScheduler, ExternalWorkScheduler):
    pass


@pytest.fixture(params=[1, 2, 4, 100])
def scheduler(request):
    logger.info(f'Creating scheduler with {request.param} worker threads')
    yield TScheduler(workers=request.param)


@pytest.fixture
def verify_events(scheduler):
    actual = []
    scheduler.event_handler = actual.append
    before = time.time()

    def _verify_events(todo, done):
        nonlocal actual, before
        after = time.time()
        num_jobs = len(todo)
        expect = {j.name: j.expect_events() for j in todo}

        # Timestamps are in sorted order and all between 'before' and 'after'
        timestamps = [e['timestamp'] for e in actual]
        assert timestamps == sorted(timestamps)
        assert timestamps[0] >= before
        assert timestamps[-1] <= after

        # Jobs are added before execution starts
        expect_adds = [e.pop(0) for e in expect.values()]
        for e, a in zip(expect_adds, actual[:num_jobs]):
            assert verify_event(e, a)
        actual = actual[num_jobs:]

        # Overall execution start and finish
        overall_start, overall_finish = actual.pop(0), actual.pop()
        assert verify_event(
            {
                'event': 'start',
                'num_jobs': num_jobs,
                'keep_going': Whatever,
                'timestamp': Whatever,
            },
            overall_start,
        )
        assert verify_event(
            {
                'event': 'finish',
                'num_tasks': len(done),
                'timestamp': Whatever,
            },
            overall_finish,
        )

        if expect:
            # Jobs are started
            expect_starts = [e.pop(0) for e in expect.values()]
            for e, a in zip(expect_starts, actual[:num_jobs]):
                assert verify_event(e, a)
            actual = actual[num_jobs:]

            # Await task execution
            await_tasks, awaited_tasks = actual.pop(0), actual.pop()
            assert verify_event(
                {
                    'event': 'await tasks',
                    'jobs': [j.name for j in todo],
                    'timestamp': Whatever,
                },
                await_tasks,
            )
            assert verify_event(
                {'event': 'awaited tasks', 'timestamp': Whatever},
                awaited_tasks,
            )

            # Remaining events belong to individual tasks
            while actual:
                a = actual.pop(0)
                job_name = a['job']
                e = expect[job_name].pop(0)
                if a['event'] == 'finish' and a['fate'] == 'cancelled':
                    while e.get('ALLOW_CANCEL', False):
                        e = expect[job_name].pop(0)
                assert verify_event(e, a)

            # No more expected events
            assert all(len(e) == 0 for e in expect.values())

        assert not actual  # no more actual events
        return True

    return _verify_events


@pytest.fixture
def run_jobs(scheduler, verify_events):
    def _run_jobs(todo, abort_after=0, **kwargs):
        for job in todo:
            scheduler.add(job)
        with abort_in(abort_after):
            done = asyncio.run(scheduler.run(**kwargs), debug=True)
        verify_events(todo, done)
        return done

    return _run_jobs


Cancelled = object()


def verify_tasks(tasks, expects):
    errors = 0

    def fail(job_name, expect, actual=None):
        nonlocal errors
        if actual is None:
            logger.error(f'{job_name}: {expect}')
        else:
            logger.error(f'{job_name}: expected {expect!r}, actual {actual!r}')
        errors += 1

    for name in set(tasks.keys()) | set(expects.keys()):
        try:
            expect = expects[name]
            task = tasks[name]
        except KeyError:
            e = 'present' if name in expects else 'missing'
            t = 'present' if name in tasks else 'missing'
            fail(name, f'{e} in expects, {t} in tasks')
            continue
        if expect is Cancelled:
            if not task.cancelled():
                fail(name, Cancelled, task)
        elif isinstance(expect, Exception):
            e = task.exception()
            if not isinstance(e, expect.__class__):
                fail(name, expect.__class__, type(e))
            if e.args != expect.args:
                fail(name, expect.args, e.args)
        else:
            if task.result() != expect:
                fail(name, expect, task.result())
    return errors == 0


def test_zero_jobs_does_nothing(run_jobs):
    assert run_jobs([]) == {}


# simple async jobs, no threading or subprocesses


def test_one_ok_job(run_jobs):
    todo = [TJob('foo')]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': 'foo done'})


def test_one_failed_job(run_jobs):
    done = run_jobs([TJob('foo', result=ValueError('UGH'))])
    assert len(done) == 1
    e = done['foo'].exception()
    assert isinstance(e, ValueError) and e.args == ('UGH',)
    with pytest.raises(ValueError, match='UGH'):
        done['foo'].result()


def test_cannot_add_second_job_with_same_name(run_jobs):
    with pytest.raises(ValueError):
        run_jobs([TJob('foo'), TJob('foo')])


def test_job_with_nonexisting_dependency_raises_KeyError(run_jobs):
    done = run_jobs([TJob('foo', {'MISSING'})])
    with pytest.raises(KeyError, match='MISSING'):
        done['foo'].result()


def test_two_independent_ok_jobs(run_jobs):
    todo = [TJob('foo'), TJob('bar')]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': 'foo done', 'bar': 'bar done'})


def test_one_ok_before_another_ok_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}),
        TJob('bar', {'foo'}),
    ]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': 'foo done', 'bar': 'bar done'})


def test_one_ok_before_one_failed_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}),
        TJob('bar', {'foo'}, result=ValueError('UGH')),
    ]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': 'foo done', 'bar': ValueError('UGH')})


def test_one_failed_job_before_one_ok_job_cancels_second_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}, result=ValueError('UGH')),
        TJob('bar', {'foo'}),
    ]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': ValueError('UGH'), 'bar': Cancelled})


def test_one_failed_job_before_two_ok_jobs_cancels_two_jobs(run_jobs):
    todo = [
        TJob('foo', before={'bar', 'baz'}, result=ValueError('UGH')),
        TJob('bar', {'foo'}),
        TJob('baz', {'foo'}),
    ]
    done = run_jobs(todo)
    assert verify_tasks(
        done, {'foo': ValueError('UGH'), 'bar': Cancelled, 'baz': Cancelled}
    )


def test_one_failed_job_before_two_dependent_jobs_cancels_two_jobs(run_jobs):
    todo = [
        TJob('foo', before={'bar'}, result=ValueError('UGH')),
        TJob('bar', {'foo'}, before={'baz'}),
        TJob('baz', {'bar'}),
    ]
    done = run_jobs(todo)
    assert verify_tasks(
        done, {'foo': ValueError('UGH'), 'bar': Cancelled, 'baz': Cancelled}
    )


def test_one_failed_job_between_two_ok_jobs_cancels_last_job(run_jobs):
    todo = [
        TJob('foo', before={'bar'}),
        TJob('bar', {'foo'}, before={'baz'}, result=ValueError('UGH')),
        TJob('baz', {'bar'}),
    ]
    done = run_jobs(todo)
    assert verify_tasks(
        done, {'foo': 'foo done', 'bar': ValueError('UGH'), 'baz': Cancelled}
    )


def test_one_ok_and_one_failed_job_without_keep_going_cancels_ok_job(run_jobs):
    todo = [
        TJob('foo', result=ValueError('UGH')),
        TJob('bar', async_sleep=0.01),  # allow time for potential cancellation
    ]
    done = run_jobs(todo, keep_going=False)
    assert verify_tasks(done, {'foo': ValueError('UGH'), 'bar': Cancelled})


def test_one_ok_and_one_failed_job_with_keep_going_runs_ok_job(run_jobs):
    todo = [
        TJob('foo', result=ValueError('UGH')),
        TJob('bar', async_sleep=0.01),  # allow time for potential cancellation
    ]
    done = run_jobs(todo, keep_going=True)
    assert verify_tasks(done, {'foo': ValueError('UGH'), 'bar': 'bar done'})


# jobs with work performed in threads and/or subprocesses


def test_one_ok_job_in_thread(run_jobs):
    todo = [TJob('foo', thread=lambda: 'foo worked')]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': 'foo worked'})


def test_one_ok_job_in_subproc(run_jobs, tmp_path):
    path = tmp_path / 'foo'
    todo = [TJob('foo', subproc=['touch', str(path)])]
    done = run_jobs(todo)
    assert verify_tasks(done, {'foo': 0})
    assert path.is_file()


def test_one_failed_between_two_ok_jobs_in_threads_cancels_last(run_jobs):
    def raiseUGH():
        raise ValueError('UGH')

    todo = [
        TJob('foo', before={'bar'}, thread=lambda: 'foo worked'),
        TJob('bar', {'foo'}, before={'baz'}, thread=raiseUGH),
        TJob('baz', {'bar'}, thread=lambda: 'baz worked'),
    ]
    done = run_jobs(todo)
    assert verify_tasks(
        done, {'foo': 'foo worked', 'bar': ValueError('UGH'), 'baz': Cancelled}
    )


def test_one_failed_between_two_in_subprocs_cancels_last(run_jobs, tmp_path):
    foo_path = tmp_path / 'foo'
    baz_path = tmp_path / 'baz'
    todo = [
        TJob('foo', before={'bar'}, subproc=['touch', str(foo_path)]),
        TJob('bar', {'foo'}, before={'baz'}, subproc=['false']),
        TJob('baz', {'bar'}, subproc=['touch', str(baz_path)]),
    ]
    done = run_jobs(todo)
    assert verify_tasks(
        done,
        {'foo': 0, 'bar': CalledProcessError(1, ['false']), 'baz': Cancelled},
    )
    assert foo_path.is_file()
    assert not baz_path.exists()


# aborting jobs shall properly clean up all jobs + scheduler


def test_abort_one_job_returns_immediately(run_jobs):
    todo = [TJob('foo', async_sleep=0.3)]
    with assert_elapsed_time_within(0.2):
        done = run_jobs(todo, abort_after=0.1)
    assert verify_tasks(done, {'foo': Cancelled})


def test_abort_one_job_in_thread_returns_immediately(run_jobs):
    todo = [TJob('foo', thread_sleep=0.3)]
    with assert_elapsed_time_within(0.2):
        done = run_jobs(todo, abort_after=0.1)
    assert verify_tasks(done, {'foo': Cancelled})


def test_abort_one_job_in_subproc_returns_immediately(run_jobs):
    todo = [TJob('foo', subproc_sleep=30)]
    with assert_elapsed_time_within(0.3):
        done = run_jobs(todo, abort_after=0.1)
    assert verify_tasks(done, {'foo': Cancelled})


def test_abort_hundred_jobs_returns_immediately(run_jobs):
    todo = [TJob(f'foo #{i}', async_sleep=0.3) for i in range(100)]
    with assert_elapsed_time_within(0.5):
        done = run_jobs(todo, abort_after=0.1)
    assert verify_tasks(done, {f'foo #{i}': Cancelled for i in range(100)})


def test_abort_hundred_jobs_in_threads_returns_immediately(run_jobs):
    todo = [TJob(f'foo #{i}', thread_sleep=0.3) for i in range(100)]
    with assert_elapsed_time_within(0.5):
        done = run_jobs(todo, abort_after=0.1)
    assert verify_tasks(done, {f'foo #{i}': Cancelled for i in range(100)})


def test_abort_hundred_jobs_in_subprocs_returns_immediately(run_jobs):
    todo = [TJob(f'foo #{i}', subproc_sleep=30) for i in range(100)]
    with assert_elapsed_time_within(2.0):
        done = run_jobs(todo, abort_after=0.1)
    assert verify_tasks(done, {f'foo #{i}': Cancelled for i in range(100)})
