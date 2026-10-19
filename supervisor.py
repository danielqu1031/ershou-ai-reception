"""Supervise only the local receptionist child process.

No credentials in this process, its arguments, environment, or files. Restarting
the child alone does NOT restore a login; durable credential consent is separate.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def supervise(command, stop, *, cwd, wait=None, poll_interval=.2):
    wait = wait or stop.wait
    failures = 0
    while not stop.is_set():
        began = time.monotonic()
        read_fd, write_fd = os.pipe()
        env = dict(os.environ, XIANYU_SUPERVISOR_PIPE=str(read_fd))
        try:
            if os.name == 'nt':
                import msvcrt
                handle = msvcrt.get_osfhandle(read_fd)
                os.set_handle_inheritable(handle, True)
                env['XIANYU_SUPERVISOR_PIPE'] = str(handle)
                info = subprocess.STARTUPINFO()
                info.lpAttributeList = {'handle_list': [handle]}
                child = subprocess.Popen(command, cwd=cwd, env=env, startupinfo=info, close_fds=True)
            else:
                child = subprocess.Popen(command, cwd=cwd, env=env, pass_fds=(read_fd,))
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        try:
            while child.poll() is None and not stop.wait(poll_interval):
                pass
            if stop.is_set():
                return
            failures = 1 if time.monotonic()-began >= 60 else failures+1
            delay = min(60, 2 ** min(failures, 6))
            # Log only process result, never configuration or exception bodies.
            print(f'reception exited ({child.returncode}); retry in {delay}s', flush=True)
            wait(delay)
        finally:
            # EOF also reaches the child if this supervisor dies abruptly.
            os.close(write_fd)
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def main():
    root = Path(__file__).resolve().parent
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # The explicit app argument also lets manage.py identify this owned process.
    expected = str(root / 'app.py')
    if len(sys.argv) != 2 or sys.argv[1] != expected:
        raise SystemExit('Use the prepared launch-agent configuration.')
    data = root / 'data'
    data.mkdir(mode=0o700, exist_ok=True)
    instance_fd = None
    if os.name == 'nt':
        from windows_control import acquire_instance, STOP
        instance_fd = acquire_instance()
        if instance_fd is None:
            return
        if STOP.exists():
            os.close(instance_fd)
            return
        def check_stop():
            while not stop.wait(.2):
                if STOP.exists():
                    stop.set()
        threading.Thread(target=check_stop, daemon=True).start()
    from manage import ensure_model
    ensure_model()
    (data / 'reception.pid').write_text(str(os.getpid()))
    try:
        supervise([sys.executable, expected], stop, cwd=root)
    finally:
        if instance_fd is not None:
            os.close(instance_fd)


if __name__ == '__main__':
    main()
