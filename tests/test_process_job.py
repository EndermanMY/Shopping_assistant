import subprocess
import sys

import pytest

from assistant.process_job import BrowserJob


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process ownership')
def test_closing_job_terminates_only_owned_process():
    child = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'], creationflags=subprocess.CREATE_NO_WINDOW)
    unrelated = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job = BrowserJob(child.pid)
        job.close()
        child.wait(timeout=5)
        assert unrelated.poll() is None
    finally:
        for process in (child,unrelated):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
