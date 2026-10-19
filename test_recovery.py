import asyncio
import time
from dataclasses import replace
from engine import Reception, Message
import pytest


def test_crash_before_send_can_restore_once_after_authorized_connection(tmp_path):
    async def run():
        entered = asyncio.Event()
        async def slow_model(text):
            entered.set()
            await asyncio.Event().wait()
        sent = []
        async def send(message, text): sent.append((message, text))
        path = tmp_path / 'state.sqlite'
        e = Reception(path, slow_model, send)
        e.item, e.seller, e.connected, e.mode = '123456', 'seller', True, 'auto'
        message = Message(e.item, 'chat', 'buyer', '多少钱', time.time())
        task = asyncio.create_task(e.receive(message))
        await entered.wait()
        # Simulate abrupt process loss before any network send, keeping SQLite.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        e.db.close()
        async def model(text): return 'price'
        restored = Reception(path, model, send)
        restored.item, restored.seller, restored.connected = '123456', 'seller', True
        await restored.recover_pending()
        assert not sent  # Default preview never resumes sales automatically.
        restored.remember_verification()
        restored.mode = 'auto'
        await restored.recover_pending()
        assert not sent  # Restart cannot know whether the seller replied offline.
        await restored.recover_pending(confirmed_chats=('chat',))
        await restored.recover_pending(confirmed_chats=('chat',))
        assert len(sent) == 1 and sent[0][0] == message
        assert restored.snapshot()['events'][0]['status'] == 'sent'
    asyncio.run(run())


@pytest.mark.parametrize('blocker', ['different_owner', 'different_item', 'expired', 'manual_pause', 'sold', 'uncertain'])
def test_recovery_never_overrides_scope_pause_age_or_uncertain_send(tmp_path, blocker):
    async def run():
        entered = asyncio.Event()
        async def model(text):
            entered.set()
            await asyncio.Event().wait()
        calls = []
        async def send(message, text):
            calls.append(text)
            await asyncio.Event().wait()
        path = tmp_path / 'state.sqlite'
        async def fast_model(text): return 'price'
        e = Reception(path, fast_model if blocker == 'uncertain' else model, send)
        e.item, e.seller, e.connected, e.mode = '123456', 'seller', True, 'auto'
        m = Message(e.item, 'chat', 'buyer', '多少钱', time.time())
        task = asyncio.create_task(e.receive(m))
        if blocker == 'uncertain':
            while not calls: await asyncio.sleep(0)
        else:
            await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        e.db.close()
        async def restored_send(message, text): calls.append(text)
        r = Reception(path, fast_model, restored_send)
        r.item, r.seller, r.connected = '123456', 'seller', True
        if blocker == 'different_owner': r.seller = 'other'
        if blocker == 'different_item': r.item = 'other'
        if blocker == 'expired':
            # A stored receive timestamp comes from the durable queue.
            r.db.execute('UPDATE queued_messages SET timestamp=?', (time.time()-121,))
            r.db.commit()
        if blocker == 'manual_pause': r.pause()
        if blocker == 'sold': r.stop_trading('sold')
        r.remember_verification()
        r.mode = 'auto'
        await r.recover_pending(confirmed_chats=('chat',))
        assert len(calls) == (1 if blocker == 'uncertain' else 0)
        if blocker in ('expired', 'manual_pause'):
            assert r.snapshot()['events'][0]['status'] == 'cancelled'
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['socket_close', 'heartbeat_timeout'])
def test_wire_failure_reconnects_and_sends_unsent_reply_once(monkeypatch, failure):
    import json
    from types import SimpleNamespace
    import aiohttp
    import xianyu
    from login_session import LoginSession
    async def run():
        entered, release, reconnected, sent = [asyncio.Event() for _ in range(4)]
        async def model(text):
            entered.set()
            await release.wait()
            return 'price'
        e = Reception(':memory:', model, None)
        e.item, e.seller, e.mode = '123456', 'seller', 'auto'
        transport = xianyu.Xianyu(e)
        e.send = transport.send
        sockets, deliveries, delays = [], [], []
        class Socket:
            def __init__(self): self.queue = asyncio.Queue()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def __aiter__(self): return self
            async def __anext__(self):
                value = await self.queue.get()
                if value is None: raise StopAsyncIteration
                return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(value))
            async def send_json(self, packet):
                lwp = packet.get('lwp')
                if lwp == '/r/SyncStatus/ackDiff':
                    if len(sockets) > 1: reconnected.set()
                    return
                if lwp == '/!' and len(sockets) == 1 and failure == 'heartbeat_timeout': return
                if lwp == '/r/MessageSend/sendByReceiverScope':
                    deliveries.append(packet)
                    sent.set()
                if lwp:
                    await self.queue.put({'code':200,'headers':{'mid':packet['headers']['mid']}})
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def ws_connect(self, *args, **kwargs):
                socket = Socket()
                sockets.append(socket)
                return socket
        async def token(*args): return 'fake-token'
        async def available(*args): return True
        real_sleep = asyncio.sleep
        async def sleep(delay):
            if delay >= 2: delays.append(delay)
            await real_sleep(0)
        monkeypatch.setattr(xianyu.aiohttp, 'ClientSession', lambda **kwargs: Session())
        monkeypatch.setattr(LoginSession, 'token', token)
        monkeypatch.setattr(LoginSession, 'available', available)
        monkeypatch.setattr(xianyu, 'HEARTBEAT_SECONDS', .01)
        monkeypatch.setattr(xianyu, 'REQUEST_TIMEOUT', .02)
        monkeypatch.setattr(xianyu.asyncio, 'sleep', sleep)
        worker = asyncio.create_task(transport._run('', {'unb':'123','_m_h5_tk':'fake'}))
        while not e.connected: await real_sleep(0)
        msg = Message(e.item, 'chat', 'buyer', '多少钱', time.time())
        job = asyncio.create_task(e.receive(msg))
        transport.jobs.add(job)
        job.add_done_callback(transport.jobs.discard)
        await entered.wait()
        if failure == 'socket_close': await sockets[0].queue.put(None)
        await asyncio.wait_for(reconnected.wait(), 1)
        release.set()
        await asyncio.wait_for(job, 1)
        assert len(deliveries) == 1 and len(sockets) == 2
        assert delays == [2] and e.mode == 'auto' and not e.held('chat')
        assert e.snapshot()['events'][0]['status'] == 'sent'
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_retry_backoff_is_capped_and_diagnostics_never_contain_credentials(monkeypatch):
    import json
    import xianyu
    from login_session import LoginSession, LoginRequired
    async def run():
        async def noop(*args): pass
        e = Reception(':memory:', noop, noop)
        t = xianyu.Xianyu(e)
        attempts, delays = [], []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        async def token(*args):
            attempts.append(1)
            if len(attempts) > 7: raise LoginRequired()
            raise RuntimeError('Cookie=TEST_SECRET_MUST_NOT_APPEAR')
        async def wait(delay): delays.append(delay)
        monkeypatch.setattr(xianyu.aiohttp, 'ClientSession', lambda **kwargs:Session())
        monkeypatch.setattr(LoginSession, 'token', token)
        monkeypatch.setattr(xianyu.asyncio, 'sleep', wait)
        await t._run('', {'unb':'123','_m_h5_tk':'fake'})
        assert delays == [2,4,8,16,32,60,60]
        assert t.retry_count == 7 and t.connection_history[-1]['event'] == 'login_required'
        assert 'TEST_SECRET' not in json.dumps(t.connection_history) + t.status
    asyncio.run(run())
