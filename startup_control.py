"""Only controls this user's single approved launch-agent label."""
import asyncio
import os
import signal
from pathlib import Path

from runtime_constants import LABEL

def inhibit_recovery(path):
    path = Path(path)
    with path.open('w') as file:
        file.write('Manual re-enable required.\n')
        file.flush()
        os.fsync(file.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def disable_startup():
    process = await asyncio.create_subprocess_exec(
        '/bin/launchctl', 'disable', f'gui/{os.getuid()}/{LABEL}',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        disabled = await asyncio.wait_for(process.wait(), 10) == 0
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return False
    # disable alone leaves the currently loaded supervisor running. Unload the
    # exact owned job as well, preventing its child restart loop.
    check = await asyncio.create_subprocess_exec(
        '/bin/launchctl', 'print', f'gui/{os.getuid()}/{LABEL}',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    loaded = await check.wait() == 0
    if loaded:
        stop = await asyncio.create_subprocess_exec(
            '/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return disabled and await stop.wait() == 0
    # A supervisor started from the already-authorized desktop session may not
    # belong to launchd yet. Stop only our actual, explicitly identified parent.
    if os.environ.get('XIANYU_SUPERVISOR_PIPE'):
        parent = os.getppid()
        root = Path(__file__).resolve().parent
        inspect = await asyncio.create_subprocess_exec(
            '/bin/ps', '-p', str(parent), '-o', 'command=',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await inspect.communicate()
        command = output.decode(errors='replace')
        if str(root/'supervisor.py') not in command or str(root/'app.py') not in command:
            return False
        try:
            os.kill(parent, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return disabled
