import asyncio
from dataclasses import replace
import pytest
from saved_session import ChoiceStore, Scope, Credential, SavedSession, SessionError, SERVICE

SCOPE = Scope('123', '123456')
COOKIE = 'unb=123; _m_h5_tk=TEST_FAKE_SECRET_123'


class FakeVault:
    def __init__(self):
        self.values, self.calls, self.fail = {}, [], set()
    async def get(self, service, account):
        self.calls.append(('get',service,account))
        if 'get' in self.fail: raise RuntimeError(COOKIE)
        return self.values.get((service,account))
    async def put(self, service, account, value):
        self.calls.append(('put',service,account))
        if 'put' in self.fail: raise RuntimeError(COOKIE)
        self.values[service,account] = value
    async def delete(self, service, account):
        self.calls.append(('delete',service,account))
        if 'delete' in self.fail: raise RuntimeError(COOKIE)
        self.values.pop((service,account),None)


def test_pause_survives_restart_and_never_reads_secret_when_paused(tmp_path):
    async def run():
        path = tmp_path/'choices.sqlite'
        vault = FakeVault()
        store = ChoiceStore(path)
        service = SavedSession(store,vault)
        with pytest.raises(SessionError, match='EXPLICIT_CONSENT_REQUIRED'):
            await service.save(SCOPE,COOKIE)
        assert not vault.calls and store.get(SCOPE) is None
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        assert await service.restore_candidate(SCOPE) is None
        await service.enable(SCOPE,verified=True,online=True)
        candidate = await service.restore_candidate(SCOPE)
        assert candidate.cookie == COOKIE and COOKIE not in repr(candidate)
        await service.pause(SCOPE)
        store.close()
        vault.calls.clear()
        restarted = SavedSession(ChoiceStore(path),vault)
        assert await restarted.restore_candidate(SCOPE) is None and not vault.calls
        assert b'TEST_FAKE_SECRET' not in path.read_bytes()
        assert __import__('os').name == 'nt' or path.stat().st_mode & 0o777 == 0o600
    asyncio.run(run())


@pytest.mark.parametrize('sold',[False,True])
def test_revoke_or_sold_deletes_only_bound_credential_and_blocks_stale_refresh(tmp_path,sold):
    async def run():
        store, vault = ChoiceStore(tmp_path/'choices.sqlite'), FakeVault()
        service = SavedSession(store,vault)
        other = Scope('456','999999')
        await service.save(other,'unb=456; _m_h5_tk=OTHER_FAKE',explicit_consent=True)
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        await service.enable(SCOPE,verified=True,online=True)
        old = await service.restore_candidate(SCOPE)
        await service.revoke(SCOPE,sold=sold)
        assert (SERVICE,SCOPE.account) not in vault.values
        assert (SERVICE,other.account) in vault.values
        assert store.get(SCOPE).state == ('sold' if sold else 'revoked')
        assert await service.restore_candidate(SCOPE) is None
        assert not await service.refresh(old)
    asyncio.run(run())


def test_failed_delete_stays_revoked_across_restart_and_can_be_retried(tmp_path):
    async def run():
        path = tmp_path/'choices.sqlite'
        store, vault = ChoiceStore(path), FakeVault()
        service = SavedSession(store,vault)
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        await service.enable(SCOPE,verified=True,online=True)
        vault.fail.add('delete')
        with pytest.raises(SessionError,match='VAULT_DELETE_FAILED'):
            await service.revoke(SCOPE,sold=True)
        store.close()
        restarted = SavedSession(ChoiceStore(path),vault)
        assert await restarted.restore_candidate(SCOPE) is None
        with pytest.raises(SessionError,match='REVOCATION_INCOMPLETE'):
            await restarted.save(SCOPE,COOKIE,explicit_consent=True)
        vault.fail.clear()
        await restarted.revoke(SCOPE)  # Retry retains the original sold reason.
        assert not vault.values and restarted.choices.get(SCOPE).state == 'sold'
    asyncio.run(run())


@pytest.mark.parametrize('operation',['put','get','delete'])
def test_vault_errors_never_disclose_cookie(tmp_path,operation,capsys):
    async def run():
        store,vault=ChoiceStore(tmp_path/'choices.sqlite'),FakeVault()
        service=SavedSession(store,vault)
        if operation != 'put':
            await service.save(SCOPE,COOKIE,explicit_consent=True)
            await service.enable(SCOPE,verified=True,online=True)
        vault.fail.add(operation)
        with pytest.raises(SessionError) as error:
            if operation == 'put': await service.save(SCOPE,COOKIE,explicit_consent=True)
            elif operation == 'get': await service.restore_candidate(SCOPE)
            else: await service.revoke(SCOPE)
        import traceback
        assert COOKIE not in ''.join(traceback.format_exception(error.value))
        assert COOKIE not in str(capsys.readouterr())
    asyncio.run(run())


