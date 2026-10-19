"""Start only this receptionist and, when absent, the existing local Ollama."""
import os
import shutil
import json
import re
import plistlib
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

from runtime_constants import LABEL
# Emergency stop must work even if an edited config cannot be parsed.
try:
    from settings import PORT, require_configured
except (ValueError, OSError, TypeError, KeyError):
    if sys.argv[1:] != ['stop']:
        raise
    PORT = None

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
URL = f"http://127.0.0.1:{PORT}/" if PORT is not None else ""
AGENT = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
DOMAIN = f'gui/{os.getuid()}'


def launchctl(*args):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True).returncode


def install():
    require_configured()
    DATA.mkdir(mode=0o700, exist_ok=True)
    if sys.platform != "darwin":
        raise SystemExit("macOS required for login startup")
    from prepare_launch_agent import configuration
    config = configuration(ROOT)
    config['Disabled'] = False
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    temporary = AGENT.with_suffix('.plist.tmp')
    temporary.write_bytes(plistlib.dumps(config))
    temporary.chmod(0o600)
    temporary.replace(AGENT)
    if launchctl('enable', f'{DOMAIN}/{LABEL}'):
        raise SystemExit('无法启用接待台登录自启')
    if launchctl('print', f'{DOMAIN}/{LABEL}') != 0:
        if launchctl('bootstrap', DOMAIN, str(AGENT)):
            raise SystemExit('无法载入接待台登录自启')
    print('已安装当前用户登录自启，仅管理这一个接待程序。')


def pause_service():
    with urllib.request.urlopen(URL, timeout=5) as response:
        page = response.read().decode()
    match = re.search("const token='([^']+)'", page)
    if not match:
        raise RuntimeError('本机接待页面无法确认')
    request = urllib.request.Request(URL+'api/mode',data=json.dumps({'mode':'preview'}).encode(),
        headers={'X-Local-Token':match[1], 'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=70) as response:
        if response.status != 200:
            raise RuntimeError('未能保存暂停设置')



def reachable(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_model():
    if not reachable("http://127.0.0.1:11434/api/version"):
        binary = Path(shutil.which("ollama") or "/Applications/Ollama.app/Contents/Resources/ollama")
        if not binary.is_file():
            raise SystemExit("未找到已有本机模型程序")
        env = os.environ.copy()
        env.update(OLLAMA_HOST="127.0.0.1:11434", OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="2", OLLAMA_CONTEXT_LENGTH="8192", OLLAMA_NO_CLOUD="true")
        with (DATA/"ollama.log").open("ab") as log:
            subprocess.Popen([str(binary), "serve"], env=env, cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        for _ in range(30):
            if reachable("http://127.0.0.1:11434/api/version"):
                break
            time.sleep(1)
        else:
            raise SystemExit("本机模型服务启动失败，请查看本机日志")

def start():
    if AGENT.exists():
        install()
        launchctl('kickstart', f'{DOMAIN}/{LABEL}')
    ensure_model()
    if not reachable(URL) and not AGENT.exists():
        with (DATA/"reception.log").open("ab") as log:
            process = subprocess.Popen([str(ROOT/".venv/bin/python"), str(ROOT/"supervisor.py"), str(ROOT/"app.py")], cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
        (DATA/"reception.pid").write_text(str(process.pid))
        for _ in range(20):
            if reachable(URL):
                break
            time.sleep(.5)
        else:
            raise SystemExit("接待台启动失败，请查看本机日志")
    for _ in range(30):
        if reachable(URL):
            break
        time.sleep(1)
    else:
        raise SystemExit("Service not ready: check system permissions and data/service.stderr.log")
    print("接待台已启动："+URL+"（按保存的接待设置恢复）")


def stop():
    from startup_control import inhibit_recovery
    recorded = False
    try:
        inhibit_recovery(DATA/'recovery.disabled')
        recorded = True
    except OSError:
        pass
    disabled = launchctl('disable', f'{DOMAIN}/{LABEL}') == 0
    if reachable(URL):
        try:
            pause_service()
        except Exception:
            pass  # The independent marker/disabled job already prevent recovery.
    if AGENT.exists():
        launchctl('bootout', f'{DOMAIN}/{LABEL}')
    pidfile = DATA/"reception.pid"
    if pidfile.exists():
        pid = int(pidfile.read_text())
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        if str(ROOT/"app.py") in command:
            os.kill(pid, signal.SIGTERM)
        pidfile.unlink(missing_ok=True)
    if not recorded and not disabled:
        raise SystemExit('接待台已停止，但无法持久禁用自动恢复，需要处理')
    print("接待台已停止并禁用自动恢复；共享本机模型服务保持运行。")


if __name__ == "__main__":
    os.umask(0o077)
    DATA.mkdir(mode=0o700, exist_ok=True)
    if len(sys.argv) == 2 and sys.argv[1] == "stop":
        stop()
    elif len(sys.argv) == 2 and sys.argv[1] == "install":
        install()
    else:
        start()
