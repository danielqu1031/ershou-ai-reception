import asyncio
import json
import pytest
from keychain_vault import KeychainVault, APPROVED_SCOPE
from saved_session import Credential, SessionError, SERVICE

def test_native_helper_receives_credentials_only_on_stdin(monkeypatch):
    async def run():
        calls = []
        record = Credential(APPROVED_SCOPE,'a'*32,'unb=111111111; _m_h5_tk=FAKE_SECRET')
        class Process:
            returncode = 0
            async def communicate(self,payload):
                calls.append(payload)
                return b'',b''
        async def spawn(*args,**kwargs):
            calls.append(args)
            assert 'FAKE_SECRET' not in repr(args) and 'env' not in kwargs
            return Process()
        monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
        await KeychainVault().put(SERVICE,APPROVED_SCOPE.account,record)
        assert calls[0][-1] == 'put'
        assert json.loads(calls[1])['cookie'] == record.cookie
        with pytest.raises(SessionError):
            await KeychainVault().get(SERVICE,'other')
        assert len(calls) == 2
    asyncio.run(run())
