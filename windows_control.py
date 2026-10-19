"""Per-user Windows startup; stopping never kills a PID read from disk."""
import os
from pathlib import Path
import subprocess
import sys
import time
from runtime_constants import LABEL

ROOT = Path(__file__).resolve().parent
DATA = ROOT/'data'
STOP = DATA/'stop.request'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'

def acquire_instance():
    from file_lock import lock
    DATA.mkdir(parents=True, exist_ok=True)
    fd = os.open(DATA/'supervisor.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lock(fd)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise

def command():
    python = ROOT/'.venv/Scripts/pythonw.exe'
    if not python.is_file():
        raise RuntimeError('请先运行 install-windows.bat')
    return subprocess.list2cmdline([str(python), str(ROOT/'manage.py'), 'start'])

def install():
    import winreg
    from settings import require_configured
    require_configured()
    value = command()
    if len(value) > 260:
        raise RuntimeError('项目路径太长，请移到较短的本机目录')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        try:
            old, _ = winreg.QueryValueEx(key, LABEL)
        except FileNotFoundError:
            old = value
        if old != value:
            raise RuntimeError('已有另一份接待台自启，请先从原目录停用')
        winreg.SetValueEx(key, LABEL, 0, winreg.REG_SZ, value)
    print('已设置当前用户登录后启动。')

def disable():
    import winreg
    DATA.mkdir(parents=True, exist_ok=True)
    STOP.touch()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            try:
                old, _ = winreg.QueryValueEx(key, LABEL)
            except FileNotFoundError:
                return True
            if old != command():
                return False
            winreg.DeleteValue(key, LABEL)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False

def start():
    from manage import ensure_model, reachable, URL
    from settings import require_configured
    require_configured()
    DATA.mkdir(parents=True, exist_ok=True)
    if reachable(URL):
        print('接待台已运行：'+URL)
        return
    guard = acquire_instance()
    if guard is None:
        raise RuntimeError('接待台正在启动或停止，请稍后重试')
    try:
        STOP.unlink(missing_ok=True)
        ensure_model()
        os.close(guard)
        guard = None
        python = ROOT/'.venv/Scripts/python.exe'
        with (DATA/'reception.log').open('ab') as log:
            subprocess.Popen([str(python), str(ROOT/'supervisor.py'), str(ROOT/'app.py')], cwd=ROOT,
                             stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        if guard is not None:
            os.close(guard)
    for _ in range(60):
        if reachable(URL):
            print('接待台已启动：'+URL)
            return
        time.sleep(.5)
    raise RuntimeError('启动失败，请检查 data/reception.log')

def stop():
    from startup_control import inhibit_recovery
    from manage import reachable, pause_service, URL
    DATA.mkdir(parents=True, exist_ok=True)
    inhibit_recovery(DATA/'recovery.disabled')
    disabled = disable()
    if reachable(URL):
        try:
            pause_service()
        except Exception:
            pass
    for _ in range(50):
        guard = acquire_instance()
        if guard is not None:
            os.close(guard)
            if not disabled:
                raise RuntimeError('后台已停止，但自启设置未能移除')
            if reachable(URL):
                raise RuntimeError('后台已停止；另有前台接待程序，请在其窗口按 Ctrl+C 关闭')
            print('接待台已停止，并禁用自动恢复和登录自启。')
            return
        time.sleep(.2)
    raise RuntimeError('停止尚未完成，请查看日志；自动恢复已禁用')
