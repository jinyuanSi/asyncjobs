import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
import logging
import subprocess

from .scheduler import Scheduler
from .util import current_task_name, fate

logger = logging.getLogger(__name__)


class ExternalWorkScheduler(Scheduler):
    """Manage jobs whose work is done in threads or subprocesses.

    Extend Scheduler with methods that allow Job instances to perform work in
    other threads or subprocess, while keeping the number of _concurrent_
    threads/processes within the given limit.
    """

    def __init__(self, *, workers=1, **kwargs):
        assert workers > 0
        self.workers = workers
        self.worker_sem = None
        self.worker_threads = None
        super().__init__(**kwargs)

    @asynccontextmanager
    async def reserve_worker(self, caller=None):
        """Acquire a worker context where the caller can run its own work.

        This is the mechanism that ensures we do not schedule more concurrent
        work than allowed by .workers. Anybody that wants to spin off another
        thread or process to perform some work should use this context manager
        to await a free "slot".
        """
        assert self.running
        logger.debug('acquiring worker semaphore…')
        self.event('await worker slot', caller)
        async with self.worker_sem:
            self.event('awaited worker slot', caller)
            logger.debug('acquired worker semaphore')
            yield
            logger.debug('releasing worker semaphore')

    async def call_in_thread(self, func, *args):
        """Call func(*args) in a worker thread and await its result."""
        caller = current_task_name()
        async with self.reserve_worker(caller):
            if self.worker_threads is None:
                self.worker_threads = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.workers,
                    thread_name_prefix='Worker Thread',
                )
            try:
                logger.debug(f'{caller} -> awaiting worker thread…')
                self.event('await worker thread', caller, {'func': str(func)})
                future = asyncio.get_running_loop().run_in_executor(
                    self.worker_threads, func, *args
                )
                future.add_done_callback(
                    lambda f: self.event(
                        'awaited worker thread', caller, {'fate': fate(f)}
                    )
                )
                result = await future
                logger.debug(f'{caller} <- {result} from worker')
                return result
            except Exception as e:
                logger.warning(f'{caller} <- Exception {e} from worker!')
                raise

    async def run_in_subprocess(self, argv, check=True):
        """Run a command line in a subprocess and await its exit code."""
        caller = current_task_name()
        retcode = None
        async with self.reserve_worker(caller):
            logger.debug(f'{caller} -> starting {argv} in subprocess…')
            self.event('await worker proc', caller, {'argv': argv})
            proc = await asyncio.create_subprocess_exec(*argv)
            try:
                logger.debug(f'{caller} -- awaiting subprocess…')
                retcode = await proc.wait()
            except asyncio.CancelledError:
                logger.error(f'{caller} cancelled! Terminating {proc}!…')
                proc.terminate()
                try:
                    retcode = await proc.wait()
                    logger.debug(f'{caller} cancelled! {proc} terminated.')
                except asyncio.CancelledError:
                    logger.error(f'{caller} cancelled again! Killing {proc}!…')
                    proc.kill()
                    retcode = await proc.wait()
                    logger.debug(f'{caller} cancelled! {proc} killed.')
                raise
            finally:
                self.event('awaited worker proc', caller, {'exit': retcode})
        if check and retcode != 0:
            raise subprocess.CalledProcessError(retcode, argv)
        return retcode

    async def _run_tasks(self, *args, **kwargs):
        self.worker_sem = asyncio.BoundedSemaphore(self.workers)

        try:
            await super()._run_tasks(*args, **kwargs)
            if self.worker_threads is not None:
                logger.debug('Shutting down worker threads…')
                self.worker_threads.shutdown()  # wait=timeout is None)
                self.worker_threads = None
                logger.debug('Shut down worker threads')
        finally:
            if self.worker_threads is not None:
                logger.error('Cancelling without awaiting workers…')
                self.worker_threads.shutdown(wait=False)
                self.worker_threads = None