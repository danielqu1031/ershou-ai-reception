"""Native local-login-keychain backend, restricted to the approved scope."""
import asyncio
import json
import re
from pathlib import Path
from saved_session import Credential, Scope, SessionError, SERVICE

from settings import CONFIG
APPROVED_SCOPE = Scope(CONFIG['seller_id'], CONFIG['item_id'])


class KeychainVault:
    def __init__(self, helper=None):
        self.helper = Path(helper) if helper else Path(__file__).with_name('keychain-helper')

    async def _call(self, operation, service, account, payload=None):
        if service != SERVICE or account != APPROVED_SCOPE.account:
            raise SessionError('UNAPPROVED_KEYCHAIN_SCOPE')
        process = await asyncio.create_subprocess_exec(str(self.helper), operation,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            output, diagnostic = await asyncio.wait_for(process.communicate(payload), 60)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.communicate()
            raise SessionError('KEYCHAIN_UNAVAILABLE') from None
        if process.returncode == 4:
            return None
        if process.returncode != 0:
            code = re.fullmatch(rb'keychain-status:(-?\d+)\n?', diagnostic)
            if code:
                print('Keychain operation failed; OSStatus=' + code[1].decode(), flush=True)
            raise SessionError('KEYCHAIN_UNAVAILABLE')
        return output

    async def get(self, service, account):
        raw = await self._call('get',service,account)
        if raw is None: return None
        try:
            data = json.loads(raw)
            return Credential(Scope(data['seller'],data['item']), data['generation'], data['cookie'])
        except Exception:
            raise SessionError('KEYCHAIN_RECORD_INVALID') from None

    async def put(self, service, account, value):
        if value.scope != APPROVED_SCOPE:
            raise SessionError('UNAPPROVED_KEYCHAIN_SCOPE')
        data = json.dumps({'seller':value.scope.seller,'item':value.scope.item,
            'generation':value.generation,'cookie':value.cookie},separators=(',',':')).encode()
        await self._call('put',service,account,data)

    async def delete(self, service, account):
        await self._call('delete',service,account)
