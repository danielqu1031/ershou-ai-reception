"""Generate a disabled, local review artifact only. Never invokes launchctl."""
from pathlib import Path
import plistlib

from runtime_constants import LABEL


def configuration(root):
    root = Path(root).resolve()
    return {
        'Label': LABEL,
        'ProgramArguments': [str(root/'.venv/bin/python'), str(root/'supervisor.py'), str(root/'app.py')],
        'WorkingDirectory': str(root),
        'EnvironmentVariables': {'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
        'Disabled': True,
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'AbandonProcessGroup': False,
        'ThrottleInterval': 30,
        'ExitTimeOut': 10,
        'ProcessType': 'Background',
        'LimitLoadToSessionType': 'Aqua',
        'Umask': 0o077,
        'StandardOutPath': str(root/'data/service.stdout.log'),
        'StandardErrorPath': str(root/'data/service.stderr.log'),
    }


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    target = root
    out = root/(LABEL+'.plist.disabled')
    out.write_bytes(plistlib.dumps(configuration(target)))
    print(out)
