import asyncio
import pytest
from engine import Reception
from saved_session import ChoiceStore, SavedSession, SessionError, SERVICE
from recovery_control import RecoveryControl
from keychain_vault import APPROVED_SCOPE
from test_saved_session import FakeVault

COOKIE = 'unb=111111111; _m_h5_tk=FAKE_TEST_ONLY'

class Transport:
    def __init__(self, engine):
        self.engine, self.live_verified = engine, True
        self.calls = 0
    async def connect(self, cookie, item):
        assert cookie == COOKIE
        self.calls += 1
        self.engine.item, self.engine.seller = item, APPROVED_SCOPE.seller
        self.engine.connected = True
    async def stop(self):
        self.engine.pause(preserve_recovery=True)
        self.engine.connected = False

def setup(tmp_path):
    async def model(text): return 'unknown'
    async def send(*args): raise AssertionError('Recovery must not send old replies')
    engine = Reception(tmp_path/'messages.sqlite',model,send)
    transport = Transport(engine)
    store = ChoiceStore(tmp_path/'choices.sqlite')
    saved = SavedSession(store,FakeVault())
    disabled = []
    async def disable():
        disabled.append(True)
        return True
    control = RecoveryControl(saved,engine,transport,disable)
    return control, saved, engine, transport, disabled

def test_restart_restores_auto_but_pause_survives_restart(tmp_path):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        e.remember_verification()
        assert e.mode == 'preview'
        await c.enable()
        assert e.mode == 'auto'
        e.pause()
        restarted = RecoveryControl(s,e,t,c.disable)
        await restarted.restore()
        await restarted.connected()
        assert e.mode == 'auto' and t.calls == 2
        await restarted.pause()
        again = RecoveryControl(s,e,t,c.disable)
        await again.restore()
        assert e.mode == 'preview' and t.calls == 2
    asyncio.run(run())

def test_unverified_or_uncertain_restart_stays_paused(tmp_path):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        await c.enable()
        e.hold('test','uncertain')
        e.pause()
        await c.restore()
        await c.connected()
        assert e.mode == 'preview' and not s.choices.get(APPROVED_SCOPE).auto
        assert await s.restore_candidate(APPROVED_SCOPE) is None
    asyncio.run(run())

def test_pause_read_failure_disables_startup(tmp_path,monkeypatch):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        await c.enable()
        def fail(*args): raise SessionError('CHOICE_READ_FAILED')
        monkeypatch.setattr(s.choices,'get',fail)
        with pytest.raises(SessionError):
            await c.pause()
        assert d and e.mode == 'preview'
        # Existing retry task and a fresh child cannot use the stale ready row.
        await c.restore()
        assert e.mode == 'preview'
        restarted = RecoveryControl(s,e,t,c.disable)
        await restarted.restore()
        assert e.mode == 'preview' and t.calls == 1
    asyncio.run(run())

def test_pause_double_storage_failure_survives_new_process(tmp_path,monkeypatch):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        await c.enable()
        s.vault.fail.add('delete')
        original_put = s.choices.put
        def fail(*args): raise SessionError('CHOICE_WRITE_FAILED')
        monkeypatch.setattr(s.choices,'put',fail)
        with pytest.raises(SessionError):
            await c.pause()
        assert s.choices.get(APPROVED_SCOPE).auto  # Original hazardous state.
        monkeypatch.setattr(s.choices,'put',original_put)
        await c.restore()
        restarted = RecoveryControl(s,e,t,c.disable)
        await restarted.restore()
        assert e.mode == 'preview' and t.calls == 1
        assert c.inhibit_path.exists()
    asyncio.run(run())

def test_pause_while_restore_connection_pending_cannot_reenable(tmp_path):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        await c.enable()
        await c.restore()
        await c.pause()
        await c.connected()
        assert e.mode == 'preview' and not s.choices.get(APPROVED_SCOPE).auto
    asyncio.run(run())

def test_scope_and_revoke(tmp_path):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        with pytest.raises(ValueError):
            await c.connect(COOKIE,'999999',remember=True)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        await c.enable()
        await c.revoke()
        assert d and not e.connected and e.mode == 'preview'
        assert not s.vault.values and await s.restore_candidate(APPROVED_SCOPE) is None
        assert s.choices.get(APPROVED_SCOPE).state == 'revoked'
    asyncio.run(run())

def test_sold_removes_login_and_auth_failure_preserves_paused_login(tmp_path):
    async def run():
        c,s,e,t,d = setup(tmp_path)
        await c.connect(COOKIE,APPROVED_SCOPE.item,remember=True)
        await c.enable()
        await c.safety_stop('login_required')
        assert s.choices.get(APPROVED_SCOPE).state == 'paused'
        assert s.vault.values and e.mode == 'preview'
        await c.safety_stop('sold')
        assert not s.vault.values
        assert s.choices.get(APPROVED_SCOPE).state == 'sold'
    asyncio.run(run())
