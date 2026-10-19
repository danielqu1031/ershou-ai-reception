import asyncio
from dataclasses import replace
from http.cookies import SimpleCookie
import pytest
import xianyu
from login_session import LoginSession, LoginRequired
from policy import decide
from test_reception import setup_engine


@pytest.mark.parametrize('text', ['能不能便宜点', '能不能便宜一点啊', '可以便宜一点吗', '便宜点', '多少钱啊'])
def test_colloquial_single_questions_have_safe_replies(text):
    d = decide(text, 'unknown')
    assert '100元' in d.reply and not d.handoff
    assert '不能随意保证' not in d.reply


def test_unknown_questions_do_not_silence_conversation():
    async def run():
        e, m, sent = setup_engine()
        await e.receive(replace(m, text='能保温多久'))
        e.last_sent = 0
        await e.receive(replace(m, text='多少钱啊', timestamp=m.timestamp+.001))
        assert len(sent) == 2 and not e.held(m.chat)
        assert '没有实测数据' in sent[0][1]
    asyncio.run(run())


def test_unsent_work_survives_temporary_disconnect():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(text):
            entered.set()
            await release.wait()
            return 'price'
        e, m, sent = setup_engine(model)
        job = asyncio.create_task(e.receive(m))
        await entered.wait()
        e.suspend()
        e.connected = True
        release.set()
        await job
        assert len(sent) == 1 and e.mode == 'auto'
        assert e.snapshot()['events'][0]['status'] == 'sent'
    asyncio.run(run())


def test_reconnect_keeps_auto_but_never_overrides_user_pause(monkeypatch):
    async def run():
        e, _, _ = setup_engine()
        t = xianyu.Xianyu(e)
        t.live_verified = True
        attempts, delays = [], []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        async def token(*args): return 'test'
        async def sleep(delay): delays.append(delay)
        async def connection(*args):
            attempts.append((e.mode, t.live_verified))
            e.connected = True
            if len(attempts) == 1:
                raise ConnectionError()
            if len(attempts) == 2:
                e.pause()
                raise TimeoutError()
            raise LoginRequired()
        monkeypatch.setattr(xianyu.aiohttp, 'ClientSession', lambda **kwargs: Session())
        monkeypatch.setattr(LoginSession, 'token', token)
        monkeypatch.setattr(xianyu.asyncio, 'sleep', sleep)
        t._connection = connection
        await t._run('', {'unb':'123', '_m_h5_tk':'fake_123'})
        assert attempts == [('auto', True), ('auto', True), ('preview', True)]
        assert delays == [2, 4]
        assert not e.connected and not t.live_verified
        assert '验证' in t.status
    asyncio.run(run())


def test_planned_renewal_continues_without_expiry_stop(monkeypatch):
    async def run():
        e, _, _ = setup_engine()
        t = xianyu.Xianyu(e)
        t.live_verified = True
        turns = []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        async def token(*args): return 'test'
        async def connection(*args):
            turns.append(e.mode)
            e.connected = True
            if len(turns) == 4:
                raise asyncio.CancelledError()
        monkeypatch.setattr(xianyu.aiohttp, 'ClientSession', lambda **kwargs: Session())
        monkeypatch.setattr(LoginSession, 'token', token)
        t._connection = connection
        with pytest.raises(asyncio.CancelledError):
            await t._run('', {'unb':'123', '_m_h5_tk':'fake_123'})
        assert turns == ['auto']*4
        assert not e.connected
    asyncio.run(run())


def test_token_renewal_uses_new_cookie_and_never_changes_account():
    async def run():
        login = LoginSession({'unb':'123','_m_h5_tk':'old_123'}, 'device')
        requests = []
        class Response:
            status = 200
            def __init__(self, first):
                self.first = first
                self.cookies = SimpleCookie('_m_h5_tk=new_456')
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def json(self, **kwargs):
                return {'ret':['FAIL_SYS_TOKEN_EXPIRED']} if self.first else {'ret':['SUCCESS'], 'data':{'accessToken':'ok'}}
        class Session:
            def post(self, url, **kwargs):
                requests.append(kwargs)
                return Response(len(requests)==1)
        assert await login.token(Session()) == 'ok'
        assert '_m_h5_tk=new_456' in requests[1]['headers']['Cookie']
        assert requests[0]['params']['sign'] != requests[1]['params']['sign']
        assert requests[0]['allow_redirects'] is False
        with pytest.raises(LoginRequired):
            login.update(SimpleCookie('unb=456'))
        assert login.seller == '123' and login.cookies['unb'] == '123'
    asyncio.run(run())


