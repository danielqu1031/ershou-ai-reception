"""Consent-gated recovery policy with an injected credential vault.

No macOS Keychain backend is provided or instantiated here. Tests inject a fake
vault. SQLite contains only scope, choices and revocation state, never cookies.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import fcntl
import os
from pathlib import Path
import re
import sqlite3
from typing import Protocol
import uuid

from settings import SERVICE
STATES = {'saving', 'ready', 'paused', 'revoking', 'revoking_sold', 'revoked', 'sold'}


class SessionError(Exception):
    """Only fixed, public error codes may cross this boundary."""


@dataclass(frozen=True)
class Scope:
    seller: str
    item: str

    def __post_init__(self):
        if not isinstance(self.seller,str) or not isinstance(self.item,str) or not re.fullmatch(r'\d{1,24}', self.seller) or not re.fullmatch(r'\d{6,24}', self.item):
            raise SessionError('INVALID_SCOPE')

    @property
    def account(self):
        return self.seller + ':' + self.item


@dataclass(frozen=True)
class Credential:
    scope: Scope
    generation: str
    cookie: str = field(repr=False)


class Vault(Protocol):
    async def get(self, service: str, account: str) -> Credential | None: ...
    async def put(self, service: str, account: str, value: Credential) -> None: ...
    async def delete(self, service: str, account: str) -> None: ...


@dataclass(frozen=True)
class Choice:
    scope: Scope
    generation: str
    state: str
    auto: bool = False


class ChoiceStore:
    def __init__(self, path):
        path = Path(path)
        self.path = path.resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS choices (seller TEXT,item TEXT,generation TEXT,state TEXT,auto INTEGER,PRIMARY KEY(seller,item))')
        self.db.commit()

    def get(self, scope):
        try:
            row = self.db.execute('SELECT generation,state,auto FROM choices WHERE seller=? AND item=?', (scope.seller, scope.item)).fetchone()
            if not row:
                return None
            generation, state, auto = row
            if state not in STATES or auto not in (0,1) or not re.fullmatch('[a-f0-9]{32}', generation):
                raise ValueError()
            return Choice(scope, generation, state, bool(auto))
        except Exception:
            raise SessionError('CHOICE_READ_FAILED') from None

    def put(self, choice):
        try:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO choices VALUES (?,?,?,?,?)', (choice.scope.seller, choice.scope.item, choice.generation, choice.state, int(choice.auto)))
        except Exception:
            raise SessionError('CHOICE_WRITE_FAILED') from None

    def close(self):
        self.db.close()

    @asynccontextmanager
    async def exclusive(self):
        # Serialize settings + external-vault operations across overlapping
        # service instances, without blocking the event loop on a file lock.
        try:
            fd = os.open(str(self.path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            raise SessionError('CHOICE_LOCK_FAILED') from None
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.01)
            yield
        finally:
            os.close(fd)


def validate_cookie(scope, cookie):
    if not isinstance(cookie, str) or not 1 <= len(cookie) <= 32000 or '\r' in cookie or '\n' in cookie:
        raise SessionError('INVALID_CREDENTIAL')
    parts = {}
    for part in cookie.split(';'):
        key, sep, value = part.strip().partition('=')
        if sep:
            if key in parts:
                raise SessionError('INVALID_CREDENTIAL')
            parts[key] = value
    if parts.get('unb') != scope.seller or not parts.get('_m_h5_tk'):
        raise SessionError('CREDENTIAL_SCOPE_MISMATCH')


class SavedSession:
    def __init__(self, choices, vault):
        self.choices, self.vault = choices, vault

    async def _vault(self, operation, scope, value=None):
        try:
            method = getattr(self.vault, operation)
            args = (SERVICE, scope.account) if value is None else (SERVICE, scope.account, value)
            task = asyncio.create_task(method(*args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A native vault call may keep running after cancellation.
                # Keep ownership until it completes, then permit revocation.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()  # Consume a failure without exposing it.
                raise
        except Exception:
            raise SessionError('VAULT_' + operation.upper() + '_FAILED') from None

    async def _checked(self, choice):
        credential = await self._vault('get', choice.scope)
        if credential is None:
            return None
        if not isinstance(credential, Credential) or credential.scope != choice.scope or credential.generation != choice.generation:
            raise SessionError('CREDENTIAL_SCOPE_MISMATCH')
        validate_cookie(choice.scope, credential.cookie)
        return credential

    async def save(self, scope, cookie, *, explicit_consent=False):
        if explicit_consent is not True:
            raise SessionError('EXPLICIT_CONSENT_REQUIRED')
        validate_cookie(scope, cookie)
        async with self.choices.exclusive():
            current = self.choices.get(scope)
            if current and current.state.startswith('revoking'):
                raise SessionError('REVOCATION_INCOMPLETE')
            if current and current.state == 'sold':
                raise SessionError('ITEM_ALREADY_SOLD')
            choice = Choice(scope, uuid.uuid4().hex, 'saving')
            self.choices.put(choice)  # A crash during save must leave auto off.
            await self._vault('put', scope, Credential(scope, choice.generation, cookie))
            self.choices.put(replace(choice, state='paused'))

    async def enable(self, scope, *, verified=False, online=False):
        async with self.choices.exclusive():
            choice = self.choices.get(scope)
            if not choice or choice.state not in ('ready','paused') or verified is not True or online is not True:
                raise SessionError('NOT_READY_FOR_AUTO')
            if await self._checked(choice) is None:
                raise SessionError('CREDENTIAL_MISSING')
            self.choices.put(replace(choice, state='ready', auto=True))

    async def restore_candidate(self, scope):
        """Return login material only; caller must reverify login/item/holds.

        This never starts a connection or sends a message.
        """
        async with self.choices.exclusive():
            choice = self.choices.get(scope)
            if not choice or choice.state != 'ready' or not choice.auto:
                return None
            return await self._checked(choice)

    async def pause(self, scope):
        # Caller must pause the runtime before awaiting persistence.
        async with self.choices.exclusive():
            choice = self.choices.get(scope)
            if not choice:
                return
            try:
                self.choices.put(replace(choice, state='paused' if choice.state in ('ready','paused') else choice.state, auto=False))
            except SessionError:
                # If choices cannot be saved, removing login prevents a restart
                # from using the old auto setting. Report partial failure.
                try:
                    await self._vault('delete', scope)
                except SessionError:
                    raise SessionError('PAUSE_NOT_DURABLE_DISABLE_STARTUP_REQUIRED') from None
                raise SessionError('PAUSE_CHOICE_FAILED_CREDENTIAL_REMOVED') from None

    async def refresh(self, credential):
        validate_cookie(credential.scope, credential.cookie)
        async with self.choices.exclusive():
            choice = self.choices.get(credential.scope)
            if not choice or choice.generation != credential.generation or choice.state not in ('ready','paused'):
                return False
            if await self._checked(choice) is None:
                return False
            await self._vault('put', credential.scope, credential)
            return True

    async def revoke(self, scope, *, sold=False):
        # A durable tombstone must precede external deletion. Deletion can be
        # retried, but failure must never make an old login eligible again.
        async with self.choices.exclusive():
            choice = self.choices.get(scope)
            if not choice:
                return
            sold = sold or choice.state in ('sold', 'revoking_sold')
            pending = replace(choice, state='revoking_sold' if sold else 'revoking', auto=False)
            try:
                self.choices.put(pending)
            except SessionError:
                try:
                    await self._vault('delete', scope)
                except SessionError:
                    raise SessionError('REVOCATION_NOT_DURABLE_DISABLE_STARTUP_REQUIRED') from None
                raise SessionError('REVOCATION_CHOICE_FAILED_CREDENTIAL_REMOVED') from None
            await self._vault('delete', scope)
            self.choices.put(replace(pending, state='sold' if sold else 'revoked'))