@pytest.mark.parametrize('change',['seller','item','generation','cookie_owner'])
def test_swapped_or_stale_vault_record_is_rejected(tmp_path,change):
    async def run():
        vault,store=FakeVault(),ChoiceStore(tmp_path/'choices.sqlite')
        service=SavedSession(store,vault)
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        await service.enable(SCOPE,verified=True,online=True)
        original=vault.values[SERVICE,SCOPE.account]
        changes={'seller':{'scope':Scope('456',SCOPE.item)},'item':{'scope':Scope(SCOPE.seller,'999999')},'generation':{'generation':'0'*32},'cookie_owner':{'cookie':'unb=456; _m_h5_tk=WRONG_FAKE'}}
        vault.values[SERVICE,SCOPE.account]=replace(original,**changes[change])
        with pytest.raises(SessionError,match='CREDENTIAL_SCOPE_MISMATCH'):
            await service.restore_candidate(SCOPE)
        vault.calls.clear()
        assert await service.restore_candidate(Scope('789','888888')) is None
        assert not vault.calls
    asyncio.run(run())


@pytest.mark.parametrize('operation',['pause','revoke'])
@pytest.mark.parametrize('delete_fails',[False,True])
def test_choice_write_failure_attempts_to_remove_credential_and_reports_partial_failure(tmp_path,monkeypatch,operation,delete_fails):
    async def run():
        path=tmp_path/'choices.sqlite'
        vault,store=FakeVault(),ChoiceStore(path)
        service=SavedSession(store,vault)
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        await service.enable(SCOPE,verified=True,online=True)
        old = await service.restore_candidate(SCOPE)
        def failed(choice): raise SessionError('CHOICE_WRITE_FAILED')
        monkeypatch.setattr(store,'put',failed)
        if delete_fails: vault.fail.add('delete')
        with pytest.raises(SessionError) as error:
            await getattr(service,operation)(SCOPE)
        if delete_fails:
            assert 'DISABLE_STARTUP_REQUIRED' in str(error.value)
            assert (SERVICE,SCOPE.account) in vault.values
        else:
            assert 'CREDENTIAL_REMOVED' in str(error.value)
            restarted=SavedSession(ChoiceStore(path),vault)
            assert await restarted.restore_candidate(SCOPE) is None
            assert not await service.refresh(old)
            assert await restarted.restore_candidate(SCOPE) is None
    asyncio.run(run())


def test_failed_save_never_persists_automatic_restore(tmp_path):
    async def run():
        path=tmp_path/'choices.sqlite'
        vault=FakeVault()
        vault.fail.add('put')
        service=SavedSession(ChoiceStore(path),vault)
        with pytest.raises(SessionError,match='VAULT_PUT_FAILED'):
            await service.save(SCOPE,COOKIE,explicit_consent=True)
        vault.fail.clear()
        restarted=SavedSession(ChoiceStore(path),vault)
        assert await restarted.restore_candidate(SCOPE) is None
        with pytest.raises(SessionError,match='NOT_READY_FOR_AUTO'):
            await restarted.enable(SCOPE,verified=True,online=True)
    asyncio.run(run())


def test_paused_refresh_does_not_enable_and_old_generation_cannot_overwrite_new_consent(tmp_path):
    async def run():
        store,vault=ChoiceStore(tmp_path/'choices.sqlite'),FakeVault()
        service=SavedSession(store,vault)
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        old=vault.values[SERVICE,SCOPE.account]
        refreshed=replace(old,cookie='unb=123; _m_h5_tk=REFRESHED_FAKE')
        assert await service.refresh(refreshed)
        assert await service.restore_candidate(SCOPE) is None
        await service.revoke(SCOPE)
        await service.save(SCOPE,COOKIE,explicit_consent=True)
        new=vault.values[SERVICE,SCOPE.account]
        assert not await service.refresh(refreshed)
        assert vault.values[SERVICE,SCOPE.account] == new
    asyncio.run(run())


def test_cancellation_does_not_let_inflight_write_resurrect_revoked_credential(tmp_path):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        class SlowVault(FakeVault):
            slow=False
            async def put(self,service,account,value):
                if self.slow:
                    entered.set()
                    await release.wait()
                await super().put(service,account,value)
        path=tmp_path/'choices.sqlite'
        vault=SlowVault()
        first=SavedSession(ChoiceStore(path),vault)
        second=SavedSession(ChoiceStore(path),vault)
        await first.save(SCOPE,COOKIE,explicit_consent=True)
        current=vault.values[SERVICE,SCOPE.account]
        vault.slow=True
        writing=asyncio.create_task(first.refresh(current))
        await entered.wait()
        writing.cancel()
        deleting=asyncio.create_task(second.revoke(SCOPE))
        await asyncio.sleep(0)
        writing.cancel()  # Repeated shutdown cancellation must not unlock early.
        await asyncio.sleep(.02)
        assert not deleting.done()
        release.set()
        await asyncio.gather(writing,return_exceptions=True)
        await deleting
        assert not vault.values and second.choices.get(SCOPE).state == 'revoked'
    asyncio.run(run())