def test_verified_receipt_is_bound_to_account_and_item():
    e, _, _ = setup_engine()
    assert not e.previously_verified()
    e.remember_verification()
    assert e.previously_verified()
    e.item = '999999'
    assert not e.previously_verified()
    e.item, e.seller = '123456', 'other-owner'
    assert not e.previously_verified()


@pytest.mark.parametrize('code', [401, 403])
def test_registration_denial_requires_login(code):
    async def run():
        e, _, _ = setup_engine()
        t = xianyu.Xianyu(e)
        class Socket:
            async def send_json(self, packet):
                t.pending[packet['headers']['mid']].set_result({'code':code})
        t.ws = Socket()
        with pytest.raises(LoginRequired):
            await t._request('/reg')
        assert not t.pending
    asyncio.run(run())


def test_planned_renewal_preserves_work_and_counts_only_reconnected(monkeypatch):
    async def run():
        e, m, _ = setup_engine()
        t = xianyu.Xianyu(e)
        calls, token_calls = [], []
        entered = asyncio.Event()
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send_json(self, packet): pass
        socket = Socket()
        class Session:
            def ws_connect(self, *args, **kwargs): return socket
        class Login:
            device = 'test-device'
            def headers(self): return {}
            async def available(self, session, item): return True
            async def token(self, session):
                token_calls.append(True)
                return 'renewed'
        async def read(): await entered.wait()
        async def request(lwp, **kwargs):
            calls.append((lwp, kwargs.get('headers', {}).get('token')))
            if lwp == '/!' and t.renewal_count == 1:
                raise asyncio.CancelledError()
        t._read, t._request = read, request
        revision = e.revision
        monkeypatch.setattr(xianyu, 'HEARTBEAT_SECONDS', .001)
        t.renew_requested.set()
        await t._connection(Session(), Login(), 'initial')
        assert not e.connected and t.renewal_pending and t.renewal_count == 0
        with pytest.raises(asyncio.CancelledError):
            await t._connection(Session(), Login(), 'renewed')
        assert ('/reg', 'initial') in calls and ('/reg', 'renewed') in calls
        assert t.ws is socket and t.renewal_count == 1
        assert e.revision == revision and e.mode == 'auto'
    asyncio.run(run())


def test_paid_state_remains_blocked_after_restart(tmp_path):
    from engine import Reception
    e, _, _ = setup_engine()
    path = tmp_path / 'state.sqlite'
    original = Reception(path, e.classify, e.send)
    original.seller, original.item = 'owner', '123456'
    original.stop_trading('sold')
    original.db.close()
    restored = Reception(path, e.classify, e.send)
    restored.seller, restored.item = 'owner', '123456'
    assert restored.trading_blocked()
    restored.item = 'other-item'
    assert not restored.trading_blocked()


def test_replayed_bot_echo_does_not_hold_chat():
    async def run():
        e, m, sent = setup_engine()
        await e.receive(m)
        echo = replace(m, sender=e.seller, text=sent[0][1])
        await e.receive(echo)
        e.suspend()
        e.connected = True
        await e.receive(echo)
        assert not e.held(m.chat)
    asyncio.run(run())


@pytest.mark.parametrize('status,label,sold,owner,expected', [(0,'在线',0,'123',True),(1,'已卖出',1,'123',False),(0,'在线',0,'other',False),(None,None,None,None,False)])
def test_item_sale_state_requires_confirmed_owner_and_online(status,label,sold,owner,expected):
    async def run():
        login = LoginSession({'unb':'123','_m_h5_tk':'fake'}, 'device')
        async def api(*args):
            return {'itemDO':{'itemId':123456,'itemStatus':status,'itemStatusStr':label,'soldCnt':sold,'trackParams':{'sellerId':owner}}}
        login.api = api
        assert await login.available(None, '123456') is expected
    asyncio.run(run())
