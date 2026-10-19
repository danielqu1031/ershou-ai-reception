import json
from pathlib import Path
import sys
import threading
from supervisor import supervise


def test_real_disposable_child_crash_restarts_without_production_access(tmp_path):
    # Both child processes only write to pytest's temporary directory.
    child = tmp_path / 'child.py'
    child.write_text("""from pathlib import Path
import os, time
p=Path('starts.txt')
old=p.read_text() if p.exists() else ''
p.write_text(old+str(os.getpid())+'\\n')
if not old: raise SystemExit(7)
Path('ready').touch()
time.sleep(10)
""")
    stop = threading.Event()
    delays = []
    def wait(delay): delays.append(delay)
    worker = threading.Thread(target=supervise, args=([sys.executable,str(child)],stop), kwargs={'cwd':tmp_path,'wait':wait,'poll_interval':.01})
    worker.start()
    try:
        import time
        deadline = time.monotonic()+3
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'ready').exists()
        ids = (tmp_path/'starts.txt').read_text().splitlines()
        assert len(ids) == 2 and len(set(ids)) == 2 and delays == [2]
    finally:
        stop.set()
        worker.join(6)
    assert not worker.is_alive()


def test_prepared_login_agent_is_disabled_and_has_no_credentials(tmp_path):
    from prepare_launch_agent import configuration
    config = configuration(tmp_path)
    assert config['Disabled'] is True
    assert config['RunAtLoad'] is True
    assert config['KeepAlive'] == {'SuccessfulExit':False}
    assert config['Umask'] == 0o077
    assert set(config.get('EnvironmentVariables', {})) == {'PATH'}
    assert config['EnvironmentVariables']['PATH'] == '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'
    assert config['ProgramArguments'][-1] == str(tmp_path/'app.py')


def test_supervisor_sigkill_makes_owned_child_exit(tmp_path):
    import os
    import subprocess
    import signal
    import time
    source = Path(__file__).resolve().parent
    child = tmp_path/'watched_child.py'
    child.write_text("""import asyncio, os
from pathlib import Path
from parent_watch import watch_parent
Path('child.pid').write_text(str(os.getpid()))
asyncio.run(watch_parent(int(os.environ['XIANYU_SUPERVISOR_PIPE']), lambda: Path('parent_lost').touch()))
""")
    env = dict(os.environ, PYTHONPATH=str(source))
    code = 'import sys,threading; from supervisor import supervise; supervise([sys.executable,sys.argv[1]],threading.Event(),cwd=sys.argv[2])'
    parent = subprocess.Popen([sys.executable,'-c',code,str(child),str(tmp_path)], env=env, start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic()+3
        while not (tmp_path/'child.pid').exists() and time.monotonic()<deadline: time.sleep(.01)
        child_pid = int((tmp_path/'child.pid').read_text())
        assert os.getpgid(child_pid) == parent.pid
        parent.kill()  # Only the disposable supervisor created by this test.
        parent.wait(timeout=3)
        deadline = time.monotonic()+3
        while not (tmp_path/'parent_lost').exists() and time.monotonic()<deadline: time.sleep(.01)
        assert (tmp_path/'parent_lost').exists()
        while time.monotonic()<deadline:
            try: os.kill(child_pid,0)
            except ProcessLookupError: break
            time.sleep(.01)
        else: raise AssertionError('Orphaned child did not exit')
    finally:
        if parent.poll() is None:
            parent.terminate()
            parent.wait(timeout=6)
        if child_pid:
            try: os.kill(child_pid,signal.SIGTERM)
            except ProcessLookupError: pass
