from functools import wraps
from shutil import copyfileobj
import sys

from luigi.interface import _WorkerSchedulerFactory
from luigi.rpc import RemoteScheduler
from luigi.scheduler import CentralPlannerScheduler, SimpleTaskState
from luigi.s3 import S3Target

__all__ = [
    'FireflowerStateManager',
    'FireflowerWorkerSchedulerFactory',
    'FireflowerCentralPlannerScheduler',
]


class FireflowerStateManager(object):
    """
    Fireflower expects a SqlAlchemy session and a sentry object to be
    registered when the luigi process is booted up. See signals.pipeline.luigid
    for example.
    """
    session = None
    sentry = None

    @classmethod
    def register_sqlalchemy_session(cls, session):
        cls.session = session

    @classmethod
    def register_sentry(cls, sentry):
        cls.sentry = sentry


def luigi_run_with_sentry(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception:
            type_, value, traceback_ = sys.exc_info()
            if FireflowerStateManager.sentry.client:
                extra = {
                    'task_args': self.param_args,
                    'task_kwargs': self.param_kwargs,
                }
                FireflowerStateManager.sentry.captureException(extra=extra)
            raise type_(value).with_traceback(traceback_)
        finally:
            FireflowerStateManager.sentry.client.context.clear()
    return wrapper


class FireflowerWorkerSchedulerFactory(_WorkerSchedulerFactory):
    def __init__(self, remote_host='', remote_port='', s3_state_path=''):
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._s3_state_path = s3_state_path

    def create_local_scheduler(self):
        from fireflower.models import FireflowerTaskHistory
        task_history = FireflowerTaskHistory()
        if self._s3_state_path:
            state = S3TaskState('state.pkl', self._s3_state_path)
        else:
            state = None
        return FireflowerCentralPlannerScheduler(prune_on_get_work=True,
                                                 task_history_impl=task_history,
                                                 state=state)

    def create_remote_scheduler(self, url=None):
        url = url or 'http://{}:{}'.format(self._remote_host, self._remote_port)
        return RemoteScheduler(url)


class FireflowerCentralPlannerScheduler(CentralPlannerScheduler):
    def __init__(self, *args, **kwargs):
        state = kwargs.pop('state') if 'state' in kwargs else None
        super(FireflowerCentralPlannerScheduler, self).__init__(*args, **kwargs)
        if state is not None:
            self._state = state


class S3TaskState(SimpleTaskState):
    def __init__(self, local_path, s3_path):
        super(S3TaskState, self).__init__(local_path)
        self._s3_file = S3Target(s3_path)

    def dump(self):
        super(S3TaskState, self).dump()
        with open(self._state_path, 'r') as f_src, \
                self._s3_file.open('w') as f_dst:
            copyfileobj(f_src, f_dst)

    def load(self):
        if self._s3_file.exists():
            with self._s3_file.open('r') as f_src, \
                    open(self._state_path, 'w') as f_dst:
                copyfileobj(f_src, f_dst)
        super(S3TaskState, self).load()
