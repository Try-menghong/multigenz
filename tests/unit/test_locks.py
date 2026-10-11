import multiprocessing as mp
import time

import pytest

from multigenz.core.locks import RunLock
from multigenz.errors import RunLockedError


def _hold(path, ready):
    with RunLock(path):
        ready.set()
        time.sleep(30)


def test_second_owner_refused_and_released_on_kill(tmp_path):
    p = tmp_path / "run.lock"
    ready = mp.Event()
    proc = mp.Process(target=_hold, args=(p, ready))
    proc.start()
    assert ready.wait(10)
    assert RunLock.is_held(p)
    with pytest.raises(RunLockedError):
        RunLock(p).acquire()
    proc.kill()  # SIGKILL: no cleanup code runs
    proc.join()
    assert not RunLock.is_held(p)
    with RunLock(p):
        pass
