"""A supervised child exits if its parent disappears, including SIGKILL."""
import asyncio
import os
import signal
import stat


async def watch_parent(fd, on_lost=None):
    if os.name == 'nt':
        import msvcrt
        fd = msvcrt.open_osfhandle(fd, os.O_RDONLY | os.O_BINARY)
        # A daemon reader avoids blocking asyncio shutdown while the parent lives.
        import threading
        loop = asyncio.get_running_loop()
        gone = asyncio.Event()
        def read_pipe():
            try:
                os.read(fd, 1)
            finally:
                os.close(fd)
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(gone.set)
                    except RuntimeError:
                        pass
        threading.Thread(target=read_pipe, daemon=True).start()
        await gone.wait()
        (on_lost or (lambda: os.kill(os.getpid(), signal.SIGTERM)))()
        return
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
