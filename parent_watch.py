"""A supervised child exits if its parent disappears, including SIGKILL."""
import asyncio
import os
import signal
import stat


async def watch_parent(fd, on_lost=None):
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise ValueError('Supervisor descriptor is not a pipe')
    loop = asyncio.get_running_loop()
    gone = asyncio.Event()
    def readable():
        if not os.read(fd, 1):
            loop.remove_reader(fd)
            gone.set()
    loop.add_reader(fd, readable)
    try:
        await gone.wait()
        (on_lost or (lambda: os.kill(os.getpid(), signal.SIGTERM)))()
    finally:
        loop.remove_reader(fd)
        os.close(fd)
